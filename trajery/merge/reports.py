"""Merge report.json files from multi-day delivery_to_teich outputs."""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from trajery.merge.common import DISCARDED_REASON, discover_day_dirs, load_json
from trajery.paths import report_path
from trajery.report import to_markdown_report


SUM_FIELDS = (
    "scanned",
    "parse_errors",
    "json_line_errors",
    "unwrapped",
    "filter_kept",
    "filter_dropped",
    "dedup_dropped",
    "teich_valid",
    "teich_incomplete",
    "teich_invalid",
    "export_total",
    "elapsed_seconds",
)

COUNTER_FIELDS = ("unwrap_failures", "drop_reasons", "teich_errors")


def _merge_counters(reports: list[dict[str, Any]], key: str) -> dict[str, int]:
    merged: Counter[str] = Counter()
    for report in reports:
        block = report.get(key)
        if isinstance(block, dict):
            merged.update({str(k): int(v) for k, v in block.items()})
    return dict(merged.most_common())


def _per_day_rows(day_dirs: list[Path], manifest: dict[str, Any] | None) -> list[dict[str, Any]]:
    manifest_by_day: dict[str, dict[str, Any]] = {}
    if manifest:
        for row in manifest.get("per_day") or []:
            if isinstance(row, dict) and row.get("date"):
                manifest_by_day[str(row["date"])] = row

    rows: list[dict[str, Any]] = []
    for day_dir in day_dirs:
        report = load_json(day_dir / "report.json")
        day = day_dir.name
        m = manifest_by_day.get(day, {})
        teich_valid = int(report.get("teich_valid") or 0)
        discarded = int(m.get("discarded_unknown_session") or 0)
        kept = int(m.get("kept_after_quality_filter") or max(teich_valid - discarded, 0))
        rows.append(
            {
                "date": day,
                "scanned": int(report.get("scanned") or 0),
                "teich_valid": teich_valid,
                "teich_incomplete": int(report.get("teich_incomplete") or 0),
                "teich_invalid": int(report.get("teich_invalid") or 0),
                "elapsed_seconds": float(report.get("elapsed_seconds") or 0),
                "discarded_unknown_session": discarded,
                "kept_after_quality_filter": kept,
            }
        )
    return rows


def _quality_filter_section(manifest: dict[str, Any] | None, merged_valid: int) -> dict[str, Any]:
    if manifest:
        return {
            "discarded_unknown_session": int(manifest.get("discarded_unknown_session") or 0),
            "discarded_reason": manifest.get("discarded_reason") or DISCARDED_REASON,
            "teich_valid_before_filter": int(manifest.get("pre_filter_files") or merged_valid),
            "teich_valid_after_filter": int(manifest.get("post_filter_files") or 0),
        }
    return {
        "discarded_unknown_session": None,
        "discarded_reason": DISCARDED_REASON,
        "teich_valid_before_filter": merged_valid,
        "teich_valid_after_filter": None,
        "note": "Run merge_traces first and pass --manifest for quality_filter counts.",
    }


def _cross_day_section(manifest: dict[str, Any] | None) -> dict[str, Any]:
    if manifest:
        return {
            "duplicate_session_keys": int(manifest.get("cross_day_duplicates") or 0),
            "dedup_dropped": int(manifest.get("cross_day_duplicates") or 0),
            "teich_valid_final": int(manifest.get("post_dedup_files") or 0),
        }
    return {
        "duplicate_session_keys": None,
        "dedup_dropped": None,
        "teich_valid_final": None,
        "note": "Run merge_traces first and pass --manifest for cross_day_dedup counts.",
    }


def build_merged_report(
    input_root: Path,
    *,
    manifest: dict[str, Any] | None = None,
    include_days: list[str] | None = None,
    report_absolute_paths: bool = False,
) -> dict[str, Any]:
    """Build merged report.json dict from per-day reports and optional manifest."""
    day_dirs = discover_day_dirs(input_root, include_days=include_days)
    if not day_dirs:
        raise ValueError(f"No day directories with report.json under {input_root}")

    reports = [load_json(d / "report.json") for d in day_dirs]
    merged: dict[str, Any] = {
        "report_type": "merged_delivery_output",
        "input_root": report_path(input_root, absolute=report_absolute_paths),
        "source_days": [d.name for d in day_dirs],
    }

    for field in SUM_FIELDS:
        merged[field] = sum(int(r.get(field) or 0) for r in reports)

    for field in COUNTER_FIELDS:
        merged[field] = _merge_counters(reports, field)

    tar_warnings: list[Any] = []
    for r in reports:
        tw = r.get("tar_warnings")
        if isinstance(tw, list):
            tar_warnings.extend(tw)
    merged["tar_warnings"] = tar_warnings
    merged["tar_archives_with_multiple_jsonl"] = len(tar_warnings)
    merged["tar_skipped_jsonl_members"] = sum(
        len(w.get("skipped_members") or []) for w in tar_warnings if isinstance(w, dict)
    )

    export_total = merged["teich_valid"] + merged["teich_incomplete"] + merged["teich_invalid"]
    merged["export_total"] = export_total
    merged["funnel"] = {
        "teich_valid_rate": round(merged["teich_valid"] / export_total, 4) if export_total else None,
        "teich_incomplete_rate": (
            round(merged["teich_incomplete"] / export_total, 4) if export_total else None
        ),
        "teich_invalid_rate": (
            round(merged["teich_invalid"] / export_total, 4) if export_total else None
        ),
    }
    merged["elapsed_seconds_max"] = max(float(r.get("elapsed_seconds") or 0) for r in reports)
    merged["teich_trace_results"] = []

    per_day = _per_day_rows(day_dirs, manifest)
    merged["per_day"] = per_day
    merged["pre_cross_day_dedup"] = {
        "teich_valid": merged["teich_valid"],
        "trace_files": merged["teich_valid"],
    }
    merged["quality_filter"] = _quality_filter_section(manifest, merged["teich_valid"])
    merged["cross_day_dedup"] = _cross_day_section(manifest)

    if manifest:
        merged["output_dir"] = manifest.get("output_dir")
        merged["merge_manifest"] = str(
            Path(manifest.get("output_dir") or input_root / "merged") / "merge_manifest.json"
        )

    return merged


def to_merged_markdown_report(report: dict[str, Any]) -> str:
    """Markdown report: base funnel + per-day + quality filter + cross-day sections."""
    base = to_markdown_report(report)
    lines = [base.rstrip(), "", "## 分日汇总", ""]
    lines.append("| 日期 | scanned | teich_valid | incomplete | 淘汰 unknown-session | 保留 | 耗时 |")
    lines.append("|------|---------|-------------|------------|----------------------|------|------|")
    for row in report.get("per_day") or []:
        elapsed = row.get("elapsed_seconds")
        elapsed_s = f"{elapsed:.0f}s" if isinstance(elapsed, (int, float)) else "—"
        lines.append(
            f"| {row.get('date', '')} | {row.get('scanned', 0)} | {row.get('teich_valid', 0)} | "
            f"{row.get('teich_incomplete', 0)} | {row.get('discarded_unknown_session', 0)} | "
            f"{row.get('kept_after_quality_filter', 0)} | {elapsed_s} |"
        )

    qf = report.get("quality_filter") or {}
    lines.extend(["", "## 质量过滤（unknown-session 淘汰）", ""])
    lines.append(f"- **淘汰原因**: {qf.get('discarded_reason', '—')}")
    lines.append(f"- **过滤前 valid 文件数**: {qf.get('teich_valid_before_filter', '—')}")
    lines.append(f"- **淘汰 unknown-session**: {qf.get('discarded_unknown_session', '—')}")
    lines.append(f"- **过滤后保留**: {qf.get('teich_valid_after_filter', '—')}")

    cd = report.get("cross_day_dedup") or {}
    lines.extend(["", "## 跨天 dedup", ""])
    lines.append(f"- **跨天重复 session 数**: {cd.get('duplicate_session_keys', '—')}")
    lines.append(f"- **dedup 淘汰**: {cd.get('dedup_dropped', '—')}")
    lines.append(f"- **最终 teich_valid**: {cd.get('teich_valid_final', '—')}")
    lines.append("")
    return "\n".join(lines)


def merge_reports(
    input_root: Path,
    output_dir: Path,
    *,
    manifest_path: Path | None = None,
    include_days: list[str] | None = None,
    report_absolute_paths: bool = False,
    log: Callable[[str], None] | None = print,
) -> dict[str, Any]:
    """Write merged report.json and report.md under output_dir."""
    manifest: dict[str, Any] | None = None
    if manifest_path is not None:
        if not manifest_path.is_file():
            raise FileNotFoundError(f"manifest not found: {manifest_path}")
        manifest = load_json(manifest_path)

    report = build_merged_report(
        input_root,
        manifest=manifest,
        include_days=include_days,
        report_absolute_paths=report_absolute_paths,
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    report_json = output_dir / "report.json"
    report_md = output_dir / "report.md"
    report_json.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    report_md.write_text(to_merged_markdown_report(report), encoding="utf-8")

    if log:
        log(f"report: {report_json}")
        log(f"report: {report_md}")
        qf = report.get("quality_filter") or {}
        cd = report.get("cross_day_dedup") or {}
        log(
            f"summary: teich_valid={report.get('teich_valid')} "
            f"discarded={qf.get('discarded_unknown_session')} "
            f"final={cd.get('teich_valid_final')}"
        )

    return report
