"""Tests for delivery log parsing and Teich export."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from filter_traj_multi_plat import detect_format, evaluate, extract
from tests.paths import FIXTURES_DATA
from trajery.export import (
    openai_responses_to_codex_events,
    validate_trace_with_teich,
    write_trace,
)
from trajery.parser import (
    classify_unwrap_failure,
    inspect_tar_jsonl_members,
    iter_delivery_records,
    iter_delivery_sources,
    parse_delivery_response,
    unwrap_delivery_record,
)


class ResponseParserTests(unittest.TestCase):
    def test_chat_completion_json(self) -> None:
        payload = {
            "id": "resp_test",
            "object": "chat.completion",
            "model": "gpt-5.5",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "hello"},
                    "finish_reason": "stop",
                }
            ],
        }
        result = parse_delivery_response(json.dumps(payload))
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["object"], "response")
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["output"])

    def test_chunk_stream_aggregation(self) -> None:
        chunks = [
            'data: {"object":"chat.completion.chunk","model":"gpt-5.5","choices":[{"delta":{"reasoning_content":"think"}}]}\n\n',
            'data: {"object":"chat.completion.chunk","model":"gpt-5.5","choices":[{"delta":{"content":"hi"}}]}\n\n',
            'data: {"object":"chat.completion.chunk","model":"gpt-5.5","choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","type":"function","function":{"name":"Read","arguments":"{}"}}]}}]}\n\n',
            'data: {"object":"chat.completion.chunk","model":"gpt-5.5","choices":[{"finish_reason":"tool_calls"}]}\n\n',
        ]
        result = parse_delivery_response("".join(chunks))
        self.assertIsNotNone(result)
        assert result is not None
        types = [item.get("type") for item in result.get("output", [])]
        self.assertIn("reasoning", types)
        self.assertIn("message", types)
        self.assertIn("function_call", types)

    def test_event_sse_stream(self) -> None:
        sse = (
            "event: response.created\n"
            'data: {"type":"response.created","response":{"id":"resp_1","model":"gpt-5.5","status":"in_progress"}}\n\n'
            "event: response.output_item.done\n"
            'data: {"type":"response.output_item.done","item":{"type":"function_call","name":"exec_command","call_id":"call_1","arguments":"{}"}}\n\n'
            "event: response.completed\n"
            'data: {"type":"response.completed","response":{"id":"resp_1","status":"completed","output":[]}}\n\n'
        )
        result = parse_delivery_response(sse)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(result["output"]), 1)
        self.assertEqual(result["output"][0]["type"], "function_call")


class DeliveryUnwrapTests(unittest.TestCase):
    def test_unwrap_openai_responses_shape(self) -> None:
        request = {
            "model": "gpt-5.5",
            "instructions": "You are Codex.",
            "tools": [
                {"type": "function", "name": "Read", "parameters": {"type": "object"}}
            ],
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "hello"}],
                },
            ],
        }
        response = {
            "object": "response",
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "done"}],
                }
            ],
        }
        record = {
            "model": "gpt-5.5",
            "request": json.dumps(request),
            "response": json.dumps(response),
            "request_id": "client:test-1",
            "stop_reason": "stop",
        }
        unwrapped = unwrap_delivery_record(record)
        self.assertIsNotNone(unwrapped)
        assert unwrapped is not None
        self.assertEqual(unwrapped["call_type"], "openai_responses")
        self.assertEqual(detect_format(unwrapped), "openai_responses")


class ExcludeOutputDirTests(unittest.TestCase):
    def test_output_dir_is_not_scanned_as_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "raw.jsonl").write_text('{"request":"{}","response":"{}"}\n', encoding="utf-8")
            output_dir = root / "output"
            traces_dir = output_dir / "traces"
            traces_dir.mkdir(parents=True)
            (traces_dir / "client_test.jsonl").write_text('{"type":"session_meta"}\n', encoding="utf-8")

            all_sources = list(iter_delivery_sources(root))
            filtered_sources = list(iter_delivery_sources(root, exclude_dirs=[output_dir]))

            self.assertEqual(len(all_sources), 2)
            self.assertEqual(len(filtered_sources), 1)
            self.assertEqual(filtered_sources[0][0], "raw.jsonl")


class FixtureIntegrationTests(unittest.TestCase):
    def test_committed_pass_fixture_unwraps_and_filters(self) -> None:
        line = (FIXTURES_DATA / "delivery_pass.jsonl").read_text(encoding="utf-8").splitlines()[0]
        record = json.loads(line)
        unwrapped = unwrap_delivery_record(record)
        self.assertIsNotNone(unwrapped)
        trajectory = extract(unwrapped)
        passed, reasons = evaluate(trajectory)
        self.assertTrue(passed, reasons)

    def test_tar_fixture_reports_multiple_members(self) -> None:
        meta = inspect_tar_jsonl_members(FIXTURES_DATA / "delivery_multi_member.tar.gz")
        self.assertEqual(meta["jsonl_member_count"], 2)
        self.assertEqual(meta["used_member"], "shard_a.jsonl")
        self.assertEqual(meta["skipped_members"], ["shard_b.jsonl"])

    def test_tar_single_open_yields_same_records(self) -> None:
        from trajery.parser.delivery import iter_delivery_records, iter_tar_jsonl_with_meta

        tar_path = FIXTURES_DATA / "delivery_multi_member.tar.gz"
        meta = None
        line_count = 0
        for kind, *payload in iter_tar_jsonl_with_meta(tar_path):
            if kind == "meta":
                meta = payload[0]
            else:
                line_count += 1
        self.assertIsNotNone(meta)
        assert meta is not None
        self.assertEqual(meta["jsonl_member_count"], 2)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shutil.copy(tar_path, root / "delivery_multi_member.tar.gz")
            records = list(iter_delivery_records(root))
        self.assertGreaterEqual(len(records), 1)
        self.assertGreaterEqual(line_count, 1)

    def test_malformed_fixture_failure_codes(self) -> None:
        lines = (FIXTURES_DATA / "delivery_malformed.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(classify_unwrap_failure(json.loads(lines[0])), "request_not_string")
        self.assertEqual(classify_unwrap_failure(json.loads(lines[1])), "request_not_string")
        self.assertEqual(classify_unwrap_failure(json.loads(lines[2])), "response_parse_error")


if __name__ == "__main__":
    unittest.main()
