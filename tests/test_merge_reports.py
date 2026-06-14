"""Tests for merge_reports."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from trajery.merge.reports import build_merged_report, merge_reports


def _write_day_report(root: Path, day: str, *, teich_valid: int, scanned: int) -> None:
    day_dir = root / day
    day_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "input_dir": f"/in/{day}",
        "output_dir": str(day_dir),
        "scanned": scanned,
        "parse_errors": 0,
        "json_line_errors": 0,
        "unwrapped": scanned,
        "filter_kept": teich_valid,
        "filter_dropped": scanned - teich_valid,
        "dedup_dropped": 0,
        "teich_valid": teich_valid,
        "teich_incomplete": 1,
        "teich_invalid": 0,
        "export_total": teich_valid + 1,
        "elapsed_seconds": 10.0,
        "drop_reasons": {"stop_reason_tool_calls": 5},
        "unwrap_failures": {},
        "teich_errors": {},
        "tar_warnings": [],
    }
    (day_dir / "report.json").write_text(json.dumps(report), encoding="utf-8")


class MergeReportsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_aggregate_sums_and_per_day(self) -> None:
        _write_day_report(self.root, "20260520", teich_valid=10, scanned=100)
        _write_day_report(self.root, "20260524", teich_valid=20, scanned=200)
        manifest = {
            "pre_filter_files": 30,
            "post_filter_files": 25,
            "post_dedup_files": 25,
            "discarded_unknown_session": 5,
            "cross_day_duplicates": 0,
            "discarded_reason": "session_meta.id missing or unknown-session (logid-only upstream data)",
            "per_day": [
                {
                    "date": "20260520",
                    "pre_filter": 10,
                    "discarded_unknown_session": 5,
                    "kept_after_quality_filter": 5,
                },
                {
                    "date": "20260524",
                    "pre_filter": 20,
                    "discarded_unknown_session": 0,
                    "kept_after_quality_filter": 20,
                },
            ],
        }
        report = build_merged_report(self.root, manifest=manifest)
        self.assertEqual(report["report_type"], "merged_delivery_output")
        self.assertEqual(report["teich_valid"], 30)
        self.assertEqual(report["scanned"], 300)
        self.assertEqual(report["quality_filter"]["discarded_unknown_session"], 5)
        self.assertEqual(report["cross_day_dedup"]["teich_valid_final"], 25)
        self.assertEqual(len(report["per_day"]), 2)

    def test_merge_reports_writes_files(self) -> None:
        _write_day_report(self.root, "20260602", teich_valid=3, scanned=30)
        out = self.root / "merged"
        manifest_path = out / "merge_manifest.json"
        out.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(
            json.dumps(
                {
                    "pre_filter_files": 3,
                    "post_filter_files": 3,
                    "post_dedup_files": 3,
                    "discarded_unknown_session": 0,
                    "cross_day_duplicates": 0,
                    "per_day": [
                        {
                            "date": "20260602",
                            "pre_filter": 3,
                            "discarded_unknown_session": 0,
                            "kept_after_quality_filter": 3,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        merge_reports(self.root, out, manifest_path=manifest_path, log=None)
        self.assertTrue((out / "report.json").is_file())
        self.assertTrue((out / "report.md").is_file())
        data = json.loads((out / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(data["teich_valid"], 3)


if __name__ == "__main__":
    unittest.main()
