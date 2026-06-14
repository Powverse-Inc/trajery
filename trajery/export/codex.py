"""Export openai_responses records as Teich-compatible Codex trace JSONL."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _extract_cwd_from_text(text: str) -> str | None:
    match = re.search(r"<cwd>\s*([^<]+?)\s*</cwd>", text, flags=re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return None


def _guess_cwd(record: dict[str, Any]) -> str:
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
    if isinstance(item.get("role"), str) and itype is None:
        role = item["role"]
        return {
            "type": "message",
            "role": role,
            "content": _normalize_message_content(item.get("content"), role=role),
        }
    return None


def _output_item_key(item: dict[str, Any]) -> str:
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
    """Convert an unwrapped openai_responses record into Codex trace events."""
    provenance = record.get("_provenance")
    if not isinstance(provenance, dict):
        provenance = {}

    session_id = provenance.get("request_id") or provenance.get("response_id") or "unknown-session"
    model = record.get("model") or "gpt-5.5"
    cwd = _guess_cwd(record)
    instructions = record.get("instructions") or record.get("system") or ""

    events: list[dict[str, Any]] = []
    ts = _utc_now_iso()

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
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fp:
        for event in events:
            fp.write(json.dumps(event, ensure_ascii=False))
            fp.write("\n")


def check_teich_available() -> tuple[bool, str | None]:
    """Return ``(available, error_message)`` for the optional teich dependency."""
    try:
        import teich  # noqa: F401
    except ImportError as exc:
        return False, str(exc)
    return True, None


def validate_trace_with_teich(trace_path: Path, events: list[dict[str, Any]]) -> dict[str, Any]:
    """Validate a trace using teich APIs. Returns a result dict."""
    try:
        from teich import convert_trace_to_training_example, detect_trace_type, trace_is_complete
    except ImportError as exc:
        return {
            "ok": False,
            "trace_type": None,
            "complete": False,
            "error": f"teich not installed: {exc}",
        }

    trace_type = detect_trace_type(events)
    if trace_type != "codex":
        return {
            "ok": False,
            "trace_type": trace_type,
            "complete": False,
            "error": f"unexpected trace_type: {trace_type}",
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
        }

    complete = trace_is_complete(row)
    return {
        "ok": True,
        "trace_type": trace_type,
        "complete": complete,
        "error": None if complete else "trace_is_complete=False",
        "messages_count": len(row.get("messages") or []),
        "tools_count": len(row.get("tools") or []),
    }
