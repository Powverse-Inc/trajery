"""Path safety utilities and read-size limits for trajery."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable

MAX_READ_BYTES = 4 * 1024 * 1024 * 1024  # 4 GiB


def format_byte_size(size_bytes: int) -> str:
    """Human-readable byte size for log messages."""
    if size_bytes >= 1024**3:
        return f"{size_bytes / 1024**3:.1f} GiB"
    if size_bytes >= 1024**2:
        return f"{size_bytes / 1024**2:.1f} MiB"
    if size_bytes >= 1024:
        return f"{size_bytes / 1024:.1f} KiB"
    return f"{size_bytes} B"


def is_safe_tar_member(name: str) -> bool:
    """True when a tar member name is safe to use as a relative path segment."""
    if not name or "\x00" in name:
        return False
    if not name.endswith(".jsonl"):
        return False
    normalized = name.replace("\\", "/")
    if normalized.startswith("/"):
        return False
    if len(name) >= 2 and name[1] == ":":
        return False
    parts = [part for part in normalized.split("/") if part]
    if any(part == ".." for part in parts):
        return False
    return True


def sanitize_sidecar_segment(segment: str) -> str:
    """Sanitize a source_file segment for dropped/unwrapped sidecar filenames."""
    safe = segment.replace(":", "_").replace("|", "_")
    safe = safe.replace("/", "_").replace("\\", "_")
    safe = safe.replace("<", "_").replace(">", "_")
    parts = [part for part in safe.split("_") if part and part != ".."]
    return "_".join(parts) if parts else "unsafe"


def resolve_under(base: Path, relative: str) -> Path:
    """Resolve ``base / relative`` and ensure the result stays under ``base``."""
    base_resolved = base.resolve()
    dest = (base_resolved / relative).resolve()
    if not dest.is_relative_to(base_resolved):
        raise ValueError(f"path traversal blocked: {relative!r} under {base}")
    return dest


def report_path(path: Path, *, absolute: bool = False) -> str:
    """Serialize a path for reports; default is relative to cwd."""
    if absolute:
        return str(path.resolve())
    try:
        return os.path.relpath(path.resolve(), Path.cwd().resolve())
    except ValueError:
        return path.name


def manifest_trace_path(day: str, trace_path: Path) -> str:
    """Redacted trace path for verbose merge manifests."""
    return f"{day}/{trace_path.name}"


def is_under_root(path: Path, root: Path) -> bool:
    """True when ``path`` resolves under ``root``."""
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def check_read_size(
    source: str,
    size_bytes: int,
    kind: str,
    *,
    limit_bytes: int = MAX_READ_BYTES,
    log: Callable[[str], None] | None = None,
    warnings: list[dict[str, Any]] | None = None,
) -> bool:
    """Return True when ``size_bytes`` is within limit; otherwise warn and return False."""
    if size_bytes <= limit_bytes:
        return True
    limit_label = format_byte_size(limit_bytes)
    size_label = format_byte_size(size_bytes)
    msg = f"WARN: skipped {source} — size {size_label} exceeds MAX_READ_BYTES ({limit_label})"
    if log is not None:
        log(msg)
    if warnings is not None:
        warnings.append(
            {
                "source_file": source,
                "kind": kind,
                "size_bytes": size_bytes,
                "limit_bytes": limit_bytes,
                "reason": "size_exceeded",
            }
        )
    return False


def sidecar_rel_path(source_file: str, source_line: int) -> str:
    """Build a safe relative path for a dropped/unwrapped sidecar file."""
    return f"{sanitize_sidecar_segment(source_file)}_{source_line}.jsonl"


def write_sidecar_jsonl(
    base_dir: Path,
    source_file: str,
    source_line: int,
    record: dict[str, Any],
) -> None:
    """Write one JSONL sidecar record under ``base_dir`` with path-boundary checks."""
    rel = sidecar_rel_path(source_file, source_line)
    path = resolve_under(base_dir, rel)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fp:
        fp.write(json.dumps(record, ensure_ascii=False))
        fp.write("\n")
