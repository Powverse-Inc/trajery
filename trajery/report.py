"""报表 Markdown 生成 / Markdown report generation for delivery_to_teich."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

DROP_REASON_DESCRIPTIONS: dict[str, str] = {
    "model_not_target_family": "模型不在 R1 目标范围（Claude Opus 4.5+ 或 GPT-5 系列）",
    "no_tools": "无工具定义（R2）",
    "messages_le_5": "messages 数量 ≤ 5（R3）",
    "no_assistant_tool_call": "无 assistant 工具调用（R4）",
    "stop_reason_missing": "缺少 stop_reason（R5）",
    "stop_reason_tool_calls": "响应末项停在 function_call，Agent 尚未继续（R5）",
    "stop_reason_length": "因 max_output_tokens 截断（R5）",
    "stop_reason_content_filter": "因 content_filter 中断（R5）",
    "stop_reason_end_turn": "stop_reason 不在接受列表（R5）",
    "stop_reason_stop": "stop_reason 不在接受列表（R5）",
    "no_system_prompt": "无 system / instructions（R6）",
    "silent_keepalive_marker": "含 [SILENT] / [SLIENT] 心跳标记（R7）",
    "heartbeat_non_task_traffic": "OpenClaw / 心跳探活流量（R7）",
    "session_id_duplicate": "同 session 较短快照被去重淘汰",
}


def _describe_drop_reason(reason: str) -> str:
    if reason in DROP_REASON_DESCRIPTIONS:
        return DROP_REASON_DESCRIPTIONS[reason]
    if reason.startswith("stop_reason_"):
        return f"stop_reason 不符合 R5 要求（{reason}）"
    return "—"


def _pct(count: int, total: int) -> str:
    if total <= 0:
        return "—"
    return f"{100.0 * count / total:.1f}%"


def _fmt_cell(value: Any) -> str:
    if value is None:
        return "—"
    text = str(value)
    return text.replace("|", "\\|")


def _elapsed_display(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{int(minutes)}m{secs:.0f}s"
    hours, minutes = divmod(minutes, 60)
    return f"{int(hours)}h{int(minutes)}m"


def to_markdown_report(
    report: dict[str, Any],
    *,
    generated_at: datetime | None = None,
) -> str:
    """从 ``to_report()`` 字典生成人类可读的 Markdown 报表。"""
    when = generated_at or datetime.now(timezone.utc)
    lines: list[str] = []

    lines.append("# delivery_to_teich Report")
    lines.append("")
    lines.append("## 运行信息")
    lines.append("")
    lines.append(f"- **输入目录**: `{report.get('input_dir', '')}`")
    lines.append(f"- **输出目录**: `{report.get('output_dir', '')}`")
    lines.append(f"- **生成时间 (UTC)**: {when.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(
        f"- **总耗时**: {_elapsed_display(report.get('elapsed_seconds'))}"
    )
    lines.append("")

    teich_valid = int(report.get("teich_valid") or 0)
    parse_errors = int(report.get("parse_errors") or 0)
    lines.append("## 验收摘要")
    lines.append("")
    parse_ok = "x" if parse_errors == 0 else " "
    valid_ok = "x" if teich_valid > 0 else " "
    lines.append(
        f"- [{parse_ok}] `parse_errors == 0`（当前: {parse_errors}）"
    )
    lines.append(
        f"- [{valid_ok}] `teich_valid > 0`（当前: {teich_valid}）"
    )
    lines.append(
        f"- [{valid_ok}] 最终可用 trace 在 `traces/`（{teich_valid} 条）"
    )
    lines.append("")

    scanned = int(report.get("scanned") or 0)
    unwrapped = int(report.get("unwrapped") or 0)
    filter_kept = int(report.get("filter_kept") or 0)
    filter_dropped = int(report.get("filter_dropped") or 0)
    dedup_dropped = int(report.get("dedup_dropped") or 0)
    export_total = int(report.get("export_total") or 0)
    teich_incomplete = int(report.get("teich_incomplete") or 0)
    teich_invalid = int(report.get("teich_invalid") or 0)
    funnel = report.get("funnel") or {}

    lines.append("## 漏斗")
    lines.append("")
    lines.append("| 阶段 | 数量 | 占 scanned |")
    lines.append("|------|------|------------|")
    rows = [
        ("scanned", scanned),
        ("unwrapped", unwrapped),
        ("filter_kept", filter_kept),
        ("filter_dropped", filter_dropped),
        ("dedup_dropped", dedup_dropped),
        ("export_total", export_total),
        ("teich_valid → traces/", teich_valid),
        ("teich_incomplete → incomplete/", teich_incomplete),
        ("teich_invalid → invalid/", teich_invalid),
    ]
    for label, count in rows:
        lines.append(f"| {label} | {count} | {_pct(count, scanned)} |")
    lines.append("")

    if export_total:
        lines.append("### Teich 校验转化率（相对 export_total）")
        lines.append("")
        lines.append("| 结果 | 数量 | 占比 |")
        lines.append("|------|------|------|")
        for key, count, label in (
            ("teich_valid_rate", teich_valid, "valid"),
            ("teich_incomplete_rate", teich_incomplete, "incomplete"),
            ("teich_invalid_rate", teich_invalid, "invalid"),
        ):
            rate = funnel.get(key)
            if isinstance(rate, (int, float)):
                rate_str = f"{100.0 * rate:.1f}%"
            else:
                rate_str = _pct(count, export_total)
            lines.append(f"| {label} | {count} | {rate_str} |")
        lines.append("")

    drop_reasons: dict[str, int] = report.get("drop_reasons") or {}
    lines.append("## 淘汰原因 (drop_reasons)")
    lines.append("")
    if drop_reasons:
        lines.append("| 原因 | 数量 | 说明 |")
        lines.append("|------|------|------|")
        for reason, count in drop_reasons.items():
            desc = _describe_drop_reason(reason)
            lines.append(
                f"| `{_fmt_cell(reason)}` | {count} | {desc} |"
            )
    else:
        lines.append("（无）")
    lines.append("")

    lines.append("## Teich 校验")
    lines.append("")
    lines.append("### 判定标准")
    lines.append("")
    lines.append(
        "- **invalid**：Teich 无法将 trace 转为 training example"
        "（`ok=False`）"
    )
    lines.append(
        "- **incomplete**：转换成功，但 `trace_is_complete=False`——"
        "末条 assistant/model/tool 消息的 role 为 `tool`，"
        "表示对话在工具返回后尚未有最终 assistant 回复"
    )
    lines.append(
        "- **valid**：转换成功且完整，保留在 `traces/`（验收优先看此数量）"
    )
    lines.append("")
    lines.append(
        "R5 筛选检查单条 response 的 stop_reason；"
        "Teich 完整性检查整段转换后 messages 的末条 relevant role。"
        "两者是不同层级的标准，故能通过 R5 的记录仍可能被判 incomplete。"
    )
    lines.append("")

    teich_errors: dict[str, int] = report.get("teich_errors") or {}
    if teich_errors:
        lines.append("### Teich 错误聚合 (teich_errors)")
        lines.append("")
        lines.append("| 错误 | 数量 |")
        lines.append("|------|------|")
        for err, count in teich_errors.items():
            lines.append(f"| `{_fmt_cell(err)}` | {count} |")
        lines.append("")

    trace_results: list[dict[str, Any]] = report.get("teich_trace_results") or []
    invalid_rows = [
        r for r in trace_results if r.get("status") == "invalid"
    ]
    incomplete_rows = [
        r for r in trace_results if r.get("status") == "incomplete"
    ]
    valid_omitted = report.get("valid_files_omitted")

    lines.append(f"### invalid（{teich_invalid}）")
    lines.append("")
    if invalid_rows:
        lines.append("| 文件 | session_key | error | trace_type |")
        lines.append("|------|-------------|-------|------------|")
        for row in invalid_rows:
            lines.append(
                "| "
                + " | ".join(
                    _fmt_cell(row.get(k))
                    for k in ("filename", "session_key", "error", "trace_type")
                )
                + " |"
            )
    else:
        lines.append("（无）")
    lines.append("")

    lines.append(f"### incomplete（{teich_incomplete}）")
    lines.append("")
    if incomplete_rows:
        lines.append(
            "| 文件 | session_key | messages | tools | last_role | error |"
        )
        lines.append(
            "|------|-------------|----------|-------|-----------|-------|"
        )
        for row in incomplete_rows:
            lines.append(
                "| "
                + " | ".join(
                    [
                        _fmt_cell(row.get("filename")),
                        _fmt_cell(row.get("session_key")),
                        _fmt_cell(row.get("messages_count")),
                        _fmt_cell(row.get("tools_count")),
                        _fmt_cell(row.get("last_relevant_role")),
                        _fmt_cell(row.get("error")),
                    ]
                )
                + " |"
            )
    else:
        lines.append("（无）")
    lines.append("")

    if valid_omitted:
        lines.append(
            f"> valid trace 共 {valid_omitted} 条，未列入上表。"
            "使用 `--report-include-valid` 可写入 `report.json`。"
        )
        lines.append("")

    tar_warnings: list[dict[str, Any]] = report.get("tar_warnings") or []
    if tar_warnings:
        lines.append("## tar 告警")
        lines.append("")
        for warning in tar_warnings:
            skipped = warning.get("skipped_members") or []
            lines.append(
                f"- `{warning.get('source_file')}`: "
                f"{warning.get('jsonl_member_count')} 个 .jsonl 成员，"
                f"使用 `{warning.get('used_member')}`，跳过 {len(skipped)} 个"
            )
            if skipped:
                lines.append(f"  - 跳过: {', '.join(skipped)}")
        lines.append("")

    unwrap_failures: dict[str, int] = report.get("unwrap_failures") or {}
    if unwrap_failures:
        lines.append("## 解包失败 (unwrap_failures)")
        lines.append("")
        lines.append("| 失败码 | 数量 |")
        lines.append("|--------|------|")
        for code, count in unwrap_failures.items():
            lines.append(f"| `{_fmt_cell(code)}` | {count} |")
        lines.append("")

    lines.append("## 附录")
    lines.append("")
    lines.append("详见 [USER_GUIDE.md](../USER_GUIDE.md) §6–§10。")
    lines.append("")

    return "\n".join(lines)


__all__ = ["DROP_REASON_DESCRIPTIONS", "to_markdown_report"]
