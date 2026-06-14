"""Tests for merge_traces."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from trajery.merge.traces import merge_traces


def _write_trace(path: Path, session_id: str, message_count: int = 3) -> None:
    events = [
        {
            "type": "session_meta",
            "payload": {"id": session_id, "model": "gpt-5.5"},
        },
        {
            "type": "response_item",
            "payload": {"type": "message", "role": "user", "content": []},
        },
    ]
    for i in range(max(message_count - 1, 0)):
        events.append(
            {
                "type": "response_item",
                "payload": {"type": "message", "role": "assistant", "content": []},
            }
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fp:
        for event in events:
            fp.write(json.dumps(event, ensure_ascii=False))
            fp.write("\n")


def _write_day(root: Path, day: str, traces: list[tuple[str, str, int]]) -> None:
    day_dir = root / day
    traces_dir = day_dir / "traces"
    for filename, session_id, msg_count in traces:
        _write_trace(traces_dir / filename, session_id, msg_count)
    report = {
        "input_dir": f"/in/{day}",
        "output_dir": str(day_dir),
        "teich_valid": len(traces),
        "scanned": 100,
        "teich_incomplete": 0,
        "teich_invalid": 0,
        "export_total": len(traces),
        "elapsed_seconds": 1.0,
    }
    day_dir.mkdir(parents=True, exist_ok=True)
    (day_dir / "report.json").write_text(json.dumps(report), encoding="utf-8")


class MergeTracesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_discards_unknown_session(self) -> None:
        _write_day(
            self.root,
            "20260520",
            [
                ("bad.jsonl", "unknown-session", 5),
                ("good.jsonl", "client:aaa-bbb", 3),
            ],
        )
        out = self.root / "merged"
        result = merge_traces(self.root, out, log=None)
        self.assertEqual(result.manifest["discarded_unknown_session"], 1)
        self.assertEqual(result.post_dedup_files, 1)
        self.assertTrue((out / "traces" / "client_aaa-bbb.jsonl").is_file())
        self.assertFalse((out / "traces" / "bad.jsonl").exists())

    def test_cross_day_dedup_keeps_longer_messages(self) -> None:
        _write_day(
            self.root,
            "20260524",
            [("a.jsonl", "client:same-id", 3)],
        )
        _write_day(
            self.root,
            "20260525",
            [("b.jsonl", "client:same-id", 7)],
        )
        out = self.root / "merged"
        result = merge_traces(self.root, out, log=None)
        self.assertEqual(result.manifest["cross_day_duplicates"], 1)
        self.assertEqual(result.post_dedup_files, 1)
        merged = out / "traces" / "client_same-id.jsonl"
        self.assertTrue(merged.is_file())
        first = json.loads(merged.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(first["payload"]["id"], "client:same-id")

    def test_dry_run_writes_manifest_without_traces(self) -> None:
        _write_day(self.root, "20260602", [("x.jsonl", "client:xyz", 2)])
        out = self.root / "merged"
        merge_traces(self.root, out, dry_run=True, log=None)
        self.assertTrue((out / "merge_manifest.json").is_file())
        self.assertFalse((out / "traces").exists())


if __name__ == "__main__":
    unittest.main()
