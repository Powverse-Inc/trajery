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


if __name__ == "__main__":
    unittest.main()
