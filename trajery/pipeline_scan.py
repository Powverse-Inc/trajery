"""单文件 scan worker 与并行 scan 结果合并 / Per-source scan workers and merge helpers."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from filter_traj_multi_plat import compute_session_id, evaluate, extract
from trajery.export import openai_responses_to_codex_events
from trajery.parser.delivery import iter_records_from_source, iter_tar_jsonl_with_meta
from trajery.parser import (
    classify_unwrap_failure,
    session_key_from_record,
    unwrap_delivery_record,
)
from trajery.paths import write_sidecar_jsonl
from trajery.pipeline import PipelineStats

DedupEntry = tuple[int, dict[str, Any], dict[str, Any], str]
PendingExport = tuple[dict[str, Any], list[dict[str, Any]], str, str]


def _safe_filename(session_key: str) -> str:
    safe = session_key.replace(":", "_").replace("/", "_").replace("\\", "_")
    safe = safe.replace("<", "_").replace(">", "_").replace("|", "_")
    return safe[:180] if len(safe) > 180 else safe


@dataclass
class ScanWorkerConfig:
    """Picklable scan worker configuration / Config passed to each worker process."""

    apply_filter: bool
    dedup: bool
    keep_unwrapped: bool
    write_dropped: bool
    dropped_dir: str | None
    unwrapped_dir: str
    traces_dir: str


@dataclass
class SourceScanResult:
    """单源文件 scan 结果 / Result from scanning one delivery source file."""

    stats: PipelineStats = field(default_factory=PipelineStats)
    dedup_winners: dict[str, DedupEntry] = field(default_factory=dict)
    tar_warnings: list[dict[str, Any]] = field(default_factory=list)
    size_warnings: list[dict[str, Any]] = field(default_factory=list)
    pending_exports: list[PendingExport] = field(default_factory=list)


def _source_order_key(item: dict[str, Any], source_order: dict[str, int]) -> tuple[int, int]:
    rel = str(item.get("source_file") or "").split(":")[0]
    return (source_order[rel], int(item.get("source_line") or 0))


def pick_dedup_winner(
    a: DedupEntry,
    b: DedupEntry,
    source_order: dict[str, int],
) -> tuple[DedupEntry, DedupEntry]:
    """Return (winner, loser) using serial-equivalent dedup rules."""
    if a[0] != b[0]:
        return (a, b) if a[0] > b[0] else (b, a)
    key_a = _source_order_key(a[2], source_order)
    key_b = _source_order_key(b[2], source_order)
    return (a, b) if key_a <= key_b else (b, a)


def merge_dedup_winners(
    parts: list[dict[str, DedupEntry]],
    source_order: dict[str, int],
    *,
    write_dropped: bool,
    dropped_dir: Path | None,
) -> tuple[dict[str, DedupEntry], int]:
    """Merge per-worker dedup buffers by replaying serial file order per session."""
    entries_by_sid: dict[str, list[DedupEntry]] = {}
    for local in parts:
        for sid, entry in local.items():
            entries_by_sid.setdefault(sid, []).append(entry)

    global_winners: dict[str, DedupEntry] = {}
    cross_file_drops = 0

    for sid, entries in entries_by_sid.items():
        ordered = sorted(entries, key=lambda entry: _source_order_key(entry[2], source_order))
        global_winners[sid] = ordered[0]
        for entry in ordered[1:]:
            prev = global_winners[sid]
            msg_count = entry[0]
            if prev[0] >= msg_count:
                cross_file_drops += 1
                if write_dropped and dropped_dir is not None:
                    record = entry[2].get("record")
                    if isinstance(record, dict):
                        dropped = {**record, "_drop_reasons": ["session_id_duplicate"]}
                        item = entry[2]
                        write_sidecar_jsonl(
                            dropped_dir,
                            str(item["source_file"]),
                            int(item["source_line"]),
                            dropped,
                        )
            else:
                global_winners[sid] = entry

    return global_winners, cross_file_drops


def merge_stats(parts: list[PipelineStats], *, cross_file_drops: int = 0) -> PipelineStats:
    """Sum counters from worker-local stats into one PipelineStats."""
    merged = PipelineStats()
    for part in parts:
        merged.scanned += part.scanned
        merged.parse_errors += part.parse_errors
        merged.json_line_errors += part.json_line_errors
        merged.unwrap_failures.update(part.unwrap_failures)
        merged.unwrapped += part.unwrapped
        merged.filter_kept += part.filter_kept
        merged.filter_dropped += part.filter_dropped
        merged.dedup_dropped += part.dedup_dropped
        merged.drop_reasons.update(part.drop_reasons)
        merged.tar_warnings.extend(part.tar_warnings)
        merged.size_warnings.extend(part.size_warnings)
    if cross_file_drops:
        merged.dedup_dropped += cross_file_drops
        merged.drop_reasons["session_id_duplicate"] += cross_file_drops
    return merged


def _process_record(
    *,
    item: dict[str, Any],
    stats: PipelineStats,
    dedup_winners: dict[str, DedupEntry],
    pending_exports: list[PendingExport],
    config: ScanWorkerConfig,
    dropped_dir: Path | None,
    unwrapped_dir: Path,
    traces_dir: Path,
) -> None:
    stats.scanned += 1
    record = item.get("record")

    if not isinstance(record, dict):
        stats.parse_errors += 1
        stats.json_line_errors += 1
        return

    failure = classify_unwrap_failure(record)
    if failure is not None:
        stats.parse_errors += 1
        stats.unwrap_failures[failure] += 1
        return

    unwrapped = unwrap_delivery_record(record)
    if unwrapped is None:
        stats.parse_errors += 1
        stats.unwrap_failures["unwrap_error"] += 1
        return
    stats.unwrapped += 1

    if config.keep_unwrapped:
        write_sidecar_jsonl(
            unwrapped_dir,
            str(item["source_file"]),
            int(item["source_line"]),
            unwrapped,
        )

    trajectory = extract(unwrapped)
    passed, reasons = evaluate(trajectory)
    if config.apply_filter and not passed:
        stats.filter_dropped += 1
        stats.drop_reasons.update(reasons)
        if config.write_dropped and dropped_dir is not None:
            dropped = {**record, "_drop_reasons": reasons}
            write_sidecar_jsonl(
                dropped_dir,
                str(item["source_file"]),
                int(item["source_line"]),
                dropped,
            )
        return

    stats.filter_kept += 1
    session_key = session_key_from_record(record)
    msg_count = len(trajectory.get("messages") or [])

    if config.dedup:
        sid = compute_session_id(trajectory) or session_key
        prev = dedup_winners.get(sid)
        if prev is not None and prev[0] >= msg_count:
            stats.dedup_dropped += 1
            stats.drop_reasons["session_id_duplicate"] += 1
            if config.write_dropped and dropped_dir is not None:
                dropped = {**record, "_drop_reasons": ["session_id_duplicate"]}
                write_sidecar_jsonl(
                    dropped_dir,
                    str(item["source_file"]),
                    int(item["source_line"]),
                    dropped,
                )
            return
        dedup_winners[sid] = (msg_count, unwrapped, item, session_key)
        return

    events = openai_responses_to_codex_events(unwrapped)
    trace_name = _safe_filename(session_key) + ".jsonl"
    pending_exports.append(
        (unwrapped, events, session_key, str(traces_dir / trace_name))
    )


def scan_one_source(args: tuple[str, str, ScanWorkerConfig]) -> SourceScanResult:
    """Scan one delivery source file (worker entry point for ProcessPool)."""
    rel, path_str, config = args
    path = Path(path_str)
    result = SourceScanResult()
    dropped_dir = Path(config.dropped_dir) if config.dropped_dir else None
    unwrapped_dir = Path(config.unwrapped_dir)
    traces_dir = Path(config.traces_dir)

    if path.name.endswith(".tar.gz"):
        for kind, *payload in iter_tar_jsonl_with_meta(
            path,
            rel,
            size_warnings=result.size_warnings,
        ):
            if kind == "meta":
                meta = payload[0]
                if meta["jsonl_member_count"] > 1:
                    result.tar_warnings.append({**meta, "source_file": rel})
                continue
            source_file, line_no, record = payload
            item = {
                "source_file": source_file,
                "source_line": line_no,
                "record": record,
            }
            _process_record(
                item=item,
                stats=result.stats,
                dedup_winners=result.dedup_winners,
                pending_exports=result.pending_exports,
                config=config,
                dropped_dir=dropped_dir,
                unwrapped_dir=unwrapped_dir,
                traces_dir=traces_dir,
            )
        return result

    for item in iter_records_from_source(
        rel,
        path,
        size_warnings=result.size_warnings,
    ):
        _process_record(
            item=item,
            stats=result.stats,
            dedup_winners=result.dedup_winners,
            pending_exports=result.pending_exports,
            config=config,
            dropped_dir=dropped_dir,
            unwrapped_dir=unwrapped_dir,
            traces_dir=traces_dir,
        )
    result.stats.size_warnings = list(result.size_warnings)
    return result


__all__ = [
    "DedupEntry",
    "PendingExport",
    "ScanWorkerConfig",
    "SourceScanResult",
    "merge_dedup_winners",
    "merge_stats",
    "pick_dedup_winner",
    "scan_one_source",
]
