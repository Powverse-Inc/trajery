"""解析 distillXC delivery 日志为 openai_responses 记录。
Parse distillXC delivery logs into openai_responses records.

中文：
- 本模块位于 parser 层，负责 L0 delivery 信封的扫描、解包与 unwrap。
- 核心数据形态（delivery 信封字段）：
  - ``request`` (str, JSON 字符串): OpenAI Responses API 请求体；unwrap 后展开为顶层字段
  - ``response`` (str): 原始响应，可能是 JSON、chat.completion 或 SSE 流
  - ``request_id`` / ``response_id`` / ``usage_log_id``: session 标识与 trace 命名
  - ``model`` / ``requested_model``: 模型 ID
  - ``input`` / ``output`` (int?): token 计数
  - ``stop_reason`` / ``trajectory_state``: 写入 ``_provenance``；R5 以 extract 结果为准
- 上游：``*.jsonl`` / ``*.jsonl.gz`` / ``*.tar.gz`` 文件；下游：``pipeline.process``、``response_sse.parse_delivery_response``
- tar 归档：读取并处理归档内**所有**安全 ``.jsonl`` 成员（见 MAINTAINER、USER_GUIDE §5）

English:
- Parser layer: scans delivery files, unwraps L0 envelopes into L1 openai_responses records.
- Consumes ``*.jsonl``, ``*.jsonl.gz``, and ``*.tar.gz``; delegates ``response`` parsing to ``response_sse``.
- Tar archives: reads and processes **all** safe ``.jsonl`` members inside the archive.

iter_delivery_records yield shape::

    {"source_file": str, "source_line": int, "record": dict | None}
"""

from __future__ import annotations

import gzip
import json
import tarfile
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any, Callable

from trajery.parser.response_sse import parse_delivery_response
from trajery.paths import (
    MAX_READ_BYTES,
    check_read_size,
    is_safe_tar_member,
    is_under_root,
    sanitize_sidecar_segment,
)


def _parse_jsonl_line(
    line_no: int,
    raw: str,
    *,
    source: str,
    size_warnings: list[dict[str, Any]] | None = None,
    log: Callable[[str], None] | None = None,
) -> tuple[int, dict[str, Any] | None, str | None] | None:
    """解析单行 JSONL / Parse one JSONL line.

    中文：空行返回 ``None``；否则 yield ``(line_no, dict|None, raw_line)``。

    English: Returns ``None`` for blank lines; otherwise a parse result tuple.
    """
    line = raw.rstrip("\n")
    if not line.strip():
        return None
    line_size = len(raw.encode("utf-8", errors="replace"))
    if not check_read_size(
        f"{source}:{line_no}",
        line_size,
        "jsonl_line",
        log=log,
        warnings=size_warnings,
    ):
        return line_no, None, line
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        # JSON line parse failure → record=None for upstream json_line_errors.
        return line_no, None, line
    if not isinstance(obj, dict):
        # Valid JSON but not a delivery envelope object.
        return line_no, None, line
    return line_no, obj, line


def _iter_jsonl_lines(
    path: Path,
    rel: str,
    *,
    size_warnings: list[dict[str, Any]] | None = None,
    log: Callable[[str], None] | None = None,
) -> Iterator[tuple[int, dict[str, Any] | None, str | None]]:
    """逐行读取 JSONL 文件 / Iterate JSONL lines with parse status."""
    try:
        file_size = path.stat().st_size
    except OSError:
        return
    if not check_read_size(rel, file_size, "file", log=log, warnings=size_warnings):
        return
    with path.open("r", encoding="utf-8", errors="replace") as fp:
        for line_no, raw in enumerate(fp, start=1):
            parsed = _parse_jsonl_line(
                line_no,
                raw,
                source=rel,
                size_warnings=size_warnings,
                log=log,
            )
            if parsed is not None:
                yield parsed


def _iter_gzip_jsonl_lines(
    path: Path,
    rel: str,
    *,
    size_warnings: list[dict[str, Any]] | None = None,
    log: Callable[[str], None] | None = None,
) -> Iterator[tuple[int, dict[str, Any] | None, str | None]]:
    """逐行读取 gzip 压缩的 JSONL 文件 / Iterate gzip-compressed JSONL lines."""
    try:
        file_size = path.stat().st_size
    except OSError:
        return
    if not check_read_size(rel, file_size, "gzip", log=log, warnings=size_warnings):
        return
    with gzip.open(path, "rt", encoding="utf-8", errors="replace") as fp:
        for line_no, raw in enumerate(fp, start=1):
            parsed = _parse_jsonl_line(
                line_no,
                raw,
                source=rel,
                size_warnings=size_warnings,
                log=log,
            )
            if parsed is not None:
                yield parsed


def _tar_jsonl_member_names(archive: tarfile.TarFile) -> list[str]:
    """List safe ``.jsonl`` member paths inside an open tar archive."""
    return [
        name
        for name in archive.getnames()
        if name.endswith(".jsonl") and is_safe_tar_member(name)
    ]


def _tar_jsonl_meta(path: Path, members: list[str]) -> dict[str, Any]:
    """Build tar member metadata dict from a precomputed member list."""
    if not members:
        return {
            "archive": str(path),
            "jsonl_member_count": 0,
            "used_member": None,
            "used_members": [],
            "skipped_members": [],
        }
    return {
        "archive": str(path),
        "jsonl_member_count": len(members),
        # Back-compat: some reports/tests expect a single "used_member".
        "used_member": members[0],
        # New policy: process ALL safe .jsonl members; nothing is skipped.
        "used_members": members,
        "skipped_members": [],
    }


def inspect_tar_jsonl_members(path: Path) -> dict[str, Any]:
    """返回 tar 内 .jsonl 成员元数据 / Return metadata about ``.jsonl`` members in a tar.

    中文：仅列出成员名，**不**读取内容；返回 schema 含 ``archive``、
    ``jsonl_member_count``、``used_member``、``skipped_members`` 四键。

    English: Lists ``.jsonl`` member names without reading content; documents which
    member would be used (first) and which would be skipped.

    Returns:
        Metadata dict with keys ``archive``, ``jsonl_member_count``,
        ``used_member``, ``skipped_members``.
    """
    with tarfile.open(path, "r:gz") as archive:
        return _tar_jsonl_meta(path, _tar_jsonl_member_names(archive))


def _iter_tar_jsonl_lines(
    archive: tarfile.TarFile,
    member_name: str,
    rel: str,
    *,
    size_warnings: list[dict[str, Any]] | None = None,
    log: Callable[[str], None] | None = None,
) -> Iterator[tuple[int, dict[str, Any] | None, str | None]]:
    """Iterate parsed JSONL lines from one tar member inside an open archive."""
    if not is_safe_tar_member(member_name):
        return
    try:
        member = archive.getmember(member_name)
    except KeyError:
        return
    source = f"{rel}:{member_name}"
    if not check_read_size(
        source,
        member.size,
        "tar_member",
        log=log,
        warnings=size_warnings,
    ):
        return
    extracted = archive.extractfile(member_name)
    if extracted is None:
        return
    for line_no, raw in enumerate(extracted, start=1):
        parsed = _parse_jsonl_line(
            line_no,
            raw.decode("utf-8", errors="replace"),
            source=source,
            size_warnings=size_warnings,
            log=log,
        )
        if parsed is not None:
            yield parsed


def _iter_tar_jsonl(
    path: Path,
    rel: str,
    *,
    size_warnings: list[dict[str, Any]] | None = None,
    log: Callable[[str], None] | None = None,
) -> Iterator[tuple[str, int, dict[str, Any] | None, str | None]]:
    """迭代 tar 内所有安全 .jsonl 成员的行 / Iterate lines from all safe .jsonl in tar."""
    with tarfile.open(path, "r:gz") as archive:
        members = _tar_jsonl_member_names(archive)
        if not members:
            return
        for member_name in members:
            for line_no, record, raw in _iter_tar_jsonl_lines(
                archive,
                member_name,
                rel,
                size_warnings=size_warnings,
                log=log,
            ):
                yield member_name, line_no, record, raw


def iter_tar_jsonl_with_meta(
    path: Path,
    rel: str | None = None,
    *,
    size_warnings: list[dict[str, Any]] | None = None,
    log: Callable[[str], None] | None = None,
) -> Iterator[tuple[str, Any]]:
    """单次打开 tar：先 yield 元数据，再 yield 行记录 / Open tar once for meta + lines.

    Yields:
        ``("meta", meta_dict)`` then ``("record", source_file, line_no, record)``.
    """
    rel_path = rel or path.name
    with tarfile.open(path, "r:gz") as archive:
        members = _tar_jsonl_member_names(archive)
        meta = _tar_jsonl_meta(path, members)
        yield "meta", meta
        if not members:
            return
        for member_name in members:
            safe_member = sanitize_sidecar_segment(member_name)
            for line_no, record, _raw in _iter_tar_jsonl_lines(
                archive,
                member_name,
                rel_path,
                size_warnings=size_warnings,
                log=log,
            ):
                yield "record", f"{rel_path}:{safe_member}", line_no, record


def iter_records_from_source(
    rel: str,
    path: Path,
    *,
    size_warnings: list[dict[str, Any]] | None = None,
    log: Callable[[str], None] | None = None,
) -> Iterator[dict[str, Any]]:
    """逐条 yield 单个源文件（jsonl 或 tar.gz）的 delivery 记录 / Yield records from one file.

    Yields:
        Dicts with keys ``source_file``, ``source_line``, ``record``.
    """
    if path.name.endswith(".jsonl.gz"):
        line_iter = _iter_gzip_jsonl_lines(
            path,
            rel,
            size_warnings=size_warnings,
            log=log,
        )
    elif path.suffix == ".jsonl":
        line_iter = _iter_jsonl_lines(
            path,
            rel,
            size_warnings=size_warnings,
            log=log,
        )
    else:
        line_iter = None

    if line_iter is not None:
        for line_no, record, _raw in line_iter:
            yield {
                "source_file": rel,
                "source_line": line_no,
                "record": record,
            }
        return

    for kind, *payload in iter_tar_jsonl_with_meta(
        path,
        rel,
        size_warnings=size_warnings,
        log=log,
    ):
        if kind == "meta":
            continue
        source_file, line_no, record = payload
        yield {
            "source_file": source_file,
            "source_line": line_no,
            "record": record,
        }


def _is_under(path: Path, parent: Path) -> bool:
    """判断 path 是否在 parent 目录树下（含自身）/ True when path is under parent."""
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
    """递归扫描 delivery 输入文件 / Recursively discover delivery input files."""
    input_root = input_dir.resolve()
    excluded = [path.resolve() for path in (exclude_dirs or [])]

    def _skip(path: Path) -> bool:
        resolved = path.resolve()
        if not is_under_root(resolved, input_root):
            return True
        return any(_is_under(resolved, excluded_dir) for excluded_dir in excluded)

    for pattern in ("*.jsonl", "*.jsonl.gz", "*.tar.gz"):
        for path in sorted(input_root.rglob(pattern)):
            if _skip(path):
                continue
            yield str(path.relative_to(input_root)), path


def iter_delivery_records(
    input_dir: Path,
    *,
    limit_files: int | None = None,
    exclude_dirs: Iterable[Path] | None = None,
    tar_warnings: list[dict[str, Any]] | None = None,
    size_warnings: list[dict[str, Any]] | None = None,
    log: Callable[[str], None] | None = None,
) -> Iterator[dict[str, Any]]:
    """主扫描入口：逐条 yield delivery 记录 / Yield delivery metadata dicts."""
    seen_files = 0
    for rel, path in iter_delivery_sources(input_dir, exclude_dirs=exclude_dirs):
        seen_files += 1
        if limit_files is not None and seen_files > limit_files:
            return

        if path.name.endswith(".tar.gz"):
            tar_meta: dict[str, Any] | None = None
            for kind, *payload in iter_tar_jsonl_with_meta(
                path,
                rel,
                size_warnings=size_warnings,
                log=log,
            ):
                if kind == "meta":
                    tar_meta = payload[0]
                    if tar_meta["jsonl_member_count"] > 1 and tar_warnings is not None:
                        tar_warnings.append({**tar_meta, "source_file": rel})
                    continue
                source_file, line_no, record = payload
                yield {
                    "source_file": source_file,
                    "source_line": line_no,
                    "record": record,
                }
            if tar_meta is not None and tar_meta["jsonl_member_count"] == 0:
                if size_warnings is not None:
                    size_warnings.append(
                        {
                            "source_file": rel,
                            "kind": "tar_member",
                            "size_bytes": 0,
                            "limit_bytes": MAX_READ_BYTES,
                            "reason": "no_safe_jsonl_member",
                        }
                    )
                if log is not None:
                    log(f"WARN: skipped {rel} — no safe .jsonl members in tar archive")
            continue

        for item in iter_records_from_source(
            rel,
            path,
            size_warnings=size_warnings,
            log=log,
        ):
            yield item


def classify_unwrap_failure(record: dict[str, Any]) -> str | None:
    """预判 unwrap 是否会失败 / Return failure code when unwrap would fail."""
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
    """L0 delivery 信封 → L1 openai_responses 记录 / Convert envelope to openai_responses."""
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
    """从 delivery 记录提取 trace 文件命名键 / Derive trace filename stem from record."""
    request_id = record.get("request_id") or record.get("response_id") or record.get("usage_log_id")
    if request_id:
        safe = str(request_id).replace(":", "_").replace("/", "_").replace("\\", "_")
        return safe
    return str(record.get("logid") or "unknown")
