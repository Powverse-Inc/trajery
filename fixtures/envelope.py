"""Helpers to build delivery-log envelope records for tests and fixtures."""

from __future__ import annotations

import json
from typing import Any


def wrap_delivery_record(
    payload: dict[str, Any],
    *,
    request_id: str = "client:fixture-1",
    response_id: str | None = None,
    stop_reason: str = "",
) -> dict[str, Any]:
    """Wrap an openai_responses-shaped dict as a distillXC delivery envelope."""
    request_body = {k: v for k, v in payload.items() if k != "response"}
    response_body = payload.get("response")
    if response_body is None:
        raise ValueError("payload must include a 'response' key")

    record: dict[str, Any] = {
        "model": payload.get("model") or request_body.get("model") or "gpt-5-codex-2025-08-07",
        "request": json.dumps(request_body, ensure_ascii=False),
        "response": json.dumps(response_body, ensure_ascii=False),
        "request_id": request_id,
        "trajectory_state": "root",
    }
    if response_id:
        record["response_id"] = response_id
    if stop_reason:
        record["stop_reason"] = stop_reason
    return record


def passing_openai_responses_payload(*, user_prefix: str = "Read config.yaml.") -> dict[str, Any]:
    """Minimal openai_responses payload that passes R1–R7 after unwrap."""
    return {
        "model": "gpt-5-codex-2025-08-07",
        "instructions": "You are a senior coding assistant. Use the provided tools.",
        "tools": [
            {
                "type": "function",
                "name": "read_file",
                "description": "Read a file.",
                "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
            },
            {
                "type": "function",
                "name": "edit_file",
                "description": "Edit a file.",
                "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
            },
        ],
        "input": [
            {"role": "user", "content": user_prefix},
            {"role": "assistant", "content": "Let me read it first."},
            {
                "type": "function_call",
                "name": "read_file",
                "arguments": '{"path": "config.yaml"}',
                "call_id": "call_1",
            },
            {
                "type": "function_call_output",
                "call_id": "call_1",
                "output": "max_workers: 4\nretries: 3",
            },
            {"role": "user", "content": "OK proceed."},
            {
                "type": "function_call",
                "name": "edit_file",
                "arguments": '{"path": "config.yaml"}',
                "call_id": "call_2",
            },
        ],
        "response": {
            "object": "response",
            "status": "completed",
            "model": "gpt-5-codex-2025-08-07",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Done — max_workers is now 8."}],
                }
            ],
        },
    }
