"""Security tests for path handling and read-size limits."""

from __future__ import annotations

import io
import json
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from trajery.parser.delivery import iter_delivery_records, iter_tar_jsonl_with_meta
from trajery.paths import (
    MAX_READ_BYTES,
    is_safe_tar_member,
    resolve_under,
    sanitize_sidecar_segment,
    write_sidecar_jsonl,
)
from trajery.pipeline import process
from tests.paths import FIXTURES_DATA


class PathUtilityTests(unittest.TestCase):
    def test_is_safe_tar_member_rejects_traversal(self) -> None:
        self.assertFalse(is_safe_tar_member("../../evil.jsonl"))
        self.assertFalse(is_safe_tar_member("/tmp/evil.jsonl"))
        self.assertTrue(is_safe_tar_member("shard_a.jsonl"))

    def test_sanitize_sidecar_segment_replaces_separators(self) -> None:
        self.assertEqual(
            sanitize_sidecar_segment("archive.tar.gz:../../evil.jsonl"),
            "archive.tar.gz_evil.jsonl",
        )

    def test_resolve_under_blocks_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            with self.assertRaises(ValueError):
                resolve_under(base, "../outside.jsonl")

    def test_write_sidecar_jsonl_stays_under_base(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "unwrapped"
            write_sidecar_jsonl(base, "nested/file.jsonl", 1, {"ok": True})
            written = list(base.rglob("*.jsonl"))
            self.assertEqual(len(written), 1)
            for path in written:
                self.assertTrue(path.resolve().is_relative_to(base.resolve()))


class TarTraversalTests(unittest.TestCase):
    def _make_tar(self, members: dict[str, str], path: Path) -> None:
        with tarfile.open(path, "w:gz") as archive:
            for name, body in members.items():
                data = body.encode("utf-8")
                info = tarfile.TarInfo(name=name)
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))

    def test_unsafe_tar_member_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tar_path = Path(tmp) / "evil.tar.gz"
            self._make_tar(
                {
                    "../../evil.jsonl": '{"request":"{}","response":"{}"}\n',
                    "safe.jsonl": '{"request":"{}","response":"{}"}\n',
                },
                tar_path,
            )
            records = list(iter_tar_jsonl_with_meta(tar_path, "evil.tar.gz"))
            meta = next(payload[0] for kind, *payload in records if kind == "meta")
            self.assertEqual(meta["used_member"], "safe.jsonl")
            line_records = [
                payload for kind, *payload in records if kind == "record"
            ]
            self.assertEqual(len(line_records), 1)

    def test_keep_unwrapped_does_not_escape_output_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            output_dir = root / "output"
            input_dir.mkdir()
            tar_path = input_dir / "evil.tar.gz"
            line = (FIXTURES_DATA / "delivery_pass.jsonl").read_text(encoding="utf-8").splitlines()[0] + "\n"
            self._make_tar(
                {
                    "../../evil.jsonl": line,
                    "safe.jsonl": line,
                },
                tar_path,
            )
            process(
                input_dir=input_dir,
                output_dir=output_dir,
                apply_filter=False,
                dedup=False,
                keep_unwrapped=True,
                write_dropped=False,
                log=None,
            )
            unwrapped_dir = output_dir / "unwrapped"
            self.assertTrue(unwrapped_dir.is_dir())
            for path in unwrapped_dir.rglob("*.jsonl"):
                self.assertTrue(path.resolve().is_relative_to(unwrapped_dir.resolve()))
            outside = root / "evil.jsonl"
            self.assertFalse(outside.exists())


class ReadSizeLimitTests(unittest.TestCase):
    def test_oversized_gzip_is_skipped_with_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gz_path = root / "big.jsonl.gz"
            gz_path.write_bytes(b"\x1f\x8b\x08")
            warnings: list[dict] = []
            stderr = io.StringIO()
            original_stat = Path.stat

            def fake_stat(self: Path, *args, **kwargs):
                if self == gz_path:
                    return mock.Mock(st_size=MAX_READ_BYTES + 1)
                return original_stat(self, *args, **kwargs)

            with mock.patch.object(Path, "stat", fake_stat):
                records = list(
                    iter_delivery_records(
                        root,
                        size_warnings=warnings,
                        log=stderr.write,
                    )
                )

            self.assertEqual(records, [])
            self.assertEqual(len(warnings), 1)
            self.assertEqual(warnings[0]["reason"], "size_exceeded")
            self.assertIn("exceeds MAX_READ_BYTES", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
