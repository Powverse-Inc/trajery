"""Parse distillXC delivery logs into openai_responses records."""

from __future__ import annotations

import json
import tarfile
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from trajery.parser.response_sse import parse_delivery_response


def _iter_jsonl_lines(path: Path) -> Iterator[tuple[int, dict[str, Any] | None, str | None]]:
    with path.open("r", encoding="utf-8", errors="replace") as fp:
        for line_no, raw in enumerate(fp, start=1):
            line = raw.rstrip("\n")
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                yield line_no, None, line
                continue
            if not isinstance(obj, dict):
                yield line_no, None, line
                continue
            yield line_no, obj, line


def inspect_tar_jsonl_members(path: Path) -> dict[str, Any]:
    """Return metadata about ``.jsonl`` members inside a ``.tar.gz`` archive."""
    with tarfile.open(path, "r:gz") as archive:
        members = [name for name in archive.getnames() if name.endswith(".jsonl")]
        if not members:
            return {
                "archive": str(path),
                "jsonl_member_count": 0,
                "used_member": None,
                "skipped_members": [],
            }
        return {
            "archive": str(path),
            "jsonl_member_count": len(members),
            "used_member": members[0],
            "skipped_members": members[1:],
        }


def _iter_tar_jsonl(path: Path) -> Iterator[tuple[str, int, dict[str, Any] | None, str | None]]:
    with tarfile.open(path, "r:gz") as archive:
        members = [name for name in archive.getnames() if name.endswith(".jsonl")]
        if not members:
            return
        member_name = members[0]
        extracted = archive.extractfile(member_name)
        if extracted is None:
            return
        for line_no, raw in enumerate(extracted, start=1):
            line = raw.decode("utf-8", errors="replace").rstrip("\n")
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                yield member_name, line_no, None, line
                continue
            if not isinstance(obj, dict):
                yield member_name, line_no, None, line
                continue
            yield member_name, line_no, obj, line


def _is_under(path: Path, parent: Path) -> bool:
    """Return True when ``path`` is ``parent`` or nested inside it."""
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return path.resolve() == parent.resolve()


def iter_delivery_sources(
    input_dir: Path,
    *,
    exclude_dirs: Iterable[Path] | None = None,
) -> Iterator[tuple[str, Path]]:
    input_root = input_dir.resolve()
    excluded = [path.resolve() for path in (exclude_dirs or [])]

    def _skip(path: Path) -> bool:
        resolved = path.resolve()
        return any(_is_under(resolved, excluded_dir) for excluded_dir in excluded)

    for path in sorted(input_root.rglob("*.jsonl")):
        if _skip(path):
            continue
        yield str(path.relative_to(input_root)), path
    for path in sorted(input_root.rglob("*.tar.gz")):
        if _skip(path):
            continue
        yield str(path.relative_to(input_root)), path


def iter_delivery_records(
    input_dir: Path,
    *,
    limit_files: int | None = None,
    exclude_dirs: Iterable[Path] | None = None,
    tar_warnings: list[dict[str, Any]] | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield delivery metadata dicts with ``record`` when parsing succeeded."""
    seen_files = 0
    for rel, path in iter_delivery_sources(input_dir, exclude_dirs=exclude_dirs):
        seen_files += 1
        if limit_files is not None and seen_files > limit_files:
            return

        if path.name.endswith(".tar.gz"):
            meta = inspect_tar_jsonl_members(path)
            if meta["jsonl_member_count"] > 1 and tar_warnings is not None:
                tar_warnings.append({**meta, "source_file": rel})

        if path.suffix == ".jsonl":
            for line_no, record, _raw in _iter_jsonl_lines(path):
                yield {
                    "source_file": rel,
                    "source_line": line_no,
                    "record": record,
                }
            continue

        for member_name, line_no, record, _raw in _iter_tar_jsonl(path):
            yield {
                "source_file": f"{rel}:{member_name}",
                "source_line": line_no,
                "record": record,
            }


def classify_unwrap_failure(record: dict[str, Any]) -> str | None:
    """Return a failure code when unwrap would fail, else ``None``."""
    request_raw = record.get("request")
    if not isinstance(request_raw, str):
        return "request_not_string"
    try:
        request = json.loads(request_raw)
    except json.JSONDecodeError:
        return "request_json_error"
    if not isinstance(request, dict):
        return "request_not_object"
    if parse_delivery_response(record.get("response")) is None:
        return "response_parse_error"
    return None


def unwrap_delivery_record(record: dict[str, Any]) -> dict[str, Any] | None:
    """Convert one delivery envelope into an openai_responses-shaped dict."""
    if classify_unwrap_failure(record) is not None:
        return None

    request_raw = record.get("request")
    assert isinstance(request_raw, str)
    request = json.loads(request_raw)
    response = parse_delivery_response(record.get("response"))
    assert response is not None

    model = request.get("model") or record.get("model") or record.get("requested_model") or ""
    return {
        **request,
        "model": model,
        "response": response,
        "call_type": "openai_responses",
        "_provenance": {
            "request_id": record.get("request_id"),
            "response_id": record.get("response_id"),
            "usage_log_id": record.get("usage_log_id"),
            "logid": record.get("logid"),
            "trajectory_state": record.get("trajectory_state"),
            "source_stop_reason": record.get("stop_reason"),
            "input_tokens": record.get("input"),
            "output_tokens": record.get("output"),
        },
    }


def session_key_from_record(record: dict[str, Any]) -> str:
    request_id = record.get("request_id") or record.get("response_id") or record.get("usage_log_id")
    if request_id:
        safe = str(request_id).replace(":", "_").replace("/", "_").replace("\\", "_")
        return safe
    return str(record.get("logid") or "unknown")
