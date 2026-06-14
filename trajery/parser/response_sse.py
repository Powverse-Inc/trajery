"""Parse delivery-log ``response`` strings into OpenAI Responses API objects."""

from __future__ import annotations

import json
from typing import Any


def _normalize_response_text(text: str) -> str:
    text = text.strip()
    while text.startswith(":"):
        sep = text.find("\n\n")
        if sep == -1:
            return text.lstrip(":")
        text = text[sep + 2 :].lstrip()
    return text


def _parse_sse_blocks(text: str) -> list[tuple[str | None, dict[str, Any] | None]]:
    blocks: list[tuple[str | None, dict[str, Any] | None]] = []
    for block in text.split("\n\n"):
        event: str | None = None
        data: dict[str, Any] | None = None
        for line in block.split("\n"):
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                raw = line.split(":", 1)[1].strip()
                if raw == "[DONE]":
                    continue
                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError:
                    parsed = None
                if isinstance(parsed, dict):
                    data = parsed
        if event is not None or data is not None:
            blocks.append((event, data))
    return blocks


def _message_content_blocks(message: dict[str, Any]) -> list[dict[str, Any]]:
    content = message.get("content")
    if isinstance(content, str) and content:
        return [{"type": "output_text", "text": content}]
    if isinstance(content, list):
        blocks: list[dict[str, Any]] = []
        for block in content:
            if isinstance(block, dict):
                blocks.append(block)
        return blocks

    output: list[dict[str, Any]] = []
    if message.get("reasoning_content"):
        output.append(
            {
                "type": "reasoning",
                "summary": [{"type": "summary_text", "text": message["reasoning_content"]}],
            }
        )
    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list):
        for tc in tool_calls:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
            output.append(
                {
                    "type": "function_call",
                    "name": fn.get("name", ""),
                    "call_id": tc.get("id", ""),
                    "arguments": fn.get("arguments", ""),
                }
            )
    return output


def _chat_completion_to_response(payload: dict[str, Any]) -> dict[str, Any]:
    choices = payload.get("choices") or []
    message = choices[0].get("message") if choices and isinstance(choices[0], dict) else {}
    if not isinstance(message, dict):
        message = {}
    output = _message_content_blocks(message)
    finish = choices[0].get("finish_reason") if choices and isinstance(choices[0], dict) else None
    status = "completed"
    if finish == "length":
        status = "incomplete"
    return {
        "object": "response",
        "id": payload.get("id"),
        "model": payload.get("model"),
        "status": status,
        "output": output,
        "usage": payload.get("usage"),
        "_finish_reason": finish,
    }


def _aggregate_completion_chunks(chunks: list[dict[str, Any]]) -> dict[str, Any]:
    reasoning_parts: list[str] = []
    content_parts: list[str] = []
    tool_calls: dict[int, dict[str, Any]] = {}
    finish_reason: str | None = None
    model: str | None = None
    resp_id: str | None = None
    usage: dict[str, Any] | None = None

    for chunk in chunks:
        if chunk.get("object") != "chat.completion.chunk":
            continue
        model = chunk.get("model") or model
        resp_id = chunk.get("id") or resp_id
        usage = chunk.get("usage") or usage
        for choice in chunk.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                continue
            if delta.get("reasoning_content"):
                reasoning_parts.append(str(delta["reasoning_content"]))
            if delta.get("content"):
                content_parts.append(str(delta["content"]))
            for tc in delta.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                idx = int(tc.get("index", 0))
                slot = tool_calls.setdefault(
                    idx,
                    {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
                )
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function")
                if isinstance(fn, dict):
                    if fn.get("name"):
                        slot["function"]["name"] += str(fn["name"])
                    if fn.get("arguments") is not None:
                        slot["function"]["arguments"] += str(fn["arguments"])

    output: list[dict[str, Any]] = []
    if reasoning_parts:
        output.append(
            {
                "type": "reasoning",
                "summary": [{"type": "summary_text", "text": "".join(reasoning_parts)}],
            }
        )
    if content_parts:
        output.append(
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "".join(content_parts)}],
            }
        )
    for idx in sorted(tool_calls):
        tc = tool_calls[idx]
        fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
        output.append(
            {
                "type": "function_call",
                "name": fn.get("name", ""),
                "call_id": tc.get("id", ""),
                "arguments": fn.get("arguments", ""),
            }
        )

    status = "completed"
    if finish_reason == "length":
        status = "incomplete"
    return {
        "object": "response",
        "id": resp_id,
        "model": model,
        "status": status,
        "output": output,
        "usage": usage,
        "_finish_reason": finish_reason,
    }


def _responses_event_stream_to_response(blocks: list[tuple[str | None, dict[str, Any] | None]]) -> dict[str, Any]:
    output_items: list[dict[str, Any]] = []
    completed: dict[str, Any] | None = None
    model: str | None = None
    resp_id: str | None = None

    for event, data in blocks:
        if not isinstance(data, dict):
            continue
        event_type = data.get("type") or event
        if event_type == "response.output_item.done":
            item = data.get("item") or data.get("output_item")
            if isinstance(item, dict):
                output_items.append(item)
        elif event_type == "response.completed":
            response = data.get("response")
            if isinstance(response, dict):
                completed = response
                model = response.get("model") or model
                resp_id = response.get("id") or resp_id
                resp_output = response.get("output")
                if isinstance(resp_output, list) and resp_output and not output_items:
                    output_items = [item for item in resp_output if isinstance(item, dict)]
        elif event_type == "response.created":
            response = data.get("response")
            if isinstance(response, dict):
                model = response.get("model") or model
                resp_id = response.get("id") or resp_id

    if completed is None:
        completed = {"object": "response", "status": "completed", "output": output_items}

    result = {
        "object": "response",
        "id": resp_id or completed.get("id"),
        "model": model or completed.get("model"),
        "status": completed.get("status", "completed"),
        "output": output_items or completed.get("output") or [],
        "usage": completed.get("usage"),
        "incomplete_details": completed.get("incomplete_details"),
    }
    if output_items and isinstance(result["output"], list):
        last = output_items[-1]
        if isinstance(last, dict) and last.get("type") in ("function_call", "custom_tool_call"):
            result["_finish_reason"] = "tool_calls"
    return result


def _data_only_stream_to_response(text: str) -> dict[str, Any]:
    chunks: list[dict[str, Any]] = []
    for block in text.split("\n\n"):
        for line in block.split("\n"):
            if not line.startswith("data:"):
                continue
            raw = line.split(":", 1)[1].strip()
            if raw == "[DONE]":
                continue
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                chunks.append(parsed)
    if not chunks:
        return {"object": "response", "status": "completed", "output": []}
    if chunks[0].get("object") == "chat.completion.chunk":
        return _aggregate_completion_chunks(chunks)
    if chunks[0].get("object") == "response":
        last = chunks[-1]
        response = last.get("response") if isinstance(last.get("response"), dict) else last
        if isinstance(response, dict):
            return response
    return {"object": "response", "status": "completed", "output": []}


def parse_delivery_response(text: str | None) -> dict[str, Any] | None:
    """Return a Responses API ``response`` dict, or ``None`` when parsing fails."""
    if not text or not str(text).strip():
        return None

    normalized = _normalize_response_text(str(text))
    if not normalized:
        return None

    if normalized.startswith("{"):
        try:
            payload = json.loads(normalized)
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        obj = payload.get("object")
        if obj == "response":
            return payload
        if obj == "response.compaction":
            return {"object": "response", "status": "completed", "output": payload.get("output") or []}
        if obj == "chat.completion":
            return _chat_completion_to_response(payload)
        if obj == "chat.completion.chunk":
            return _aggregate_completion_chunks([payload])
        return None

    blocks = _parse_sse_blocks(normalized)
    if not blocks:
        if normalized.lstrip().startswith("data:"):
            return _data_only_stream_to_response(normalized)
        return None

    has_responses_events = any(
        isinstance(data, dict)
        and (
            str(data.get("type") or "").startswith("response.")
            or event in {"response.created", "response.completed", "response.output_item.done"}
        )
        for event, data in blocks
    )
    if has_responses_events:
        return _responses_event_stream_to_response(blocks)

    chunks = [data for _, data in blocks if isinstance(data, dict)]
    if chunks and chunks[0].get("object") == "chat.completion.chunk":
        return _aggregate_completion_chunks(chunks)

    return _data_only_stream_to_response(normalized)
