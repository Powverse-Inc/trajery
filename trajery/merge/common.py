"""Shared helpers for merge_traces and merge_reports."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

UNKNOWN_SESSION = "unknown-session"
DISCARDED_REASON = (
    "session_meta.id missing or unknown-session (logid-only upstream data)"
)


def safe_filename(session_key: str) -> str:
    """Sanitize session id for trace filename (matches pipeline export)."""
    safe = session_key.replace(":", "_").replace("/", "_").replace("\\", "_")
    safe = safe.replace("<", "_").replace(">", "_").replace("|", "_")
    return safe[:180] if len(safe) > 180 else safe


def discover_day_dirs(
    input_root: Path,
    *,
    include_days: Iterable[str] | None = None,
    exclude: Iterable[str] = ("merged",),
) -> list[Path]:
    """Return sorted day subdirectories that contain report.json."""
    exclude_set = set(exclude)
    allow = set(include_days) if include_days is not None else None
    days: list[Path] = []
    for child in sorted(input_root.iterdir()):
        if not child.is_dir():
            continue
        if child.name in exclude_set:
            continue
        if allow is not None and child.name not in allow:
            continue
        if (child / "report.json").is_file():
            days.append(child)
    return days


def read_session_meta_id(trace_path: Path) -> str | None:
    """Read session_meta.payload.id from the first line of a trace JSONL."""
    with trace_path.open(encoding="utf-8") as fp:
        line = fp.readline()
    if not line.strip():
        return None
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return None
    if event.get("type") != "session_meta":
        return None
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return None
    raw = payload.get("id")
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def is_discarded_session_id(session_id: str | None) -> bool:
    """True when trace should be dropped for data quality (unknown-session)."""
    if session_id is None:
        return True
    if session_id == UNKNOWN_SESSION:
        return True
    return False


def count_trace_messages(trace_path: Path) -> int:
    """Count user/assistant/tool/model messages in a Codex trace JSONL."""
    count = 0
    with trace_path.open(encoding="utf-8") as fp:
        for line in fp:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") != "response_item":
                continue
            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue
            if payload.get("type") != "message":
                continue
            if payload.get("role") in {"user", "assistant", "tool", "model"}:
                count += 1
    return count


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))
