"""将 openai_responses 记录导出为 Teich 兼容的 Codex trace JSONL。
Export openai_responses records as Teich-compatible Codex trace JSONL.

中文：
- 本模块位于 export 层，读取 L1 openai_responses（unwrapped 记录），输出 L3 Codex trace
  事件列表，供 ``pipeline.process`` 写入 ``traces/`` 并经 Teich 校验。
- Codex 事件类型：
  - ``session_meta``: 固定首事件（id, cwd, model, base_instructions）
  - ``turn_context``: 固定第二事件（cwd, model）
  - ``response_item``: 来自 input[] 与 response.output[] 的消息/工具/reasoning
  - ``event_msg``: 有 token 计数时，type=``token_count``
- 已知限制：所有 ``timestamp`` 使用运行时 UTC（``_utc_now_iso()``），非 delivery 原始时间
  （见 MAINTAINER、USER_GUIDE §5）。

English:
- Export layer: L1 openai_responses → L3 Codex trace events (JSONL, one event per line).
- Teich validation entry points: ``check_teich_available``, ``validate_trace_with_teich``.
- Timestamps are export-time UTC, not original delivery timestamps.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utc_now_iso() -> str:
    """生成 ISO8601 UTC 时间戳（毫秒）/ Export-time UTC timestamp with milliseconds.

    中文：用于所有 Codex 事件的 ``timestamp``；**非** delivery 原始时间。

    English: Used for all event timestamps at export time; not source log time.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _extract_cwd_from_text(text: str) -> str | None:
    """从文本提取 ``<cwd>...</cwd>`` 标签 / Extract working directory from XML-like tag.

    中文：扫描 user/developer message 中的 ``<cwd>path</cwd>`` 标记。

    English: Regex extraction of ``<cwd>...</cwd>`` from message text.
    """
    match = re.search(r"<cwd>\s*([^<]+?)\s*</cwd>", text, flags=re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return None


def _guess_cwd(record: dict[str, Any]) -> str:
    """推断 session 工作目录 / Guess session working directory from input messages.

    中文：扫描 ``record.input`` 中所有 message content；未找到则默认 ``/workspace``。

    English: Walks input items for ``<cwd>`` tags; defaults to ``/workspace``.
    """
    inp = record.get("input")
    if isinstance(inp, list):
        for item in inp:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if isinstance(content, str):
                cwd = _extract_cwd_from_text(content)
                if cwd:
                    return cwd
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        text = block.get("text")
                        if isinstance(text, str):
                            cwd = _extract_cwd_from_text(text)
                            if cwd:
                                return cwd
    return "/workspace"


def _normalize_message_content(content: Any, *, role: str) -> list[dict[str, Any]]:
    """归一化 message content 为 Codex content 块 / Normalize message content blocks.

    中文：``user``/``developer`` role → ``input_text``；其他 role → ``output_text``；
    支持 str、list[str]、list[dict] 形态。

    English: Maps roles to input_text vs output_text block types.
    """
    if isinstance(content, str):
        block_type = "input_text" if role in {"user", "developer"} else "output_text"
        return [{"type": block_type, "text": content}]
    if isinstance(content, list):
        blocks: list[dict[str, Any]] = []
        for block in content:
            if isinstance(block, str):
                block_type = "input_text" if role in {"user", "developer"} else "output_text"
                blocks.append({"type": block_type, "text": block})
            elif isinstance(block, dict):
                if "type" in block:
                    blocks.append(block)
                elif "text" in block:
                    block_type = "input_text" if role in {"user", "developer"} else "output_text"
                    blocks.append({"type": block_type, "text": block.get("text", "")})
        return blocks
    return []


def _input_item_to_payload(item: dict[str, Any]) -> dict[str, Any] | None:
    """input[] item → Codex response_item payload / Map input item to trace payload.

    中文：类型映射表：
    - ``message`` → ``message``（content 经 ``_normalize_message_content``）
    - ``function_call`` / ``custom_tool_call`` → 同名（call_id 取自 call_id 或 id）
    - ``function_call_output`` / ``custom_tool_call_output`` → 同名
    - ``reasoning`` → 原样
    - 仅有 role 无 type → ``message``（legacy 形态）

    English: Converts Responses API input items to Codex response_item payloads.

    Args:
        item: One element from ``record.input`` list.

    Returns:
        Payload dict, or ``None`` if item type is unsupported.
    """
    itype = item.get("type")
    if itype == "message":
        role = item.get("role")
        if not isinstance(role, str):
            return None
        payload: dict[str, Any] = {
            "type": "message",
            "role": role,
            "content": _normalize_message_content(item.get("content"), role=role),
        }
        if item.get("phase"):
            payload["phase"] = item["phase"]
        return payload
    if itype in {"function_call", "custom_tool_call"}:
        return {
            "type": itype,
            "name": item.get("name", ""),
            "call_id": item.get("call_id") or item.get("id") or "",
            "arguments": item.get("arguments", ""),
        }
    if itype in {"function_call_output", "custom_tool_call_output"}:
        return {
            "type": itype,
            "call_id": item.get("call_id") or item.get("id") or "",
            "output": item.get("output", ""),
        }
    if itype == "reasoning":
        return item
    # Legacy: role present but no explicit type field.
    if isinstance(item.get("role"), str) and itype is None:
        role = item["role"]
        return {
            "type": "message",
            "role": role,
            "content": _normalize_message_content(item.get("content"), role=role),
        }
    return None


def _output_item_key(item: dict[str, Any]) -> str:
    """生成 response_item 去重键 / Dedup key for response_item events.

    中文：input 与 response.output 可能含重复 item；用此键在 ``seen_keys`` 中跳过。
    键格式：
    - function_call: ``{type}:{call_id or name}``
    - function_call_output: ``{type}:{call_id}``
    - message: ``message:{role}:{text[:120]}``
    - reasoning: ``reasoning:{text[:120]}``
    - 其他: ``json.dumps(sort_keys=True)``

    English: Prevents duplicate response_item events when input/output overlap.
    """
    itype = item.get("type", "")
    if itype in {"function_call", "custom_tool_call"}:
        return f"{itype}:{item.get('call_id') or item.get('name')}"
    if itype in {"function_call_output", "custom_tool_call_output"}:
        return f"{itype}:{item.get('call_id')}"
    if itype == "message":
        role = item.get("role", "assistant")
        content = item.get("content")
        if isinstance(content, list):
            text = " ".join(
                str(block.get("text", ""))
                for block in content
                if isinstance(block, dict)
            )
        else:
            text = str(content or "")
        return f"message:{role}:{text[:120]}"
    if itype == "reasoning":
        summary = item.get("summary")
        if isinstance(summary, list):
            text = " ".join(
                str(block.get("text", ""))
                for block in summary
                if isinstance(block, dict)
            )
        else:
            text = str(summary or "")
        return f"reasoning:{text[:120]}"
    return json.dumps(item, sort_keys=True, ensure_ascii=False)


def _responses_output_to_payload(item: dict[str, Any]) -> dict[str, Any]:
    """response.output[] item → Codex payload / Map response output item to trace payload.

    中文：output 侧映射；``custom_tool_call`` 统一输出 type 为 ``function_call``。

    English: Maps Responses API output items; normalizes custom_tool_call to function_call.
    """
    itype = item.get("type")
    if itype == "message":
        role = item.get("role", "assistant")
        return {
            "type": "message",
            "role": role,
            "content": _normalize_message_content(item.get("content"), role=role),
            **({"phase": item["phase"]} if item.get("phase") else {}),
        }
    if itype in {"function_call", "custom_tool_call"}:
        args = item.get("arguments", "")
        return {
            "type": "function_call",
            "name": item.get("name", ""),
            "call_id": item.get("call_id") or item.get("id") or "",
            "arguments": args,
        }
    if itype == "reasoning":
        return item
    return item


def openai_responses_to_codex_events(record: dict[str, Any]) -> list[dict[str, Any]]:
    """L1 openai_responses → L3 Codex trace 事件列表 / Convert record to Codex events.

    中文：事件顺序固定：
    1. ``session_meta`` — session 元数据
    2. ``turn_context`` — 本轮上下文
    3. ``response_item`` × N — 来自 ``input[]``  then ``response.output[]``（``seen_keys`` 去重）
    4. ``event_msg`` (token_count) — 可选，当 ``_provenance`` 含 token 计数

    English: Builds ordered Codex trace events from an unwrapped openai_responses record.

    Args:
        record: L1 unwrapped openai_responses dict (from ``unwrap_delivery_record``).

    Returns:
        List of Codex trace event dicts (not yet written to disk).
    """
    provenance = record.get("_provenance")
    if not isinstance(provenance, dict):
        provenance = {}

    session_id = provenance.get("request_id") or provenance.get("response_id") or "unknown-session"
    model = record.get("model") or "gpt-5.5"
    cwd = _guess_cwd(record)
    instructions = record.get("instructions") or record.get("system") or ""

    events: list[dict[str, Any]] = []
    ts = _utc_now_iso()

    # Event 1: session_meta — required first event for Codex trace format.
    events.append(
        {
            "timestamp": ts,
            "type": "session_meta",
            "payload": {
                "id": session_id,
                "timestamp": ts,
                "cwd": cwd,
                "originator": "codex_exec",
                "source": "exec",
                "model_provider": "openai",
                "model": model,
                "base_instructions": {"text": instructions},
            },
        }
    )

    # Event 2: turn_context — per-turn cwd/model snapshot.
    events.append(
        {
            "timestamp": ts,
            "type": "turn_context",
            "payload": {
                "cwd": cwd,
                "model": model,
            },
        }
    )

    # Events 3+: response_item from input[] then response.output[] (deduped).
    seen_keys: set[str] = set()
    inp = record.get("input")
    if isinstance(inp, list):
        for item in inp:
            if not isinstance(item, dict):
                continue
            payload = _input_item_to_payload(item)
            if payload is None:
                continue
            key = _output_item_key(payload)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            events.append({"timestamp": ts, "type": "response_item", "payload": payload})

    response = record.get("response")
    if isinstance(response, dict):
        output = response.get("output")
        if isinstance(output, list):
            for item in output:
                if not isinstance(item, dict):
                    continue
                payload = _responses_output_to_payload(item)
                key = _output_item_key(payload)
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                events.append({"timestamp": ts, "type": "response_item", "payload": payload})

    # Optional: token_count event from delivery provenance.
    input_tokens = provenance.get("input_tokens")
    output_tokens = provenance.get("output_tokens")
    if input_tokens is not None or output_tokens is not None:
        events.append(
            {
                "timestamp": ts,
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "input_tokens": int(input_tokens or 0),
                            "output_tokens": int(output_tokens or 0),
                            "total_tokens": int(input_tokens or 0) + int(output_tokens or 0),
                        }
                    },
                },
            }
        )

    return events


def write_trace(path: Path, events: list[dict[str, Any]]) -> None:
    """写出 Codex trace JSONL / Write Codex trace events as JSONL.

    中文：每行一个 JSON 事件对象；自动创建父目录。

    English: One JSON event per line; creates parent directories as needed.

    Args:
        path: Destination ``.jsonl`` file path.
        events: List of Codex trace event dicts.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fp:
        for event in events:
            fp.write(json.dumps(event, ensure_ascii=False))
            fp.write("\n")


def check_teich_available() -> tuple[bool, str | None]:
    """探测 teich 是否可 import / Probe whether teich is importable.

    中文：CLI 启动时 fail-fast 检查；未安装时返回 ``(False, error_message)``。

    English: Used by CLI for early exit (code 3) when teich is missing.

    Returns:
        ``(True, None)`` if teich imports successfully, else ``(False, error)``.
    """
    try:
        import teich  # noqa: F401
    except ImportError as exc:
        return False, str(exc)
    return True, None


def _last_relevant_message_role(row: dict[str, Any]) -> str | None:
    """Return the last assistant/model/tool role in a training row, if any."""
    messages = row.get("messages") if isinstance(row, dict) else None
    if not isinstance(messages, list):
        return None
    for message in reversed(messages):
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role in {"assistant", "model", "tool"}:
            return str(role)
    return None


def validate_trace_with_teich(trace_path: Path, events: list[dict[str, Any]]) -> dict[str, Any]:
    """用 Teich API 校验 trace / Validate a trace using teich APIs.

    中文：三步校验 — ``detect_trace_type`` → ``convert_trace_to_training_example`` →
    ``trace_is_complete``。返回 dict schema：
    - ``ok`` (bool): 能否成功转为 training example
    - ``trace_type`` (str|None): 期望 ``codex``
    - ``complete`` (bool): ``trace_is_complete(row)``
    - ``error`` (str|None): 失败原因
    - ``messages_count`` / ``tools_count`` (int): 成功时诊断字段
    - ``last_relevant_role`` (str|None): 末条 assistant/model/tool 消息的 role

    pipeline 根据 ``ok``/``complete`` 决定文件留在 ``traces/`` 或移至
    ``incomplete/``/``invalid/``。

    English: Runs teich type detection, conversion, and completeness check.

    Args:
        trace_path: Path to written trace JSONL (used by teich converter).
        events: In-memory event list (used for type detection).

    Returns:
        Result dict with keys ``ok``, ``trace_type``, ``complete``, ``error``,
        and optional ``messages_count``, ``tools_count``, ``last_relevant_role``.
    """
    try:
        from teich import convert_trace_to_training_example, detect_trace_type, trace_is_complete
    except ImportError as exc:
        return {
            "ok": False,
            "trace_type": None,
            "complete": False,
            "error": f"teich not installed: {exc}",
            "last_relevant_role": None,
        }

    trace_type = detect_trace_type(events)
    if trace_type != "codex":
        return {
            "ok": False,
            "trace_type": trace_type,
            "complete": False,
            "error": f"unexpected trace_type: {trace_type}",
            "last_relevant_role": None,
        }

    try:
        example = convert_trace_to_training_example(trace_path)
        row = example.to_dict()
    except Exception as exc:
        return {
            "ok": False,
            "trace_type": trace_type,
            "complete": False,
            "error": str(exc),
            "last_relevant_role": None,
        }

    complete = trace_is_complete(row)
    return {
        "ok": True,
        "trace_type": trace_type,
        "complete": complete,
        "error": None if complete else "trace_is_complete=False",
        "messages_count": len(row.get("messages") or []),
        "tools_count": len(row.get("tools") or []),
        "last_relevant_role": _last_relevant_message_role(row),
    }
