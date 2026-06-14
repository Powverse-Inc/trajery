"""将 delivery 的 response 字段解析为 OpenAI Responses API 对象。
Parse delivery-log ``response`` strings into OpenAI Responses API objects.

中文：
- 本模块位于 parser 层，读取 L0 delivery 信封中的 ``response`` 字符串（``str``），
  输出 L1 统一的 ``{"object":"response", "output":[...], ...}`` 形态，供
  ``unwrap_delivery_record`` 写入 unwrapped 记录。
- delivery ``response`` 常见五种输入形态：
  1. 纯 JSON ``{"object":"response", ...}``
  2. 纯 JSON ``{"object":"chat.completion", ...}``
  3. 单 chunk JSON ``{"object":"chat.completion.chunk", ...}``
  4. SSE 含 ``response.*`` 事件（Responses API streaming）
  5. SSE 仅 ``data:`` 行（无 ``event:``，可能是 chunk 流或 response 流）
- 下游：``delivery.unwrap_delivery_record``；R5 规则以 ``extract()`` 结果为准
  （见 USER_GUIDE §7.2）。

English:
- Parser-layer module: normalizes the delivery ``response`` string into a
  Responses-shaped dict for the unwrapped record.
- Handles JSON objects, Chat Completions, chunk streams, and SSE event streams.
- Downstream consumer: ``unwrap_delivery_record`` in ``delivery.py``.
"""

from __future__ import annotations

import json
from typing import Any


def _normalize_response_text(text: str) -> str:
    """剥离 SSE 流前导 comment 行 / Strip leading SSE comment lines.

    中文：SSE 规范允许以 ``:`` 开头的 comment 块出现在流最前面；循环剥离直到
    首个非 comment 内容块。

    English: Per SSE spec, colon-prefixed comment blocks may prefix the stream;
    strip them until the first real payload block remains.
    """
    text = text.strip()
    # SSE comment blocks start with ":" and end at the next blank line.
    while text.startswith(":"):
        sep = text.find("\n\n")
        if sep == -1:
            return text.lstrip(":")
        text = text[sep + 2 :].lstrip()
    return text


def _parse_sse_blocks(text: str) -> list[tuple[str | None, dict[str, Any] | None]]:
    """按 SSE 规范分块并解析 event/data / Parse SSE into (event, data) blocks.

    中文：以 ``\\n\\n`` 分隔 event block；每 block 内解析 ``event:`` 与 ``data:`` 行；
    ``[DONE]`` 哨兵跳过；非法 JSON 的 data 行记为 ``data=None``。

    English: Split on blank lines; extract ``event:`` / ``data:`` per block;
    skip ``[DONE]`` markers.
    """
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
    """Chat Completions message → Responses output 块列表。
    Map a Chat Completions ``message`` to Responses ``output`` blocks.

    中文：支持 ``content`` 为 str/list、``reasoning_content``、``tool_calls`` 等
    形态，统一映射为 Responses API 的 output item 列表。

    English: Handles string/list content, reasoning_content, and tool_calls;
    emits Responses-shaped output items.
    """
    content = message.get("content")
    # Branch: plain string content → single output_text block.
    if isinstance(content, str) and content:
        return [{"type": "output_text", "text": content}]
    # Branch: pre-structured content list (already Responses-shaped blocks).
    if isinstance(content, list):
        blocks: list[dict[str, Any]] = []
        for block in content:
            if isinstance(block, dict):
                blocks.append(block)
        return blocks

    # Branch: legacy message with reasoning_content / tool_calls instead of content.
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
    """非流式 chat.completion → response 对象 / Non-streaming completion to response.

    中文：取 ``choices[0].message`` 转为 output；``finish_reason=length`` 时
    ``status=incomplete``。

    English: Reads ``choices[0].message``; maps ``finish_reason=length`` to
    ``status=incomplete``.
    """
    choices = payload.get("choices") or []
    message = choices[0].get("message") if choices and isinstance(choices[0], dict) else {}
    if not isinstance(message, dict):
        message = {}
    output = _message_content_blocks(message)
    finish = choices[0].get("finish_reason") if choices and isinstance(choices[0], dict) else None
    status = "completed"
    if finish == "length":
        # Token limit hit → mark response as incomplete for downstream R5.
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
    """合并流式 chat.completion.chunk 为单个 response / Aggregate streaming chunks.

    中文：reasoning/content 字符串逐 delta 拼接；tool_calls 按 ``index`` 槽位
    累加 name/arguments；最终组装为 Responses output 列表。

    English: Concatenate reasoning/content deltas; merge tool_calls by index slot;
    build a single Responses-shaped dict.
    """
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
                # Streamed tool_calls arrive in fragments keyed by index.
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
    """聚合 Responses API SSE 事件流 / Aggregate Responses API SSE event stream.

    中文：监听 ``response.output_item.done``、``response.completed``、
    ``response.created``；若末项为 function_call 则设 ``_finish_reason=tool_calls``
    （影响下游 R5 判定）。

    English: Collects output items from Responses API streaming events; sets
    ``_finish_reason=tool_calls`` when the last output item is a tool call.
    """
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
            # Final snapshot may contain full output if item.done events were sparse.
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
        # Agent stopped at tool call → downstream extract() yields stop_reason=tool_calls.
        if isinstance(last, dict) and last.get("type") in ("function_call", "custom_tool_call"):
            result["_finish_reason"] = "tool_calls"
    return result


def _data_only_stream_to_response(text: str) -> dict[str, Any]:
    """无 event 行的 data-only SSE fallback / Fallback for data-only SSE streams.

    中文：仅解析 ``data:`` 行；首 chunk 的 ``object`` 类型决定走 aggregate 或
    取最后一个 response 对象。

    English: Parses ``data:`` lines only; first chunk type selects aggregation
    vs. last-response extraction.
    """
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
    """将 delivery 的 response 字段解析为 Responses API response 对象。
    Parse delivery ``response`` field into a Responses API ``response`` dict.

    中文：delivery 日志中 ``response`` 可能是 JSON 字符串或 SSE 文本；本函数统一
    归一化为 ``{"object":"response", "output":[...], "status":..., ...}`` 形态，
    供 ``unwrap_delivery_record`` 写入 unwrapped 记录的 ``response`` 字段。
    返回 ``None`` 当：空输入、非法 JSON、无法识别的 object 类型。

    JSON 路径 ``payload.object`` 分支：
    - ``response`` → 原样返回
    - ``response.compaction`` → 包装为 ``{object:response, output:...}``
    - ``chat.completion`` → ``_chat_completion_to_response``
    - ``chat.completion.chunk`` → ``_aggregate_completion_chunks([payload])``
    - 其他 → ``None``

    English: Handles JSON and SSE variants (chat.completion, chunk streams,
    Responses API events). Returns ``None`` on empty input, invalid JSON,
    or unrecognized format.

    Args:
        text: Raw ``response`` field from a delivery envelope record.

    Returns:
        Responses-shaped dict, or ``None`` if parsing fails.

    See Also:
        unwrap_delivery_record — consumes the parsed response dict.
    """
    # --- Step 0: 空值检查 / Empty input guard ---
    if not text or not str(text).strip():
        return None

    # --- Step 1: normalize（剥离 SSE comment）/ Strip SSE comment prefix ---
    normalized = _normalize_response_text(str(text))
    if not normalized:
        return None

    # --- Step 2: JSON 路径（startswith "{"）/ Pure JSON object path ---
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

    # --- Step 3: SSE 分块 / Parse SSE event blocks ---
    blocks = _parse_sse_blocks(normalized)
    if not blocks:
        if normalized.lstrip().startswith("data:"):
            # --- Step 6: data-only fallback（无 event 行）---
            return _data_only_stream_to_response(normalized)
        return None

    # --- Step 4: Responses API 事件流 / Responses API event stream ---
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

    # --- Step 5: chat.completion.chunk 聚合 / Chat completion chunk aggregation ---
    chunks = [data for _, data in blocks if isinstance(data, dict)]
    if chunks and chunks[0].get("object") == "chat.completion.chunk":
        return _aggregate_completion_chunks(chunks)

    # --- Step 6: data-only fallback / Final fallback ---
    return _data_only_stream_to_response(normalized)
