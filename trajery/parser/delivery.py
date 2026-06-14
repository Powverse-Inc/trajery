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
- 上游：``*.jsonl`` / ``*.tar.gz`` 文件；下游：``pipeline.process``、``response_sse.parse_delivery_response``
- 已知限制：每个 ``.tar.gz`` 仅读取第一个 ``.jsonl`` 成员（见 MAINTAINER、USER_GUIDE §5）

English:
- Parser layer: scans delivery files, unwraps L0 envelopes into L1 openai_responses records.
- Consumes ``*.jsonl`` and ``*.tar.gz``; delegates ``response`` parsing to ``response_sse``.
- Tar archives: only the first ``.jsonl`` member is read; multi-member archives emit warnings.

iter_delivery_records yield shape::

    {"source_file": str, "source_line": int, "record": dict | None}
"""

from __future__ import annotations

import json
import tarfile
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from trajery.parser.response_sse import parse_delivery_response


def _iter_jsonl_lines(path: Path) -> Iterator[tuple[int, dict[str, Any] | None, str | None]]:
    """逐行读取 JSONL 文件 / Iterate JSONL lines with parse status.

    中文：yield ``(line_no, dict|None, raw_line)``；``None`` 表示 JSON 解析失败
    或解析结果非 dict。

    English: Yields ``(line_no, record_or_none, raw_line)``; ``None`` record means
    invalid JSON or non-dict payload.
    """
    with path.open("r", encoding="utf-8", errors="replace") as fp:
        for line_no, raw in enumerate(fp, start=1):
            line = raw.rstrip("\n")
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                # JSON line parse failure → record=None for upstream json_line_errors.
                yield line_no, None, line
                continue
            if not isinstance(obj, dict):
                # Valid JSON but not a delivery envelope object.
                yield line_no, None, line
                continue
            yield line_no, obj, line


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
            # Policy: only the first .jsonl member is consumed (see USER_GUIDE §5).
            "used_member": members[0],
            "skipped_members": members[1:],
        }


def _iter_tar_jsonl(path: Path) -> Iterator[tuple[str, int, dict[str, Any] | None, str | None]]:
    """迭代 tar 内首个 .jsonl 成员的行 / Iterate lines from the first .jsonl in tar.

    中文：与 ``inspect_tar_jsonl_members`` 策略一致，仅读 ``members[0]``。

    English: Same first-member-only policy as ``inspect_tar_jsonl_members``.
    """
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
    """判断 path 是否在 parent 目录树下（含自身）/ True when path is under parent.

    中文：用于 ``exclude_dirs`` 判断，防止 ``output_dir`` 被当作输入扫描。

    English: Used to skip paths under excluded directories (e.g. output_dir).
    """
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        # path is not a strict child; check exact match.
        return path.resolve() == parent.resolve()


def iter_delivery_sources(
    input_dir: Path,
    *,
    exclude_dirs: Iterable[Path] | None = None,
) -> Iterator[tuple[str, Path]]:
    """递归扫描 delivery 输入文件 / Recursively discover delivery input files.

    中文：先 ``*.jsonl`` 后 ``*.tar.gz``，均按路径 sorted；yield ``(rel_path, Path)``；
    ``exclude_dirs`` 下的路径跳过。

    English: Yields ``(relative_path, absolute_path)`` for jsonl then tar.gz files,
    sorted; skips paths under ``exclude_dirs``.

    Args:
        input_dir: Root directory to scan recursively.
        exclude_dirs: Directories to exclude (e.g. previous output_dir).

    Yields:
        ``(relative_path, path)`` tuples for each delivery source file.
    """
    input_root = input_dir.resolve()
    excluded = [path.resolve() for path in (exclude_dirs or [])]

    def _skip(path: Path) -> bool:
        resolved = path.resolve()
        return any(_is_under(resolved, excluded_dir) for excluded_dir in excluded)

    # Pass 1: standalone JSONL delivery logs.
    for path in sorted(input_root.rglob("*.jsonl")):
        if _skip(path):
            continue
        yield str(path.relative_to(input_root)), path
    # Pass 2: tar.gz archives containing JSONL members.
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
    """主扫描入口：逐条 yield delivery 记录 / Yield delivery metadata dicts.

    中文：每条 yield 含 ``source_file``、``source_line``、``record``（解析成功时为 dict，
    行级 JSON 失败时为 None）。tar 来源的 ``source_file`` 格式为 ``rel:member_name``。
    多成员 tar 且 ``tar_warnings`` 非 None 时追加告警条目。

    English: Main scan iterator; ``record`` is the parsed envelope dict or ``None``
    on line-level parse failure. Tar sources use ``source_file="rel:member"`` format.

    Args:
        input_dir: Root directory with delivery logs.
        limit_files: Stop after N source files (debug).
        exclude_dirs: Directories to skip during scan.
        tar_warnings: Optional list to collect multi-member tar warnings.

    Yields:
        Dicts with keys ``source_file``, ``source_line``, ``record``.
    """
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

        # Tar branch: iterate first .jsonl member inside archive.
        for member_name, line_no, record, _raw in _iter_tar_jsonl(path):
            yield {
                "source_file": f"{rel}:{member_name}",
                "source_line": line_no,
                "record": record,
            }


def classify_unwrap_failure(record: dict[str, Any]) -> str | None:
    """预判 unwrap 是否会失败 / Return failure code when unwrap would fail.

    中文：dry-run 检查，不实际构造 unwrapped 记录。失败码与 ``report.unwrap_failures`` 对应：

    - ``request_not_string``: ``request`` 缺失或非 str
    - ``request_json_error``: ``request`` 非合法 JSON
    - ``request_not_object``: JSON 解析结果非 dict
    - ``response_parse_error``: ``parse_delivery_response`` 返回 None

    行级 ``record is None`` 不计入此处，而计入 ``json_line_errors``。

    English: Pre-flight unwrap check; returns a failure code string or ``None`` if OK.

    Args:
        record: Parsed delivery envelope dict.

    Returns:
        Failure code string, or ``None`` when unwrap should succeed.
    """
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
    """L0 delivery 信封 → L1 openai_responses 记录 / Convert envelope to openai_responses.

    中文：合并 ``request`` JSON 字段与解析后的 ``response`` dict，附加
    ``call_type="openai_responses"`` 与 ``_provenance`` 溯源块。

    ``_provenance`` 字段：
    - ``request_id`` / ``response_id``: session 标识 → session_meta.id、dedup
    - ``usage_log_id`` / ``logid``: fallback 标识
    - ``trajectory_state``: 诊断
    - ``source_stop_reason``: 来自 ``stop_reason``，与 extract R5 对照
    - ``input_tokens`` / ``output_tokens``: 来自 ``input`` / ``output`` → Codex token_count

    model 合并优先级：``request.model`` → ``record.model`` → ``record.requested_model``。

    English: Expands request JSON, attaches parsed response and provenance metadata.

    Args:
        record: L0 delivery envelope dict.

    Returns:
        L1 openai_responses-shaped dict, or ``None`` on failure.

    See Also:
        classify_unwrap_failure — pre-check without constructing output.
        parse_delivery_response — parses the ``response`` string field.
    """
    if classify_unwrap_failure(record) is not None:
        return None

    request_raw = record.get("request")
    assert isinstance(request_raw, str)
    request = json.loads(request_raw)
    response = parse_delivery_response(record.get("response"))
    assert response is not None

    # Model ID: request body takes precedence over envelope metadata fields.
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
    """从 delivery 记录提取 trace 文件命名键 / Derive trace filename stem from record.

    中文：fallback 链：``request_id`` → ``response_id`` → ``usage_log_id`` → ``logid``
    → ``"unknown"``；替换 ``: / \\`` 为 ``_``（pipeline ``_safe_filename`` 二次处理）。

    English: Primary session key for trace naming; sanitizes path separators.

    Args:
        record: L0 delivery envelope dict.

    Returns:
        Sanitized session key string (before ``_safe_filename`` truncation).
    """
    request_id = record.get("request_id") or record.get("response_id") or record.get("usage_log_id")
    if request_id:
        safe = str(request_id).replace(":", "_").replace("/", "_").replace("\\", "_")
        return safe
    return str(record.get("logid") or "unknown")
