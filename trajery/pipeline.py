"""扫描 → 筛选 → 去重 → 导出流水线 orchestrator。
Scan → filter → dedup → export pipeline.

中文：
- 本模块是 delivery → Teich Codex trace 的**主流水线 orchestrator**。
- 三阶段：Scan（iter → unwrap → R1–R7 → dedup 缓冲）→ Export build（dedup winners）
  → Export write + Teich validate（traces/ / incomplete/ / invalid/）。
- 依赖：``trajery.parser``（L0→L1）、``filter_traj_multi_plat``（L2 规则 R1–R7）、
  ``trajery.export``（L1→L3 + Teich 校验）。
- 流程图见 USER_GUIDE §2；报表字段见 USER_GUIDE §6.2。

English:
- Main pipeline orchestrator: scan, filter, dedup, export, and Teich validation.
- See USER_GUIDE §2 for flowchart; §6.2 for report.json field definitions.

指标关系 (metric tree)::

    scanned
      ├─ parse_errors (+ json_line_errors)
      └─ unwrapped
           ├─ filter_dropped → dropped/
           └─ filter_kept
                ├─ dedup_dropped → dropped/
                └─ export → teich_valid / teich_incomplete / teich_invalid
"""

from __future__ import annotations

import json
import shutil
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from filter_traj_multi_plat import compute_session_id, evaluate, extract
from trajery.export import (
    check_teich_available,
    openai_responses_to_codex_events,
    validate_trace_with_teich,
    write_trace,
)
from trajery.parser import (
    classify_unwrap_failure,
    iter_delivery_records,
    session_key_from_record,
    unwrap_delivery_record,
)


@dataclass
class PipelineStats:
    """流水线运行统计 / Mutable counters collected during ``process()``.

    中文：各字段递增时机与输出目录对应关系：
    - ``scanned``: 每 yield 一条 delivery 记录
    - ``parse_errors``: record 非 dict 或 unwrap 失败
    - ``json_line_errors``: record is None（行级 JSON 失败）
    - ``unwrap_failures``: Counter，键为失败码 → ``report.unwrap_failures``
    - ``unwrapped``: unwrap 成功；``--keep-unwrapped`` → ``unwrapped/``
    - ``filter_kept`` / ``filter_dropped``: R1–R7 通过/淘汰
    - ``dedup_dropped``: 同 session 较短快照 → ``dropped/`` (session_id_duplicate)
    - ``drop_reasons``: Counter，含 R1–R7 与 dedup → ``report.drop_reasons``
    - ``teich_valid``: 校验通过且 complete → 保留 ``traces/``
    - ``teich_incomplete``: 转换 OK 但 incomplete → ``incomplete/``
    - ``teich_invalid``: 转换失败 → ``invalid/``
    - ``teich_errors``: invalid 错误消息 Counter
    - ``tar_warnings``: 多成员 tar 告警 → ``report.tar_warnings``

    English: Counters map to report.json and output subdirectories.
    See USER_GUIDE §6.3 for metric relationships.
    """

    scanned: int = 0
    parse_errors: int = 0
    json_line_errors: int = 0
    unwrap_failures: Counter = field(default_factory=Counter)
    unwrapped: int = 0
    filter_kept: int = 0
    filter_dropped: int = 0
    dedup_dropped: int = 0
    teich_valid: int = 0
    teich_incomplete: int = 0
    teich_invalid: int = 0
    drop_reasons: Counter = field(default_factory=Counter)
    teich_errors: Counter = field(default_factory=Counter)
    tar_warnings: list[dict[str, Any]] = field(default_factory=list)

    def to_report(self, *, input_dir: Path, output_dir: Path) -> dict[str, Any]:
        """序列化为 report.json 字典 / Serialize stats to report.json schema.

        中文：字段与 USER_GUIDE §6.2 一一对应；``tar_*`` 字段由 ``tar_warnings`` 聚合。

        English: Produces the JSON report dict written by CLI.
        """
        return {
            "input_dir": str(input_dir),
            "output_dir": str(output_dir),
            "scanned": self.scanned,
            "parse_errors": self.parse_errors,
            "json_line_errors": self.json_line_errors,
            "unwrap_failures": dict(self.unwrap_failures.most_common()),
            "unwrapped": self.unwrapped,
            "filter_kept": self.filter_kept,
            "filter_dropped": self.filter_dropped,
            "dedup_dropped": self.dedup_dropped,
            "teich_valid": self.teich_valid,
            "teich_incomplete": self.teich_incomplete,
            "teich_invalid": self.teich_invalid,
            "drop_reasons": dict(self.drop_reasons.most_common()),
            "teich_errors": dict(self.teich_errors.most_common()),
            "tar_archives_with_multiple_jsonl": len(self.tar_warnings),
            "tar_skipped_jsonl_members": sum(
                len(w.get("skipped_members") or []) for w in self.tar_warnings
            ),
            "tar_warnings": self.tar_warnings,
        }


def _safe_filename(session_key: str) -> str:
    """session_key → 安全 trace 文件名 / Sanitize session key for trace filename.

    中文：替换 Windows 非法字符；超过 180 字符截断。

    English: Replaces path separators and illegal chars; truncates to 180 chars.
    """
    safe = session_key.replace(":", "_").replace("/", "_").replace("\\", "_")
    safe = safe.replace("<", "_").replace(">", "_").replace("|", "_")
    return safe[:180] if len(safe) > 180 else safe


def _safe_rel_path(rel: str) -> str:
    """净化 dropped/unwrapped 相对路径 / Sanitize relative path for sidecar files."""
    return rel.replace(":", "_").replace("|", "_")


def _write_jsonl(path: Path, record: dict[str, Any]) -> None:
    """写出单条 JSONL 记录（非 trace 格式）/ Write one JSON object as a JSONL line."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fp:
        fp.write(json.dumps(record, ensure_ascii=False))
        fp.write("\n")


def clean_output_subdirs(output_dir: Path) -> None:
    """清空标准输出子目录内容 / Remove files under standard output subdirectories.

    中文：清空 ``traces/``、``incomplete/``、``invalid/``、``dropped/``、``unwrapped/``
    内的文件与子目录，**不**删除目录本身。对应 ``--clean-output``。

    English: Clears contents of the five standard output subdirs before a rerun.
    """
    for name in ("traces", "incomplete", "invalid", "dropped", "unwrapped"):
        target = output_dir / name
        if target.is_dir():
            for child in target.iterdir():
                if child.is_file():
                    child.unlink()
                elif child.is_dir():
                    shutil.rmtree(child)


def _elapsed_str(seconds: float) -> str:
    """格式化耗时 / Format elapsed seconds for log output."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{int(minutes)}m{secs:.0f}s"
    hours, minutes = divmod(minutes, 60)
    return f"{int(hours)}h{int(minutes)}m"


def _progress_summary(stats: PipelineStats, *, elapsed: float) -> str:
    """扫描阶段进度摘要 / One-line scan progress summary for heartbeat logs."""
    rate = stats.scanned / elapsed if elapsed > 0 else 0.0
    return (
        f"scanned={stats.scanned} unwrapped={stats.unwrapped} "
        f"kept={stats.filter_kept} dropped={stats.filter_dropped} "
        f"parse_err={stats.parse_errors} dedup_drop={stats.dedup_dropped} "
        f"({rate:.1f} rec/s, elapsed {_elapsed_str(elapsed)})"
    )


def _top_drop_reasons(stats: PipelineStats, limit: int = 5) -> str:
    """Top-N 淘汰原因 / Format top drop reasons for log summary."""
    if not stats.drop_reasons:
        return "(none)"
    parts = [f"{reason}={count}" for reason, count in stats.drop_reasons.most_common(limit)]
    return ", ".join(parts)


def process(
    *,
    input_dir: Path,
    output_dir: Path,
    apply_filter: bool = True,
    dedup: bool = True,
    keep_unwrapped: bool = False,
    write_dropped: bool = True,
    emit_training_rows: bool = False,
    limit_files: int | None = None,
    limit_records: int | None = None,
    progress_every: int = 1000,
    clean_output: bool = False,
    skip_teich_validate: bool = False,
    log: Any = print,
) -> PipelineStats:
    """主流水线入口：扫描、筛选、去重、导出、校验 / Main delivery pipeline entry point.

    中文：执行完整 delivery → Teich Codex trace 流水线。

    dedup 行为对比：
    - ``dedup=True``（默认）：scan 阶段仅写入 ``dedup_winners`` 缓冲；scan 结束后
      才批量生成 events 并 export。同一 ``compute_session_id`` 保留 messages 最多的快照。
    - ``dedup=False``：每条 filter_kept 记录在 scan 阶段立即生成 events 并加入
      ``pending_exports``。

    English: Orchestrates scan, R1–R7 filter, session dedup, Codex export, and
    Teich validation.

    Args:
        input_dir: delivery 日志根目录 / Root directory with delivery logs.
        output_dir: 输出根目录；扫描时 exclude / Output root; excluded from input scan.
        apply_filter: 是否 R1–R7；False = ``--no-filter``。
        dedup: session 去重；False = ``--no-dedup``。
        keep_unwrapped: 写 ``unwrapped/`` 中间产物 / Write unwrapped JSONL sidecars.
        write_dropped: 写 ``dropped/``；False = ``--no-dropped``。
        emit_training_rows: 写 ``training_rows.jsonl`` / Emit teich training rows.
        limit_files: 最多 N 个输入文件（调试）/ Max source files to scan.
        limit_records: 最多 N 条记录（调试）/ Max records to scan.
        progress_every: 扫描心跳间隔；0=关 / Scan heartbeat interval; 0=off.
        clean_output: 跑批前清空五目录 / Clear output subdirs before run.
        skip_teich_validate: 跳过 teich；teich_valid 虚假递增 / Skip Teich validation.
        log: 日志 callable；None=静默 / Logger callable; None for silent mode.

    Returns:
        ``PipelineStats`` with all counters populated.
    """
    stats = PipelineStats()
    started = time.monotonic()
    scan_started = started
    current_source_file: str | None = None
    current_file_records = 0

    # === Phase 0: 初始化（目录、dedup 缓冲、日志）/ Init: dirs, buffers, logging ===
    if clean_output:
        clean_output_subdirs(output_dir)

    traces_dir = output_dir / "traces"
    incomplete_dir = output_dir / "incomplete"
    invalid_dir = output_dir / "invalid"
    unwrapped_dir = output_dir / "unwrapped"
    dropped_dir = output_dir / "dropped" if write_dropped else None

    if log:
        log("=== delivery_to_teich: scan phase ===")
        log(f"input:  {input_dir}")
        log(f"output: {output_dir}")
        log(f"excluding from input scan: {output_dir.resolve()}")
        log(
            "options: "
            f"filter={'on' if apply_filter else 'off'} "
            f"dedup={'on' if dedup else 'off'} "
            f"write_dropped={'on' if write_dropped else 'off'} "
            f"keep_unwrapped={'on' if keep_unwrapped else 'off'}"
        )
        limits = []
        if limit_files is not None:
            limits.append(f"limit_files={limit_files}")
        if limit_records is not None:
            limits.append(f"limit_records={limit_records}")
        if limits:
            log("limits: " + ", ".join(limits))
        if progress_every:
            log(f"progress heartbeat every {progress_every} records")
        if clean_output:
            log("options: clean_output=on (cleared traces/incomplete/invalid/dropped/unwrapped)")

    # dedup_winners[sid] = (msg_count, unwrapped, item, session_key)
    dedup_winners: dict[str, tuple[int, dict[str, Any], list[dict[str, Any]], str]] = {}
    pending_exports: list[tuple[dict[str, Any], list[dict[str, Any]], str, Path]] = []
    tar_warnings: list[dict[str, Any]] = []

    # === Phase 1: Scan loop（iter_delivery_records）/ Scan all delivery records ===
    for item in iter_delivery_records(
        input_dir,
        limit_files=limit_files,
        exclude_dirs=[output_dir],
        tar_warnings=tar_warnings,
    ):
        # --- 1a: 限流与文件切换日志 / Rate limits and per-file logging ---
        if limit_records is not None and stats.scanned >= limit_records:
            if log:
                log(f"reached --limit-records={limit_records}, stopping scan")
            break

        source_file = str(item.get("source_file") or "")
        if source_file != current_source_file:
            if log and current_source_file is not None:
                log(
                    f"finished file: {current_source_file} "
                    f"({current_file_records} records in this file)"
                )
            current_source_file = source_file
            current_file_records = 0
            if log and source_file:
                log(f"reading: {source_file}")

        stats.scanned += 1
        current_file_records += 1
        if progress_every and stats.scanned % progress_every == 0 and log:
            log(f"[scan] {_progress_summary(stats, elapsed=time.monotonic() - started)}")

        record = item.get("record")

        # --- 1b: parse / unwrap / Line-level parse and envelope unwrap ---
        if not isinstance(record, dict):
            stats.parse_errors += 1
            stats.json_line_errors += 1
            continue

        failure = classify_unwrap_failure(record)
        if failure is not None:
            stats.parse_errors += 1
            stats.unwrap_failures[failure] += 1
            continue

        unwrapped = unwrap_delivery_record(record)
        if unwrapped is None:
            stats.parse_errors += 1
            stats.unwrap_failures["unwrap_error"] += 1
            continue
        stats.unwrapped += 1

        if keep_unwrapped:
            rel = f"{_safe_rel_path(item['source_file'])}_{item['source_line']}.jsonl"
            _write_jsonl(unwrapped_dir / rel, unwrapped)

        # --- 1c: filter (R1–R7 evaluate) / Apply procurement filter rules ---
        trajectory = extract(unwrapped)
        passed, reasons = evaluate(trajectory)
        if apply_filter and not passed:
            stats.filter_dropped += 1
            stats.drop_reasons.update(reasons)
            if write_dropped and dropped_dir is not None:
                dropped = {**record, "_drop_reasons": reasons}
                rel = (
                    f"{_safe_rel_path(item['source_file'])}_{item['source_line']}.jsonl"
                )
                _write_jsonl(dropped_dir / rel, dropped)
            continue

        stats.filter_kept += 1
        session_key = session_key_from_record(record)
        msg_count = len(trajectory.get("messages") or [])

        # --- 1d: dedup 缓冲 vs 即时 export / Dedup buffer or immediate export ---
        if dedup:
            sid = compute_session_id(trajectory) or session_key
            prev = dedup_winners.get(sid)
            if prev is not None and prev[0] >= msg_count:
                # Shorter snapshot loses: same session_id, fewer messages.
                stats.dedup_dropped += 1
                stats.drop_reasons["session_id_duplicate"] += 1
                if write_dropped and dropped_dir is not None:
                    dropped = {**record, "_drop_reasons": ["session_id_duplicate"]}
                    rel = f"{_safe_rel_path(item['source_file'])}_{item['source_line']}.jsonl"
                    _write_jsonl(dropped_dir / rel, dropped)
                continue
            # Keep the longest-messages snapshot; full pass required before export.
            dedup_winners[sid] = (msg_count, unwrapped, item, session_key)
            continue

        # dedup=False: convert and queue export immediately during scan.
        events = openai_responses_to_codex_events(unwrapped)
        trace_name = _safe_filename(session_key) + ".jsonl"
        pending_exports.append(
            (unwrapped, events, session_key, traces_dir / trace_name)
        )

    if log and current_source_file is not None:
        log(
            f"finished file: {current_source_file} "
            f"({current_file_records} records in this file)"
        )

    # === Phase 1 收尾：tar_warnings、scan 汇总 / Scan phase wrap-up ===
    scan_elapsed = time.monotonic() - scan_started
    stats.tar_warnings = tar_warnings
    if log and tar_warnings:
        for warning in tar_warnings:
            skipped = warning.get("skipped_members") or []
            log(
                f"WARNING: tar {warning.get('source_file')} has "
                f"{warning.get('jsonl_member_count')} .jsonl members; "
                f"using {warning.get('used_member')!r}, skipping {len(skipped)}"
            )
    if log:
        log("=== delivery_to_teich: scan phase complete ===")
        log(f"[scan] {_progress_summary(stats, elapsed=scan_elapsed)}")
        log(f"[scan] top drop reasons: {_top_drop_reasons(stats)}")
        if dedup:
            log(
                f"[scan] dedup buffer: {len(dedup_winners)} unique sessions "
                f"from {stats.filter_kept} filter-kept records"
            )

    # === Phase 2: Dedup export build / Build pending_exports from dedup winners ===
    if dedup:
        if log:
            log(f"=== delivery_to_teich: building {len(dedup_winners)} trace exports ===")
        for _msg_count, unwrapped, item, session_key in dedup_winners.values():
            events = openai_responses_to_codex_events(unwrapped)
            trace_name = _safe_filename(session_key) + ".jsonl"
            pending_exports.append(
                (unwrapped, events, session_key, traces_dir / trace_name)
            )

    export_total = len(pending_exports)
    if log:
        log(f"=== delivery_to_teich: export phase ({export_total} traces) ===")

    # === Phase 3: Export + Teich validate / Write traces and validate ===
    export_started = time.monotonic()
    for export_idx, (unwrapped, events, session_key, trace_path) in enumerate(
        pending_exports, start=1
    ):
        # --- 3a: write_trace / Write Codex JSONL to traces/ ---
        write_trace(trace_path, events)
        if skip_teich_validate:
            # Debug mode: count as valid without actual Teich check.
            stats.teich_valid += 1
            continue

        validation = validate_trace_with_teich(trace_path, events)

        # --- 3b: valid / incomplete / invalid 分流 / Route by validation result ---
        if not validation.get("ok"):
            stats.teich_invalid += 1
            err = validation.get("error") or "unknown"
            stats.teich_errors[str(err)] += 1
            if log:
                log(f"[export] invalid {trace_path.name}: {err}")
            invalid_path = invalid_dir / trace_path.name
            write_trace(invalid_path, events)
            if trace_path.exists():
                trace_path.unlink()
        elif not validation.get("complete"):
            stats.teich_incomplete += 1
            if log:
                log(f"[export] incomplete {trace_path.name}: not trace_is_complete")
            incomplete_path = incomplete_dir / trace_path.name
            write_trace(incomplete_path, events)
            if trace_path.exists():
                trace_path.unlink()
        else:
            stats.teich_valid += 1

        if log and (
            export_idx == 1
            or export_idx == export_total
            or export_idx % max(1, min(50, export_total // 10 or 1)) == 0
        ):
            export_elapsed = time.monotonic() - export_started
            rate = export_idx / export_elapsed if export_elapsed > 0 else 0.0
            log(
                f"[export] {export_idx}/{export_total} "
                f"valid={stats.teich_valid} incomplete={stats.teich_incomplete} "
                f"invalid={stats.teich_invalid} ({rate:.1f} trace/s)"
            )

    # === Phase 4（可选）: training_rows / Optional teich training rows export ===
    if emit_training_rows and stats.teich_valid > 0:
        if log:
            log("=== delivery_to_teich: training rows export ===")
        try:
            from teich import convert_traces_to_training_data

            rows = convert_traces_to_training_data(traces_dir)
            training_path = output_dir / "training_rows.jsonl"
            with training_path.open("w", encoding="utf-8") as fp:
                for row in rows:
                    fp.write(json.dumps(row, ensure_ascii=False))
                    fp.write("\n")
            if log:
                log(f"[training] wrote {len(rows)} rows -> {training_path}")
        except ImportError as exc:
            if log:
                log(f"WARNING: skipped training rows export: {exc}")

    # === 收尾：summary 日志 / Final summary logging ===
    if log:
        elapsed = time.monotonic() - started
        log("=== delivery_to_teich: done ===")
        log(
            f"summary: scanned={stats.scanned} unwrapped={stats.unwrapped} "
            f"filter_kept={stats.filter_kept} filter_dropped={stats.filter_dropped} "
            f"dedup_dropped={stats.dedup_dropped} parse_errors={stats.parse_errors}"
        )
        log(
            f"summary: teich_valid={stats.teich_valid} "
            f"teich_incomplete={stats.teich_incomplete} teich_invalid={stats.teich_invalid}"
        )
        log(f"summary: top drop reasons: {_top_drop_reasons(stats)}")
        if stats.teich_errors:
            teich_err_parts = [
                f"{reason}={count}"
                for reason, count in stats.teich_errors.most_common(3)
            ]
            log(f"summary: teich errors: {', '.join(teich_err_parts)}")
        log(f"summary: total elapsed {_elapsed_str(elapsed)}")

    return stats


__all__ = [
    "PipelineStats",
    "check_teich_available",
    "clean_output_subdirs",
    "process",
]
