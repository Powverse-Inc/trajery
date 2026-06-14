#!/usr/bin/env python3
"""Generate committed fixture files under fixtures/data/. Run from trajery/."""

from __future__ import annotations

import io
import json
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fixtures.envelope import passing_openai_responses_payload, wrap_delivery_record

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    DATA.mkdir(parents=True, exist_ok=True)

    pass_payload = passing_openai_responses_payload()
    _write_jsonl(
        DATA / "delivery_pass.jsonl",
        [wrap_delivery_record(pass_payload, request_id="client:pass-1")],
    )

    wrong_model = passing_openai_responses_payload()
    wrong_model["model"] = "claude-3-5-sonnet-20241022"
    _write_jsonl(
        DATA / "delivery_drop_wrong_model.jsonl",
        [wrap_delivery_record(wrong_model, request_id="client:drop-model")],
    )

    no_tools = passing_openai_responses_payload()
    no_tools["tools"] = []
    _write_jsonl(
        DATA / "delivery_drop_no_tools.jsonl",
        [wrap_delivery_record(no_tools, request_id="client:drop-tools")],
    )

    short_msgs = passing_openai_responses_payload()
    short_msgs["input"] = short_msgs["input"][:3]
    _write_jsonl(
        DATA / "delivery_drop_short.jsonl",
        [wrap_delivery_record(short_msgs, request_id="client:drop-short")],
    )

    no_system = passing_openai_responses_payload()
    no_system.pop("instructions", None)
    _write_jsonl(
        DATA / "delivery_drop_no_system.jsonl",
        [wrap_delivery_record(no_system, request_id="client:drop-system")],
    )

    tool_calls_end = passing_openai_responses_payload()
    tool_calls_end["response"] = {
        "object": "response",
        "status": "completed",
        "output": [
            {
                "type": "function_call",
                "name": "edit_file",
                "call_id": "call_3",
                "arguments": "{}",
            }
        ],
    }
    _write_jsonl(
        DATA / "delivery_drop_tool_calls.jsonl",
        [wrap_delivery_record(tool_calls_end, request_id="client:drop-r5")],
    )

    no_tool_call = passing_openai_responses_payload()
    no_tool_call["input"] = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
        {"role": "user", "content": "more"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "again"},
        {"role": "assistant", "content": "sure"},
    ]
    _write_jsonl(
        DATA / "delivery_drop_no_tool_call.jsonl",
        [wrap_delivery_record(no_tool_call, request_id="client:drop-r4")],
    )

    heartbeat = passing_openai_responses_payload()
    heartbeat["input"] = heartbeat["input"] + [
        {"role": "user", "content": "[OpenClaw heartbeat poll] read heartbeat.md"},
        {"role": "assistant", "content": "HEARTBEAT_OK"},
    ]
    heartbeat["response"]["output"] = [
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "HEARTBEAT_OK"}],
        }
    ]
    _write_jsonl(
        DATA / "delivery_drop_heartbeat.jsonl",
        [wrap_delivery_record(heartbeat, request_id="client:drop-r7")],
    )

    dedup_a = passing_openai_responses_payload(user_prefix="Read manifest.json")
    dedup_b = passing_openai_responses_payload(user_prefix="Read manifest.json")
    dedup_b["input"] = dedup_b["input"] + [
        {"role": "user", "content": "extra turn for longer session"},
        {"role": "assistant", "content": "noted"},
    ]
    _write_jsonl(
        DATA / "delivery_dedup_pair.jsonl",
        [
            wrap_delivery_record(dedup_a, request_id="client:dedup-a"),
            wrap_delivery_record(dedup_b, request_id="client:dedup-b"),
        ],
    )

    _write_jsonl(
        DATA / "delivery_malformed.jsonl",
        [
            {"not": "a valid delivery record"},
            {"request": 123, "response": "{}", "request_id": "client:bad-request"},
            {
                "request": json.dumps({"model": "gpt-5", "tools": [], "input": []}),
                "response": "not valid json or sse",
                "request_id": "client:bad-response",
            },
        ],
    )

    tar_path = DATA / "delivery_multi_member.tar.gz"
    first = (DATA / "delivery_pass.jsonl").read_bytes()
    second = (DATA / "delivery_drop_short.jsonl").read_bytes()
    with tarfile.open(tar_path, "w:gz") as archive:
        for name, data in [("shard_a.jsonl", first), ("shard_b.jsonl", second)]:
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))

    print(f"Wrote fixtures under {DATA}")


if __name__ == "__main__":
    main()
