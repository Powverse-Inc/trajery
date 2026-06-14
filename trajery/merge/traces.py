"""Merge traces from multi-day delivery_to_teich outputs."""

from __future__ import annotations

import json
import os
import shutil
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal

from trajery.merge.common import (
    DISCARDED_REASON,
    discover_day_dirs,
    is_discarded_session_id,
    read_session_meta_id,
    safe_filename,
    count_trace_messages,
)
from trajery.paths import manifest_trace_path, report_path

CopyMode = Literal["copy", "hardlink"]


@dataclass
class TraceCandidate:
    day: str
    path: Path
    session_id: str
    messages_count: int
    source_order: int


@dataclass
class MergeTracesResult:
    manifest: dict[str, Any]
    post_dedup_files: int = 0


def _copy_trace(src: Path, dst: Path, mode: CopyMode) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst.unlink()
    if mode == "hardlink":
        os.link(src, dst)
    else:
        shutil.copy2(src, dst)


def merge_traces(
    input_root: Path,
    output_dir: Path,
    *,
    mode: CopyMode = "copy",
    dry_run: bool = False,
    include_days: list[str] | None = None,
    verbose_manifest: bool = False,
    progress_every: int = 500,
    report_absolute_paths: bool = False,
    log: Callable[[str], None] | None = print,
) -> MergeTracesResult:
    """Quality-filter, cross-day dedup, and copy traces into merged/traces/."""
    day_dirs = discover_day_dirs(input_root, include_days=include_days)
    if not day_dirs:
        raise ValueError(f"No day directories with report.json under {input_root}")

    traces_out = output_dir / "traces"
    if not dry_run:
        traces_out.mkdir(parents=True, exist_ok=True)

    discarded: list[dict[str, Any]] = []
    per_day_stats: dict[str, dict[str, int]] = defaultdict(
        lambda: {
            "pre_filter": 0,
            "discarded_unknown_session": 0,
            "kept_after_quality_filter": 0,
        }
    )
    kept: list[TraceCandidate] = []
    source_order = 0

    for day_dir in day_dirs:
        traces_dir = day_dir / "traces"
        if not traces_dir.is_dir():
            continue
        day = day_dir.name
        for trace_path in sorted(traces_dir.glob("*.jsonl")):
            per_day_stats[day]["pre_filter"] += 1
            session_id = read_session_meta_id(trace_path)

            if is_discarded_session_id(session_id):
                per_day_stats[day]["discarded_unknown_session"] += 1
                if verbose_manifest:
                    discarded.append(
                        {
                            "day": day,
                            "path": manifest_trace_path(day, trace_path),
                            "reason": session_id or "missing-session-id",
                        }
                    )
                continue

            assert session_id is not None
            msg_count = count_trace_messages(trace_path)
            kept.append(
                TraceCandidate(
                    day=day,
                    path=trace_path,
                    session_id=session_id,
                    messages_count=msg_count,
                    source_order=source_order,
                )
            )
            per_day_stats[day]["kept_after_quality_filter"] += 1
            source_order += 1

            if log and progress_every and source_order % progress_every == 0:
                log(f"[scan] indexed {source_order} kept traces...")

    pre_filter_files = sum(s["pre_filter"] for s in per_day_stats.values())
    discarded_count = sum(s["discarded_unknown_session"] for s in per_day_stats.values())
    post_filter_files = len(kept)

    # Cross-day dedup: longest messages_count wins; tie → earlier source_order.
    by_session: dict[str, list[TraceCandidate]] = defaultdict(list)
    for candidate in kept:
        by_session[candidate.session_id].append(candidate)

    winners: list[TraceCandidate] = []
    dedup_losers: list[dict[str, Any]] = []
    cross_day_duplicates = 0

    for session_id, group in by_session.items():
        ordered = sorted(group, key=lambda c: c.source_order)
        winner = ordered[0]
        for other in ordered[1:]:
            if other.messages_count > winner.messages_count:
                dedup_losers.append(
                    {
                        "session_id": session_id,
                        "day": winner.day,
                        "path": manifest_trace_path(winner.day, winner.path),
                        "messages_count": winner.messages_count,
                    }
                )
                winner = other
                cross_day_duplicates += 1
            else:
                cross_day_duplicates += 1
                dedup_losers.append(
                    {
                        "session_id": session_id,
                        "day": other.day,
                        "path": manifest_trace_path(other.day, other.path),
                        "messages_count": other.messages_count,
                    }
                )
        winners.append(winner)

    post_dedup_files = len(winners)

    if log:
        log(
            f"merge_traces: pre_filter={pre_filter_files} "
            f"discarded={discarded_count} post_filter={post_filter_files} "
            f"post_dedup={post_dedup_files} cross_day_dedup={cross_day_duplicates}"
        )

    if not dry_run:
        for idx, winner in enumerate(winners, start=1):
            out_name = safe_filename(winner.session_id) + ".jsonl"
            _copy_trace(winner.path, traces_out / out_name, mode)
            if log and progress_every and idx % progress_every == 0:
                log(f"[copy] {idx}/{post_dedup_files} traces...")

    manifest: dict[str, Any] = {
        "input_root": report_path(input_root, absolute=report_absolute_paths),
        "output_dir": report_path(output_dir, absolute=report_absolute_paths),
        "source_days": [d.name for d in day_dirs],
        "copy_mode": mode,
        "dry_run": dry_run,
        "pre_filter_files": pre_filter_files,
        "discarded_unknown_session": discarded_count,
        "discarded_reason": DISCARDED_REASON,
        "post_filter_files": post_filter_files,
        "cross_day_duplicates": cross_day_duplicates,
        "post_dedup_files": post_dedup_files,
        "per_day": [
            {
                "date": day,
                "pre_filter": stats["pre_filter"],
                "discarded_unknown_session": stats["discarded_unknown_session"],
                "kept_after_quality_filter": stats["kept_after_quality_filter"],
            }
            for day, stats in sorted(per_day_stats.items())
        ],
        "discarded": discarded if verbose_manifest else [],
        "dedup_losers": dedup_losers if verbose_manifest else [],
    }

    manifest_path = output_dir / "merge_manifest.json"
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    if log:
        log(f"manifest: {manifest_path}")

    return MergeTracesResult(manifest=manifest, post_dedup_files=post_dedup_files)
