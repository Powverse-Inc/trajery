"""Tests for Codex trace export and Teich validation."""

from __future__ import annotations

import json
import tempfile
import unittest

from tests.paths import FIXTURES_DATA
from trajery.export import (
    openai_responses_to_codex_events,
    validate_trace_with_teich,
    write_trace,
)
from trajery.parser import unwrap_delivery_record


class TeichExporterTests(unittest.TestCase):
    def test_codex_events_include_session_meta_and_dedup(self) -> None:
        line = (FIXTURES_DATA / "delivery_pass.jsonl").read_text(encoding="utf-8").splitlines()[0]
        record = json.loads(line)
        unwrapped = unwrap_delivery_record(record)
        assert unwrapped is not None

        events = openai_responses_to_codex_events(unwrapped)
        self.assertGreaterEqual(len(events), 3)
        self.assertEqual(events[0]["type"], "session_meta")
        self.assertEqual(events[1]["type"], "turn_context")

        payload_types = [e["payload"].get("type") for e in events if e["type"] == "response_item"]
        self.assertIn("message", payload_types)
        self.assertIn("function_call", payload_types)

        keys = []
        for event in events:
            if event["type"] != "response_item":
                continue
            payload = event["payload"]
            if payload.get("type") == "function_call":
                keys.append(f"function_call:{payload.get('call_id')}")
        self.assertEqual(len(keys), len(set(keys)))

    def test_validate_trace_with_teich_on_passing_fixture(self) -> None:
        line = (FIXTURES_DATA / "delivery_pass.jsonl").read_text(encoding="utf-8").splitlines()[0]
        unwrapped = unwrap_delivery_record(json.loads(line))
        assert unwrapped is not None
        events = openai_responses_to_codex_events(unwrapped)

        with tempfile.TemporaryDirectory() as tmp:
            from pathlib import Path

            trace_path = Path(tmp) / "trace.jsonl"
            write_trace(trace_path, events)
            result = validate_trace_with_teich(trace_path, events)
            err = result.get("error")
            if isinstance(err, str) and err.startswith("teich not installed"):
                self.skipTest(err)
            self.assertTrue(result.get("ok"))
            self.assertTrue(result.get("complete"))


if __name__ == "__main__":
    unittest.main()
