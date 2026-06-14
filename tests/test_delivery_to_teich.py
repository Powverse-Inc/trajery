"""End-to-end and CLI tests for delivery_to_teich."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.paths import FIXTURES_DATA
from trajery.cli.delivery_to_teich import main
from trajery.export import check_teich_available
from trajery.pipeline import process
from trajery.report import to_markdown_report


class DeliveryToTeichProcessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.input_dir = self.root / "input"
        self.output_dir = self.root / "output"
        self.input_dir.mkdir()
        self.output_dir.mkdir()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _copy_fixture(self, name: str) -> None:
        shutil.copy(FIXTURES_DATA / name, self.input_dir / name)

    def test_process_passing_fixture_exports_trace(self) -> None:
        self._copy_fixture("delivery_pass.jsonl")
        stats = process(
            input_dir=self.input_dir,
            output_dir=self.output_dir,
            progress_every=0,
            log=None,
        )
        self.assertEqual(stats.scanned, 1)
        self.assertEqual(stats.unwrapped, 1)
        self.assertEqual(stats.filter_kept, 1)
        self.assertEqual(stats.teich_valid, 1)
        traces = list((self.output_dir / "traces").glob("*.jsonl"))
        self.assertEqual(len(traces), 1)
        first_line = json.loads(traces[0].read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(first_line["type"], "session_meta")

    def test_process_dedup_keeps_longer_session(self) -> None:
        self._copy_fixture("delivery_dedup_pair.jsonl")
        stats = process(
            input_dir=self.input_dir,
            output_dir=self.output_dir,
            progress_every=0,
            log=None,
        )
        self.assertEqual(stats.filter_kept, 2)
        self.assertEqual(stats.dedup_dropped, 0)
        self.assertEqual(stats.teich_valid, 1)
        self.assertEqual(len(list((self.output_dir / "traces").glob("*.jsonl"))), 1)

    def test_process_dedup_drops_shorter_when_seen_second(self) -> None:
        lines = (FIXTURES_DATA / "delivery_dedup_pair.jsonl").read_text(encoding="utf-8").splitlines()
        reversed_path = self.input_dir / "delivery_dedup_reversed.jsonl"
        reversed_path.write_text("\n".join(reversed(lines)) + "\n", encoding="utf-8")
        stats = process(
            input_dir=self.input_dir,
            output_dir=self.output_dir,
            progress_every=0,
            log=None,
        )
        self.assertEqual(stats.filter_kept, 2)
        self.assertEqual(stats.dedup_dropped, 1)
        self.assertEqual(stats.drop_reasons["session_id_duplicate"], 1)

    def test_process_malformed_records_classified(self) -> None:
        self._copy_fixture("delivery_malformed.jsonl")
        stats = process(
            input_dir=self.input_dir,
            output_dir=self.output_dir,
            progress_every=0,
            log=None,
        )
        self.assertEqual(stats.scanned, 3)
        self.assertEqual(stats.json_line_errors, 0)
        self.assertEqual(stats.unwrap_failures["request_not_string"], 2)
        self.assertEqual(stats.unwrap_failures["response_parse_error"], 1)

    def test_tar_multi_member_warning_in_report(self) -> None:
        self._copy_fixture("delivery_multi_member.tar.gz")
        stats = process(
            input_dir=self.input_dir,
            output_dir=self.output_dir,
            progress_every=0,
            log=None,
        )
        self.assertEqual(len(stats.tar_warnings), 1)
        self.assertEqual(stats.tar_warnings[0]["jsonl_member_count"], 2)
        self.assertEqual(stats.tar_warnings[0]["used_member"], "shard_a.jsonl")
        report = stats.to_report(input_dir=self.input_dir, output_dir=self.output_dir)
        self.assertEqual(report["tar_archives_with_multiple_jsonl"], 1)
        self.assertEqual(report["tar_skipped_jsonl_members"], 1)
        self.assertIn("elapsed_seconds", report)
        self.assertGreaterEqual(report["elapsed_seconds"], 0.0)
        self.assertIn("export_total", report)
        self.assertIn("funnel", report)
        self.assertIn("teich_trace_results", report)

    def test_passing_fixture_records_valid_trace_result(self) -> None:
        self._copy_fixture("delivery_pass.jsonl")
        stats = process(
            input_dir=self.input_dir,
            output_dir=self.output_dir,
            progress_every=0,
            log=None,
        )
        report = stats.to_report(
            input_dir=self.input_dir,
            output_dir=self.output_dir,
            include_valid_files=True,
        )
        self.assertEqual(report["export_total"], 1)
        self.assertEqual(len(report["teich_trace_results"]), 1)
        self.assertEqual(report["teich_trace_results"][0]["status"], "valid")
        self.assertNotIn("valid_files_omitted", report)

    def test_incomplete_trace_recorded_in_report(self) -> None:
        self._copy_fixture("delivery_pass.jsonl")
        incomplete_validation = {
            "ok": True,
            "trace_type": "codex",
            "complete": False,
            "error": "trace_is_complete=False",
            "messages_count": 12,
            "tools_count": 2,
            "last_relevant_role": "tool",
        }
        with mock.patch(
            "trajery.pipeline.validate_trace_with_teich",
            return_value=incomplete_validation,
        ):
            stats = process(
                input_dir=self.input_dir,
                output_dir=self.output_dir,
                progress_every=0,
                log=None,
            )
        self.assertEqual(stats.teich_incomplete, 1)
        self.assertEqual(stats.teich_valid, 0)
        report = stats.to_report(input_dir=self.input_dir, output_dir=self.output_dir)
        self.assertEqual(len(report["teich_trace_results"]), 1)
        row = report["teich_trace_results"][0]
        self.assertEqual(row["status"], "incomplete")
        self.assertEqual(row["last_relevant_role"], "tool")
        self.assertNotIn("valid_files_omitted", report)
        self.assertEqual(report["funnel"]["teich_incomplete_rate"], 1.0)

    def test_clean_output_removes_stale_trace_files(self) -> None:
        self._copy_fixture("delivery_pass.jsonl")
        process(
            input_dir=self.input_dir,
            output_dir=self.output_dir,
            progress_every=0,
            log=None,
        )
        stale = self.output_dir / "traces" / "stale.jsonl"
        stale.write_text('{"type":"session_meta"}\n', encoding="utf-8")

        process(
            input_dir=self.input_dir,
            output_dir=self.output_dir,
            progress_every=0,
            clean_output=True,
            log=None,
        )
        self.assertFalse(stale.exists())
        self.assertGreaterEqual(len(list((self.output_dir / "traces").glob("*.jsonl"))), 1)

    def test_no_filter_exports_without_rules(self) -> None:
        self._copy_fixture("delivery_drop_short.jsonl")
        stats = process(
            input_dir=self.input_dir,
            output_dir=self.output_dir,
            apply_filter=False,
            progress_every=0,
            log=None,
        )
        self.assertEqual(stats.filter_kept, 1)
        self.assertEqual(stats.filter_dropped, 0)

    def _stats_dict(self, stats) -> dict:
        return {
            "scanned": stats.scanned,
            "parse_errors": stats.parse_errors,
            "json_line_errors": stats.json_line_errors,
            "unwrap_failures": dict(stats.unwrap_failures),
            "unwrapped": stats.unwrapped,
            "filter_kept": stats.filter_kept,
            "filter_dropped": stats.filter_dropped,
            "dedup_dropped": stats.dedup_dropped,
            "teich_valid": stats.teich_valid,
            "teich_incomplete": stats.teich_incomplete,
            "teich_invalid": stats.teich_invalid,
            "drop_reasons": dict(stats.drop_reasons),
            "tar_warnings": stats.tar_warnings,
        }

    def test_workers_1_matches_default_serial(self) -> None:
        self._copy_fixture("delivery_pass.jsonl")
        serial = process(
            input_dir=self.input_dir,
            output_dir=self.root / "out_serial",
            progress_every=0,
            workers=1,
            log=None,
        )
        parallel = process(
            input_dir=self.input_dir,
            output_dir=self.root / "out_workers1",
            progress_every=0,
            workers=1,
            log=None,
        )
        self.assertEqual(self._stats_dict(serial), self._stats_dict(parallel))

    def test_workers_parallel_same_stats_as_serial(self) -> None:
        fixtures = [
            "delivery_pass.jsonl",
            "delivery_dedup_pair.jsonl",
            "delivery_malformed.jsonl",
            "delivery_multi_member.tar.gz",
        ]
        for name in fixtures:
            with self.subTest(fixture=name):
                input_dir = self.root / f"in_{name}"
                input_dir.mkdir()
                shutil.copy(FIXTURES_DATA / name, input_dir / name)
                serial = process(
                    input_dir=input_dir,
                    output_dir=self.root / f"serial_{name}",
                    progress_every=0,
                    workers=1,
                    log=None,
                )
                parallel = process(
                    input_dir=input_dir,
                    output_dir=self.root / f"parallel_{name}",
                    progress_every=0,
                    workers=4,
                    log=None,
                )
                self.assertEqual(
                    self._stats_dict(serial),
                    self._stats_dict(parallel),
                    f"stats mismatch for {name}",
                )
                self.assertEqual(
                    len(list((self.root / f"serial_{name}" / "traces").glob("*.jsonl"))),
                    len(list((self.root / f"parallel_{name}" / "traces").glob("*.jsonl"))),
                )

    def test_parallel_dedup_cross_file(self) -> None:
        lines = (FIXTURES_DATA / "delivery_dedup_pair.jsonl").read_text(encoding="utf-8").splitlines()
        (self.input_dir / "aaa_shorter.jsonl").write_text(lines[0] + "\n", encoding="utf-8")
        (self.input_dir / "bbb_longer.jsonl").write_text(lines[1] + "\n", encoding="utf-8")

        serial = process(
            input_dir=self.input_dir,
            output_dir=self.root / "cross_serial",
            progress_every=0,
            workers=1,
            log=None,
        )
        parallel = process(
            input_dir=self.input_dir,
            output_dir=self.root / "cross_parallel",
            progress_every=0,
            workers=4,
            log=None,
        )
        self.assertEqual(self._stats_dict(serial), self._stats_dict(parallel))
        self.assertEqual(serial.teich_valid, 1)
        self.assertEqual(serial.dedup_dropped, 0)
        self.assertEqual(serial.filter_kept, 2)

    def test_parallel_dedup_cross_file_drops_shorter(self) -> None:
        lines = (FIXTURES_DATA / "delivery_dedup_pair.jsonl").read_text(encoding="utf-8").splitlines()
        (self.input_dir / "aaa_longer.jsonl").write_text(lines[1] + "\n", encoding="utf-8")
        (self.input_dir / "bbb_shorter.jsonl").write_text(lines[0] + "\n", encoding="utf-8")

        serial = process(
            input_dir=self.input_dir,
            output_dir=self.root / "cross2_serial",
            progress_every=0,
            workers=1,
            log=None,
        )
        parallel = process(
            input_dir=self.input_dir,
            output_dir=self.root / "cross2_parallel",
            progress_every=0,
            workers=4,
            log=None,
        )
        self.assertEqual(self._stats_dict(serial), self._stats_dict(parallel))
        self.assertEqual(serial.dedup_dropped, 1)
        self.assertEqual(serial.drop_reasons["session_id_duplicate"], 1)

    def test_parallel_dedup_tie_break_matches_serial_order(self) -> None:
        line = (FIXTURES_DATA / "delivery_pass.jsonl").read_text(encoding="utf-8").splitlines()[0]
        (self.input_dir / "aaa_first.jsonl").write_text(line + "\n", encoding="utf-8")
        (self.input_dir / "bbb_second.jsonl").write_text(line + "\n", encoding="utf-8")

        serial = process(
            input_dir=self.input_dir,
            output_dir=self.root / "tie_serial",
            progress_every=0,
            workers=1,
            log=None,
        )
        parallel = process(
            input_dir=self.input_dir,
            output_dir=self.root / "tie_parallel",
            progress_every=0,
            workers=4,
            log=None,
        )
        self.assertEqual(self._stats_dict(serial), self._stats_dict(parallel))
        self.assertEqual(serial.dedup_dropped, 1)
        self.assertEqual(serial.teich_valid, 1)


class DeliveryToTeichCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.input_dir = self.root / "input"
        self.output_dir = self.root / "output"
        self.input_dir.mkdir()
        shutil.copy(FIXTURES_DATA / "delivery_pass.jsonl", self.input_dir / "delivery_pass.jsonl")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_strict_empty_exits_1_when_no_valid_traces(self) -> None:
        empty_input = self.root / "empty_input"
        empty_input.mkdir()
        (empty_input / "delivery_malformed.jsonl").write_text(
            '{"request": 1, "response": "{}"}\n',
            encoding="utf-8",
        )
        code = main(
            [
                str(empty_input),
                str(self.output_dir / "empty_out"),
                "--strict-empty",
                "--quiet",
                "--no-report",
            ]
        )
        self.assertEqual(code, 1)

    def test_strict_empty_exits_0_when_valid_traces_exist(self) -> None:
        code = main(
            [
                str(self.input_dir),
                str(self.output_dir),
                "--strict-empty",
                "--quiet",
                "--no-report",
            ]
        )
        self.assertEqual(code, 0)

    def test_missing_teich_exits_3_without_skip_flag(self) -> None:
        with mock.patch(
            "trajery.cli.delivery_to_teich.check_teich_available",
            return_value=(False, "no module"),
        ):
            code = main(
                [str(self.input_dir), str(self.output_dir), "--quiet", "--no-report"]
            )
        self.assertEqual(code, 3)

    def test_skip_teich_validate_allows_missing_teich(self) -> None:
        with mock.patch(
            "trajery.cli.delivery_to_teich.check_teich_available",
            return_value=(False, "no module"),
        ):
            code = main(
                [
                    str(self.input_dir),
                    str(self.output_dir),
                    "--skip-teich-validate",
                    "--quiet",
                    "--no-report",
                ]
            )
        self.assertEqual(code, 0)

    def test_check_teich_available_matches_environment(self) -> None:
        ok, err = check_teich_available()
        if ok:
            self.assertIsNone(err)
        else:
            self.assertIsNotNone(err)

    def test_cli_writes_report_json_and_markdown(self) -> None:
        out_dir = self.output_dir / "with_report"
        code = main([str(self.input_dir), str(out_dir), "--quiet"])
        self.assertEqual(code, 0)
        report_json = out_dir / "report.json"
        report_md = out_dir / "report.md"
        self.assertTrue(report_json.is_file())
        self.assertTrue(report_md.is_file())
        report = json.loads(report_json.read_text(encoding="utf-8"))
        md = report_md.read_text(encoding="utf-8")
        self.assertIn("funnel", report)
        self.assertIn("teich_trace_results", report)
        self.assertIn("# delivery_to_teich Report", md)
        self.assertIn("## 漏斗", md)
        self.assertIn("drop_reasons", md)
        self.assertIn("## Teich 校验", md)

    def test_cli_no_report_md_skips_markdown(self) -> None:
        out_dir = self.output_dir / "json_only"
        code = main(
            [str(self.input_dir), str(out_dir), "--quiet", "--no-report-md"]
        )
        self.assertEqual(code, 0)
        self.assertTrue((out_dir / "report.json").is_file())
        self.assertFalse((out_dir / "report.md").exists())

    def test_markdown_report_renders_incomplete_rows(self) -> None:
        report = {
            "input_dir": "/in",
            "output_dir": "/out",
            "elapsed_seconds": 12.5,
            "export_total": 2,
            "scanned": 10,
            "parse_errors": 0,
            "teich_valid": 1,
            "teich_incomplete": 1,
            "teich_invalid": 0,
            "funnel": {
                "teich_valid_rate": 0.5,
                "teich_incomplete_rate": 0.5,
                "teich_invalid_rate": 0.0,
            },
            "drop_reasons": {"stop_reason_tool_calls": 5},
            "teich_errors": {},
            "teich_trace_results": [
                {
                    "filename": "client_a.jsonl",
                    "session_key": "sess-a",
                    "status": "incomplete",
                    "error": "trace_is_complete=False",
                    "trace_type": "codex",
                    "messages_count": 20,
                    "tools_count": 3,
                    "last_relevant_role": "tool",
                }
            ],
            "valid_files_omitted": 1,
            "tar_warnings": [],
            "unwrap_failures": {},
        }
        md = to_markdown_report(report)
        self.assertIn("client_a.jsonl", md)
        self.assertIn("last_role", md)
        self.assertIn("stop_reason_tool_calls", md)


if __name__ == "__main__":
    unittest.main()
