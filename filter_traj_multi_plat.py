#!/usr/bin/env python3
"""Cross-platform trajectory filter — single-file, stdlib-only.

Self-contained module shipped with trajery. No third-party dependencies:
one ``.py`` file, edited and tested within this repository.

Usage
-----
    python3 filter_traj_multi_plat.py <input_dir> [<output_dir>] \\
        [--format-glob PATTERN] [--report PATH] [--limit N] [--quiet] \\
        [--strict-empty] [--dedup-mode stats|full|off]

What it does
------------
Walks ``input_dir`` recursively for ``*.json`` (override with --format-glob),
parses each file as one API request+response payload, detects which API
shape it is (Anthropic Messages / Bedrock Converse / OpenAI Chat / OpenAI
Responses), normalises it, then applies the mptf rules:

    R1 model_match       model name in Opus 4.5+ OR GPT-5 non-mini family
    R2 has_tools         request had a non-empty tools/functions list
    R3 min_messages      request transcript had > 5 messages
    R4 has_tool_call     at least one assistant turn invoked a tool
    R5 stop_reason       ended cleanly (end_turn / stop, never tool_use / length)
    R6 has_system_prompt record carries a non-empty system / developer prompt
                         (top-level ``system`` / ``system_prompt`` /
                         ``instructions``, or a ``role==system|developer``
                         message). Ordered last so the funnel reports
                         structural failures first.
    R7 no_heartbeat      drops high-confidence OpenClaw / EvoMap heartbeat
                         polling traffic AND explicit silent keep-alive
                         markers (``[SLIENT]``, ``[SILENT]``), but keeps
                         ordinary engineering conversations that merely
                         mention "heartbeat".

All rules are evaluated for every file so the breakdown shows *every*
failing reason, not just the first.

If ``output_dir`` is supplied, kept files are copied there preserving the
relative path under ``input_dir``. Otherwise only stats are printed.

After rule filtering, by default the surviving records are inspected for
session_id collisions (MD5 of canonicalised tools + first-turn prefix)
and a stats line is printed showing how many records would collapse if
real dedup were applied — but no record is dropped. This is the
low-resource default safe to run on a laptop. Pass ``--dedup-mode=full``
to actually drop duplicates (longest-messages record wins per session_id;
losers are reported with reason ``session_id_duplicate``); or
``--dedup-mode=off`` (alias ``--no-dedup``) to skip session_id
computation entirely. session_id is computed from a canonical OpenAI Chat
shape so duplicate groups can be compared consistently across runs.

Requires Python 3.10+ (uses PEP 604 unions: ``str | None``).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Iterable, Optional


# =============================================================================
# formats — platform detection + normalisation
# =============================================================================

# A normalised trajectory record. All fields are best-effort; missing values
# are filled with sensible defaults so rule code can rely on shape.
Trajectory = dict[str, Any]


def detect_format(d: dict) -> str:
    """Return one of ``anthropic_messages`` / ``bedrock_converse`` /
    ``openai_chat`` / ``openai_responses`` / ``unknown``.
    """
    if not isinstance(d, dict):
        return "unknown"

    response = d.get("response")
    if not isinstance(response, dict):
        response = d

    # --- OpenAI Responses API ---------------------------------------------
    obj = response.get("object")
    if obj == "response":
        return "openai_responses"
    if isinstance(response.get("output"), list) and "status" in response and "choices" not in response:
        return "openai_responses"

    # --- OpenAI Chat Completions ------------------------------------------
    if obj == "chat.completion":
        return "openai_chat"
    choices = response.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict) and ("message" in first or "finish_reason" in first or "delta" in first):
            return "openai_chat"

    # --- Bedrock Converse --------------------------------------------------
    if "stopReason" in response:
        return "bedrock_converse"
    output = response.get("output")
    if isinstance(output, dict) and isinstance(output.get("message"), dict):
        return "bedrock_converse"

    # --- Anthropic Messages ------------------------------------------------
    if response.get("type") == "message":
        return "anthropic_messages"
    if "stop_reason" in response and isinstance(response.get("content"), list):
        return "anthropic_messages"

    # Fallback: honour explicit call_type hint, otherwise sniff messages.
    call_type = d.get("call_type")
    if isinstance(call_type, str):
        ct = call_type.lower()
        if "anthropic" in ct:
            return "anthropic_messages"
        if "bedrock" in ct and "converse" in ct:
            return "bedrock_converse"
        if "responses" in ct:
            return "openai_responses"
        if "openai" in ct or "chat_completion" in ct or "chat-completion" in ct:
            return "openai_chat"
    messages = d.get("messages")
    if isinstance(messages, list) and messages:
        for m in messages:
            if isinstance(m, dict) and isinstance(m.get("content"), list):
                for b in m["content"]:
                    if isinstance(b, dict) and b.get("type") in ("text", "tool_use", "tool_result", "thinking"):
                        return "anthropic_messages"
        return "openai_chat"

    return "unknown"


def _unwrap_response(d: dict) -> dict:
    r = d.get("response")
    return r if isinstance(r, dict) else d


# Roles that count as carrying a system / developer prompt inside ``messages``.
# OpenAI added ``developer`` for the reasoning model family — semantically
# the same channel (out-of-band instructions to the assistant).
_SYSTEM_LIKE_ROLES = ("system", "developer")


def _coerce_text_blocks(value: Any) -> str:
    """Reduce a system-prompt-shaped value to a single string.

    Accepts: plain string; list of text blocks (Anthropic / Responses /
    Bedrock Converse shapes); list of strings. Anything else returns ``""``.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for b in value:
            if isinstance(b, str):
                parts.append(b)
            elif isinstance(b, dict):
                if "text" in b and isinstance(b["text"], str):
                    parts.append(b["text"])
        return "\n".join(p for p in parts if p)
    return ""


def extract_system_prompt(d: dict, messages: Iterable[Any] | None = None) -> str:
    """Best-effort recovery of the system / developer prompt text.

    Locations checked (priority order):

      1. Top-level ``system`` / ``system_prompt`` / ``instructions`` (request
         envelope; Anthropic / Bedrock Converse / OpenAI Responses / custom
         exports).
      2. ``role`` in {``system``, ``developer``} inside the messages list
         (OpenAI Chat, and the Responses ``input`` items hoisted into
         ``messages`` by the Responses extractor).

    Returns the first non-empty match, or ``""`` if nothing was found.
    """
    for key in ("system", "system_prompt", "instructions"):
        raw = d.get(key)
        text = _coerce_text_blocks(raw)
        if text:
            return text

    if messages is None:
        msgs = d.get("messages")
        if not isinstance(msgs, list):
            msgs = d.get("input")
        messages = msgs if isinstance(msgs, list) else []

    for m in messages or []:
        if not isinstance(m, dict):
            continue
        if m.get("role") not in _SYSTEM_LIKE_ROLES:
            continue
        text = _coerce_text_blocks(m.get("content"))
        if text:
            return text

    return ""


def extract_anthropic_messages(d: dict) -> Trajectory:
    response = _unwrap_response(d)
    messages = d.get("messages") or []
    return {
        "format": "anthropic_messages",
        "model": d.get("model") or response.get("model") or "",
        "tools": d.get("tools") or [],
        "messages": messages,
        "stop_reason": response.get("stop_reason"),
        "system_prompt": extract_system_prompt(d, messages),
        "_response": response,
    }


def extract_bedrock_converse(d: dict) -> Trajectory:
    response = _unwrap_response(d)
    tool_config = d.get("toolConfig") or {}
    tools = tool_config.get("tools") if isinstance(tool_config, dict) else None
    model = d.get("model") or d.get("modelId") or response.get("model") or ""
    messages = d.get("messages") or []
    return {
        "format": "bedrock_converse",
        "model": model,
        "tools": tools or [],
        "messages": messages,
        "stop_reason": response.get("stopReason"),
        "system_prompt": extract_system_prompt(d, messages),
        "_response": response,
    }


def extract_openai_chat(d: dict) -> Trajectory:
    response = _unwrap_response(d)
    choices = response.get("choices") or []
    finish = None
    if choices and isinstance(choices[0], dict):
        finish = choices[0].get("finish_reason")
    tools = d.get("tools") or d.get("functions") or []
    model = d.get("model") or response.get("model") or ""
    messages = d.get("messages") or []
    return {
        "format": "openai_chat",
        "model": model,
        "tools": tools,
        "messages": messages,
        "stop_reason": finish,
        "system_prompt": extract_system_prompt(d, messages),
        "_response": response,
    }


def extract_openai_responses(d: dict) -> Trajectory:
    response = _unwrap_response(d)
    status = response.get("status")
    incomplete = response.get("incomplete_details") or {}
    incomplete_reason = incomplete.get("reason") if isinstance(incomplete, dict) else None

    stop_reason: str | None
    if status == "completed":
        stop_reason = "stop"
    elif status == "incomplete":
        if incomplete_reason == "max_output_tokens":
            stop_reason = "length"
        elif incomplete_reason == "content_filter":
            stop_reason = "content_filter"
        else:
            stop_reason = None
    else:
        stop_reason = None

    output_items = response.get("output") or []
    if isinstance(output_items, list):
        last_item = output_items[-1] if output_items else None
        if isinstance(last_item, dict) and last_item.get("type") in ("function_call", "custom_tool_call"):
            stop_reason = "tool_calls"

    inp = d.get("input")
    if isinstance(inp, str):
        messages: list = [{"role": "user", "content": inp}]
    elif isinstance(inp, list):
        messages = inp
    else:
        messages = d.get("messages") or []

    tools = d.get("tools") or []
    model = d.get("model") or response.get("model") or ""

    return {
        "format": "openai_responses",
        "model": model,
        "tools": tools,
        "messages": messages,
        "stop_reason": stop_reason,
        "system_prompt": extract_system_prompt(d, messages),
        "_response": response,
        "_raw_status": status,
        "_raw_output": output_items,
    }


_EXTRACTORS = {
    "anthropic_messages": extract_anthropic_messages,
    "bedrock_converse":  extract_bedrock_converse,
    "openai_chat":       extract_openai_chat,
    "openai_responses":  extract_openai_responses,
}


def extract(d: dict) -> Trajectory:
    """Detect the format of ``d`` and normalise it into a Trajectory dict."""
    fmt = detect_format(d)
    if fmt == "unknown":
        messages = d.get("messages") if isinstance(d.get("messages"), list) else []
        return {
            "format": "unknown",
            "model": d.get("model") if isinstance(d.get("model"), str) else "",
            "tools": d.get("tools") if isinstance(d.get("tools"), list) else [],
            "messages": messages,
            "stop_reason": None,
            "system_prompt": extract_system_prompt(d, messages),
            "_response": d.get("response") or {},
        }
    return _EXTRACTORS[fmt](d)


def _iter_assistant_messages(messages: Iterable[Any]) -> Iterable[dict]:
    for m in messages:
        if isinstance(m, dict) and m.get("role") == "assistant":
            yield m


def has_assistant_tool_call(trajectory: Trajectory) -> bool:
    """True iff some assistant turn (request OR response) invoked a tool.

    Recognises all four shapes (Anthropic / Bedrock Converse / OpenAI Chat /
    OpenAI Responses).
    """
    messages = trajectory.get("messages") or []
    fmt = trajectory.get("format")

    if fmt == "openai_responses":
        for item in messages:
            if isinstance(item, dict) and item.get("type") in ("function_call", "custom_tool_call"):
                return True

    for m in _iter_assistant_messages(messages):
        if isinstance(m.get("tool_calls"), list) and m["tool_calls"]:
            return True
        if isinstance(m.get("function_call"), dict):
            return True
        content = m.get("content")
        if isinstance(content, list):
            for b in content:
                if not isinstance(b, dict):
                    continue
                t = b.get("type")
                if t in ("tool_use", "toolUse"):
                    return True
                if "toolUse" in b and isinstance(b["toolUse"], dict):
                    return True

    response = trajectory.get("_response") or {}
    if isinstance(response, dict):
        choices = response.get("choices") or []
        if choices and isinstance(choices[0], dict):
            msg = choices[0].get("message") or {}
            if isinstance(msg.get("tool_calls"), list) and msg["tool_calls"]:
                return True
            if isinstance(msg.get("function_call"), dict):
                return True
        for b in response.get("content") or []:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                return True
        out = response.get("output") or {}
        msg = out.get("message") if isinstance(out, dict) else None
        if isinstance(msg, dict):
            for b in msg.get("content") or []:
                if isinstance(b, dict) and ("toolUse" in b or b.get("type") == "tool_use"):
                    return True
        for item in response.get("output") or []:
            if isinstance(item, dict) and item.get("type") in ("function_call", "custom_tool_call"):
                return True

    return False


# --- model family matchers ---------------------------------------------------

MIN_OPUS_MAJOR = 4
MIN_OPUS_MINOR_FOR_MAJOR = 5

_OPUS_VERSION_RE = re.compile(
    r"(?:^|[/.\-_])"        # boundary
    r"opus[-_](?P<major>\d+)"
    r"(?:(?:[-_.])(?P<minor>\d{1,2}))?"
    r"(?:[-_.@:]|$)",       # boundary
    re.IGNORECASE,
)
_OPUS_LEGACY_RE = re.compile(
    r"(?:^|[/.\-_])3[-_]opus(?:[-_.@:]|$)",
    re.IGNORECASE,
)


def is_opus_model(name: str) -> bool:
    """True iff ``name`` references an Opus 4.5+ model."""
    if not name or not isinstance(name, str):
        return False
    if _OPUS_LEGACY_RE.search(name):
        return False
    match = _OPUS_VERSION_RE.search(name)
    if not match:
        return False
    major = int(match.group("major"))
    minor_raw = match.group("minor")
    minor = int(minor_raw) if minor_raw is not None else 0
    if major > MIN_OPUS_MAJOR:
        return True
    return major == MIN_OPUS_MAJOR and minor >= MIN_OPUS_MINOR_FOR_MAJOR


_GPT5_RE = re.compile(
    r"(?:^|[/.\-_])"
    r"gpt[-_]?5"
    r"(?:$|[-_.@:])",
    re.IGNORECASE,
)
_GPT5_SMALL_RE = re.compile(
    r"(?:^|[/.\-_])"
    r"gpt[-_]?5[-_](?:mini|nano)"
    r"(?:$|[-_.@:])",
    re.IGNORECASE,
)


def is_gpt5_model(name: str) -> bool:
    """True iff ``name`` references a kept OpenAI GPT-5 model."""
    if not name or not isinstance(name, str):
        return False
    if re.search(r"gpt[-_]?4[-_.]5\b", name, re.I):
        return False
    if _GPT5_SMALL_RE.search(name):
        return False
    return bool(_GPT5_RE.search(name))


def model_matches(name: str) -> bool:
    """True iff ``name`` matches any kept family (Opus 4.5+ OR GPT-5 non-mini)."""
    return is_opus_model(name) or is_gpt5_model(name)


# =============================================================================
# rules — mptf rule engine, all rules evaluated for full failure breakdown
# =============================================================================

_ACCEPTED_STOP_REASONS = {
    "anthropic_messages": {"end_turn"},
    "bedrock_converse":  {"end_turn", "stop_sequence"},
    "openai_chat":       {"stop"},
    "openai_responses":  {"stop"},
}

MIN_MESSAGES_EXCLUSIVE = 5  # request transcript must have > 5 messages


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                for key in ("text", "output", "content"):
                    value = block.get(key)
                    if isinstance(value, str):
                        parts.append(value)
                        break
        return "\n".join(p for p in parts if p)
    if content is None:
        return ""
    return str(content)


def _trim_for_scan(text: str, limit: int = 4000) -> str:
    if len(text) <= limit:
        return text
    half = max(1, limit // 2)
    return text[:half] + "\n" + text[-half:]


def _norm(text: str) -> str:
    return " ".join((text or "").strip().lower().split())


def _iter_messages(t: Trajectory) -> Iterable[dict]:
    for m in t.get("messages") or []:
        if isinstance(m, dict):
            yield m


def _iter_assistant_texts(t: Trajectory) -> Iterable[str]:
    for m in _iter_messages(t):
        if m.get("role") == "assistant" or (
            m.get("type") == "message" and m.get("role") == "assistant"
        ):
            text = _content_text(m.get("content"))
            if text:
                yield text

    response = t.get("_response") or {}
    if not isinstance(response, dict):
        return

    choices = response.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0] if isinstance(choices[0], dict) else {}
        msg = first.get("message")
        if isinstance(msg, dict):
            text = _content_text(msg.get("content"))
            if text:
                yield text

    text = _content_text(response.get("content"))
    if text:
        yield text

    output = response.get("output")
    if isinstance(output, dict):
        msg = output.get("message")
        if isinstance(msg, dict):
            text = _content_text(msg.get("content"))
            if text:
                yield text

    output_items = response.get("output")
    if isinstance(output_items, list):
        for item in output_items:
            if (
                isinstance(item, dict)
                and item.get("type") == "message"
                and item.get("role") == "assistant"
            ):
                text = _content_text(item.get("content"))
                if text:
                    yield text


def _last_assistant_text(t: Trajectory) -> str:
    texts = list(_iter_assistant_texts(t))
    return texts[-1] if texts else ""


def _scan_non_tool_text(t: Trajectory) -> str:
    parts = []
    for m in _iter_messages(t):
        if m.get("role") == "tool":
            continue
        text = _content_text(m.get("content"))
        if text:
            parts.append(_trim_for_scan(text))
    sp = t.get("system_prompt")
    if isinstance(sp, str) and sp:
        parts.append(_trim_for_scan(sp))
    return "\n".join(parts).lower()


def _scan_non_tool_text_casesensitive(t: Trajectory) -> str:
    """Same scan as :func:`_scan_non_tool_text` but preserves original case.

    Needed for markers like ``[SLIENT]`` whose detection is case-sensitive
    (we want to match the literal upstream typo, not arbitrary mixed-case
    strings that happen to contain the letters).
    """
    parts = []
    for m in _iter_messages(t):
        if m.get("role") == "tool":
            continue
        text = _content_text(m.get("content"))
        if text:
            parts.append(_trim_for_scan(text))
    sp = t.get("system_prompt")
    if isinstance(sp, str) and sp:
        parts.append(_trim_for_scan(sp))
    return "\n".join(parts)


def rule_model_match(t: Trajectory) -> tuple[bool, Optional[str]]:
    if not model_matches(t.get("model") or ""):
        return False, "model_not_target_family"
    return True, None


def rule_has_tools(t: Trajectory) -> tuple[bool, Optional[str]]:
    tools = t.get("tools") or []
    if not isinstance(tools, list) or len(tools) == 0:
        return False, "no_tools"
    return True, None


def rule_min_messages(t: Trajectory) -> tuple[bool, Optional[str]]:
    messages = t.get("messages") or []
    if not isinstance(messages, list) or len(messages) <= MIN_MESSAGES_EXCLUSIVE:
        return False, f"messages_le_{MIN_MESSAGES_EXCLUSIVE}"
    return True, None


def rule_assistant_has_tool_call(t: Trajectory) -> tuple[bool, Optional[str]]:
    if not has_assistant_tool_call(t):
        return False, "no_assistant_tool_call"
    return True, None


def rule_stop_reason(t: Trajectory) -> tuple[bool, Optional[str]]:
    fmt = t.get("format") or "unknown"
    sr = t.get("stop_reason")
    if sr is None:
        return False, "stop_reason_missing"
    accepted = _ACCEPTED_STOP_REASONS.get(fmt)
    if accepted is None:
        accepted = {"end_turn", "stop"}
    if sr not in accepted:
        return False, f"stop_reason_{sr}"
    return True, None


def rule_has_system_prompt(t: Trajectory) -> tuple[bool, Optional[str]]:
    """R6 — record must carry a non-empty system / developer prompt.

    The trajectory's ``system_prompt`` field is populated by the format
    extractors (see :func:`extract_system_prompt`). Defensive fallback for
    trajectories built by hand: re-scan the underlying messages list.
    """
    sp = t.get("system_prompt")
    if isinstance(sp, str) and sp.strip():
        return True, None
    raw = t.get("_response") or {}
    if isinstance(raw, dict):
        recovered = extract_system_prompt(raw, t.get("messages") or [])
        if recovered.strip():
            return True, None
    return False, "no_system_prompt"


# Explicit silent / keep-alive markers. If ANY message in the trajectory
# (system / user / assistant / tool) contains one of these substrings,
# treat the whole record as keep-alive traffic and drop it.
#
# These markers come from the empirical noise list in
# ``tools/data_selection/filter_fused_split.py`` and
# ``tools/m2_ka_group_id/clean_samples.py``. The most common origin is
# OpenClaw / Hermes silent keep-alives where the assistant gets pinged
# with an empty ``[SLIENT]`` (sic — original typo preserved on purpose,
# since that is what the upstream logs actually contain) tool result
# to prevent the runtime from declaring the session dead.
_SILENT_KEEPALIVE_MARKERS: tuple[str, ...] = ("[SLIENT]", "[SILENT]")

# Heartbeat-poll fingerprints that show up in the USER (or ``tool``)
# channel rather than in the assistant final reply. If the trajectory
# is shaped like ``…[OpenClaw heartbeat poll] → HEARTBEAT_OK`` the
# original R7 catches it via the terminal HEARTBEAT_OK rule. But some
# upstreams strip the terminal reply / replace it with a model error;
# in that case we still want to drop the record because the request
# was unmistakably a heartbeat poll, not a real task. These patterns
# are intentionally narrow so they don't false-positive on engineering
# discussions about heartbeats (``rule_no_heartbeat_traffic`` already
# whitelists those by virtue of the terminal-reply check above).
_HEARTBEAT_USER_FINGERPRINTS: tuple[str, ...] = (
    "[openclaw heartbeat poll]",
    "[heartbeat:chill]",
    "read heartbeat.md",
    "continue the openclaw runtime event",
)


def rule_no_heartbeat_traffic(t: Trajectory) -> tuple[bool, Optional[str]]:
    """R7 — reject high-confidence non-task heartbeat / keep-alive traffic.

    Two flavours are detected:

      1. **Explicit silent markers.** Any message whose content contains
         ``[SLIENT]`` or ``[SILENT]`` is a runtime keep-alive ping. The
         whole record is dropped — these never contain task-solving work.
      2. **OpenClaw / EvoMap heartbeat polls.** The assistant's final
         reply is one of the canonical heartbeat-success strings
         (``HEARTBEAT_OK`` etc.) AND the upstream user channel carries a
         matching poll fingerprint (e.g. ``[OpenClaw heartbeat poll]``)
         OR the assistant has emitted ``HEARTBEAT_OK`` twice in a row.

    Ordinary engineering tasks that merely *discuss* the word "heartbeat"
    are kept — the rule requires a structural fingerprint, not a keyword
    match.
    """
    scan_text_cs = _scan_non_tool_text_casesensitive(t)
    for marker in _SILENT_KEEPALIVE_MARKERS:
        if marker in scan_text_cs:
            return False, "silent_keepalive_marker"

    last_norm = _norm(_last_assistant_text(t))
    scan_text = scan_text_cs.lower()

    # Heartbeat poll fingerprint in user / tool channel without needing a
    # terminal HEARTBEAT_OK — covers cases where the assistant reply was
    # truncated or replaced by an error string upstream.
    for fp in _HEARTBEAT_USER_FINGERPRINTS:
        if fp in scan_text:
            return False, "heartbeat_non_task_traffic"

    if not last_norm:
        return True, None

    if last_norm == "heartbeat_ok":
        assistant_heartbeat_replies = sum(
            1 for text in _iter_assistant_texts(t)
            if _norm(text) == "heartbeat_ok"
        )
        has_openclaw_poll = (
            "openclaw heartbeat poll" in scan_text
            or "[openclaw heartbeat poll]" in scan_text
            or "read heartbeat.md" in scan_text
            or "inside openclaw" in scan_text
            or "running inside openclaw" in scan_text
        )
        if has_openclaw_poll or assistant_heartbeat_replies >= 2:
            return False, "heartbeat_non_task_traffic"

    if (
        last_norm.startswith("evomap heartbeat completed successfully")
        or (
            last_norm.startswith("evomap heartbeat result")
            and ("http status" in last_norm or "http_status" in last_norm)
        )
    ):
        return False, "heartbeat_non_task_traffic"

    if last_norm.startswith("<heartbeat>") and "<decision>dont_notify" in last_norm:
        return False, "heartbeat_non_task_traffic"

    return True, None


RULES: list[tuple[str, Callable[[Trajectory], tuple[bool, Optional[str]]]]] = [
    ("R1_model_match",               rule_model_match),
    ("R2_has_tools",                 rule_has_tools),
    ("R3_min_messages",              rule_min_messages),
    ("R4_assistant_has_tool_call",   rule_assistant_has_tool_call),
    ("R5_stop_reason",               rule_stop_reason),
    ("R6_has_system_prompt",         rule_has_system_prompt),
    ("R7_no_heartbeat_traffic",      rule_no_heartbeat_traffic),
]


def evaluate(t: Trajectory) -> tuple[bool, list[str]]:
    """Evaluate every rule against ``t``.

    Returns ``(passed, reasons)`` where ``reasons`` is the list of *failing*
    reason codes (empty if ``passed``). All rules are always evaluated so we
    can report multi-reason failures; intentional, not a perf bug.
    """
    reasons: list[str] = []
    for _rule_id, fn in RULES:
        ok, why = fn(t)
        if not ok and why:
            reasons.append(why)
    return (len(reasons) == 0), reasons


# =============================================================================
# session_id — canonical-shape hashing for cross-record dedup
# =============================================================================
#
# Uses a stable OpenAI-Chat-shaped canonical conversion before hashing.
# Same hash inputs => same session_id, so this single-file build can report
# duplicate groups consistently across files and repeated runs.
#
# The hash input is constructed from:
#   1. canonical ``tools`` list, recursively-key-sorted then JSON-serialised
#   2. the "session prefix" of canonical ``messages`` — everything up to (and
#      including position 0 only) the first ``role==assistant`` turn
#   3. per message: role + reasoning_content + content + sorted tool_calls
#      (id field stripped from each tool_call)
#   4. ``role==tool`` messages have content truncated to TOOL_CONTENT_MAX_CHARS
#      before participating in the hash
#   5. ``<think>``/``</think>`` tags and whitespace stripped before MD5
#
# Canonicalisation: provider shapes (anthropic_messages / bedrock_converse /
# openai_responses) are flattened to OpenAI Chat shape — text + tool_calls +
# role==tool result messages — before hashing. openai_chat is already
# canonical (we only prepend system + append assistant response).

TOOL_CONTENT_MAX_CHARS = 1000


def _sort_dict_recursive(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _sort_dict_recursive(v) for k, v in sorted(obj.items())}
    if isinstance(obj, list):
        return [_sort_dict_recursive(item) for item in obj]
    return obj


def _normalize_text(text: str) -> str:
    if not text:
        return ""
    text = text.replace("<think>", "").replace("</think>", "")
    return text.replace(" ", "").replace("\n", "").replace("\r", "").replace("\t", "")


def _process_think_content(content: str) -> tuple[str, str]:
    if not content:
        return "", ""
    if "<think>" in content and "</think>" in content:
        reasoning = content.split("<think>")[1].split("</think>")[0].strip()
        response = content.split("</think>")[1].strip()
        if "<answer>" in response and "</answer>" in response:
            response = response.split("<answer>")[1].split("</answer>")[0].strip()
        return reasoning, response
    if "<answer>" in content and "</answer>" in content:
        content = content.split("<answer>")[1].split("</answer>")[0].strip()
    return "", content


def _normalize_messages_for_hash(messages: list[dict]) -> list[dict]:
    if not messages:
        return []
    normalized: list[dict] = [dict(m) for m in messages]
    for msg in normalized:
        role = msg.get("role")
        if role == "tool":
            content = msg.get("content")
            if isinstance(content, str) and len(content) > TOOL_CONTENT_MAX_CHARS:
                msg["content"] = content[:TOOL_CONTENT_MAX_CHARS]
            elif isinstance(content, list):
                serialized = json.dumps(content, ensure_ascii=False)
                if len(serialized) > TOOL_CONTENT_MAX_CHARS:
                    msg["content"] = serialized[:TOOL_CONTENT_MAX_CHARS]
            continue
        if role == "assistant":
            content = msg.get("content") or ""
            if isinstance(content, list):
                continue
            if "<think>" in content and "</think>" in content:
                reasoning, remaining = _process_think_content(content)
                if reasoning:
                    msg["reasoning_content"] = reasoning
                    msg["content"] = remaining
    return normalized


def _extract_session_prefix(messages: list[dict]) -> list[dict]:
    """Everything up to (and including position 0 only) the first assistant."""
    prefix: list[dict] = []
    for msg in messages:
        if msg.get("role") == "assistant":
            if not prefix:
                prefix.append(msg)
            else:
                break
        else:
            prefix.append(msg)
    return prefix


def _build_normalized_string(tools: list, messages_prefix: list[dict]) -> str:
    sorted_tools = [_sort_dict_recursive(tool) for tool in tools or []]
    sorted_tools = sorted(sorted_tools, key=lambda x: json.dumps(x, sort_keys=True))
    tools_json = json.dumps(sorted_tools, ensure_ascii=False, sort_keys=True)

    parts: list[str] = []
    for msg in messages_prefix:
        role = msg.get("role", "")
        reasoning_content = msg.get("reasoning_content", "")
        content = msg.get("content") or ""
        if isinstance(content, list):
            content = json.dumps(content, ensure_ascii=False)
        tool_calls = msg.get("tool_calls") or []

        part = role
        if reasoning_content:
            part += reasoning_content
        if content:
            part += content
        if tool_calls:
            cleaned = [{k: v for k, v in tc.items() if k != "id"} for tc in tool_calls]
            sorted_tc = [_sort_dict_recursive(tc) for tc in cleaned]
            sorted_tc = sorted(sorted_tc, key=lambda x: json.dumps(x, sort_keys=True))
            part += json.dumps(sorted_tc, ensure_ascii=False, sort_keys=True)
        parts.append(part)

    return _normalize_text(tools_json + "".join(parts))


def _filter_empty_anthropic_blocks(content: list) -> list:
    if not isinstance(content, list):
        return content
    out = []
    for block in content:
        if not isinstance(block, dict):
            out.append(block)
            continue
        btype = block.get("type")
        if btype == "thinking":
            if not (block.get("thinking") or "").strip():
                continue
        elif btype == "text":
            text_val = (block.get("text") or "").strip()
            if not text_val or text_val == "(no content)":
                continue
        out.append(block)
    return out


def _convert_anthropic_message(msg: dict) -> dict | list[dict]:
    """One Anthropic message → OpenAI Chat message (or list when tool_results
    fan out into role==tool messages)."""
    role = msg.get("role", "user")
    content = msg.get("content", "")
    if isinstance(content, str):
        return {"role": role, "content": content}
    if not isinstance(content, list):
        return {"role": role, "content": content}

    content = _filter_empty_anthropic_blocks(content)
    text_parts: list[str] = []
    tool_calls: list[dict] = []
    reasoning_parts: list[str] = []
    tool_results: list[dict] = []
    other_blocks: list[dict] = []

    for block in content:
        btype = block.get("type", "text")
        if btype == "text":
            text_parts.append(block.get("text", ""))
        elif btype == "thinking":
            t = block.get("thinking", "")
            if t:
                reasoning_parts.append(t)
        elif btype == "tool_use":
            tool_calls.append({
                "id": block.get("id", ""),
                "type": "function",
                "function": {
                    "name": block.get("name", ""),
                    "arguments": block.get("input", {}),
                },
            })
        elif btype == "tool_result":
            tc = block.get("content", "")
            if isinstance(tc, list):
                tc = "\n".join(
                    item.get("text", "")
                    for item in tc
                    if isinstance(item, dict) and item.get("type") == "text"
                )
            tool_results.append({
                "role": "tool",
                "tool_call_id": block.get("tool_use_id", ""),
                "content": tc,
            })
        else:
            other_blocks.append(block)

    if other_blocks:
        content_list = [b for b in content if b.get("type") != "thinking"]
        main: dict = {"role": role, "content": content_list}
        if reasoning_parts:
            main["reasoning_content"] = "\n\n".join(reasoning_parts)
        if tool_calls:
            main["tool_calls"] = tool_calls
        return [main, *tool_results] if tool_results else main

    main = {"role": role, "content": "\n".join(text_parts) if text_parts else None}
    if reasoning_parts:
        main["reasoning_content"] = "\n\n".join(reasoning_parts)
    if tool_calls:
        main["tool_calls"] = tool_calls
        if not main["content"]:
            main["content"] = None
    if tool_results:
        out: list[dict] = []
        if main["content"] or reasoning_parts or tool_calls:
            out.append(main)
        out.extend(tool_results)
        return out
    return main


def _convert_anthropic_tools(tools: list[dict]) -> list[dict]:
    out: list[dict] = []
    for tool in tools or []:
        out.append({
            "type": "function",
            "function": {
                "name": tool.get("name", ""),
                "description": tool.get("description", ""),
                "parameters": tool.get("input_schema") or tool.get("parameters") or {},
            },
        })
    return out


def canonicalize_trajectory(t: Trajectory) -> tuple[list[dict], list[dict]]:
    """Return (messages, tools) in canonical OpenAI Chat shape for hashing.

    The session_id MD5 inputs come straight from this output.
    """
    fmt = t.get("format") or "unknown"
    raw_messages = t.get("messages") or []
    system_prompt = t.get("system_prompt") or ""
    response = t.get("_response") or {}

    def _prepend_system(out: list[dict]) -> None:
        if system_prompt and not any(
            isinstance(m, dict) and m.get("role") == "system" for m in raw_messages
        ):
            out.insert(0, {"role": "system", "content": system_prompt})

    if fmt in ("anthropic_messages", "bedrock_converse"):
        messages: list[dict] = []
        for msg in raw_messages:
            if not isinstance(msg, dict):
                continue
            converted = _convert_anthropic_message(msg)
            if isinstance(converted, list):
                messages.extend(converted)
            else:
                messages.append(converted)
        # Append the assistant response from _response.content (or
        # _response.output.message.content for Bedrock Converse).
        resp_content = response.get("content") if isinstance(response, dict) else None
        if resp_content is None and isinstance(response, dict):
            out_dict = response.get("output") or {}
            if isinstance(out_dict, dict):
                inner = out_dict.get("message") or {}
                if isinstance(inner, dict):
                    resp_content = inner.get("content")
        if isinstance(resp_content, list):
            resp_content = _filter_empty_anthropic_blocks(resp_content)
            if resp_content:
                converted = _convert_anthropic_message(
                    {"role": "assistant", "content": resp_content}
                )
                if isinstance(converted, list):
                    messages.extend(converted)
                else:
                    messages.append(converted)
        _prepend_system(messages)
        tools = _convert_anthropic_tools(t.get("tools") or [])
        return messages, tools

    if fmt == "openai_chat":
        messages = [m for m in raw_messages if isinstance(m, dict)]
        choices = response.get("choices") if isinstance(response, dict) else None
        if isinstance(choices, list) and choices:
            first = choices[0] if isinstance(choices[0], dict) else {}
            assistant_msg = first.get("message")
            if isinstance(assistant_msg, dict):
                messages.append(assistant_msg)
        _prepend_system(messages)
        return messages, list(t.get("tools") or [])

    if fmt == "openai_responses":
        # raw_messages is the Responses ``input`` items already converted by
        # extract_openai_responses; output items live in response.output.
        messages = [m for m in raw_messages if isinstance(m, dict)]
        raw_output = response.get("output") if isinstance(response, dict) else None
        if isinstance(raw_output, list):
            text_parts: list[str] = []
            tool_calls: list[dict] = []
            reasoning_parts: list[str] = []
            for item in raw_output:
                if not isinstance(item, dict):
                    continue
                itype = item.get("type")
                if itype == "message":
                    inner = item.get("content")
                    if isinstance(inner, list):
                        for b in inner:
                            if isinstance(b, dict) and b.get("type") in (
                                "output_text", "text",
                            ):
                                text_parts.append(b.get("text", ""))
                    elif isinstance(inner, str):
                        text_parts.append(inner)
                elif itype == "function_call":
                    args = item.get("arguments", {})
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except (json.JSONDecodeError, TypeError):
                            pass
                    tool_calls.append({
                        "id": item.get("call_id") or item.get("id") or "",
                        "type": "function",
                        "function": {
                            "name": item.get("name", ""),
                            "arguments": args,
                        },
                    })
                elif itype == "reasoning":
                    summary = item.get("summary")
                    if isinstance(summary, list):
                        for s in summary:
                            if isinstance(s, dict) and s.get("type") in (
                                "summary_text", "text",
                            ):
                                reasoning_parts.append(s.get("text", ""))
            if text_parts or tool_calls or reasoning_parts:
                assistant: dict = {
                    "role": "assistant",
                    "content": "\n".join(text_parts) if text_parts else None,
                }
                if reasoning_parts:
                    assistant["reasoning_content"] = "\n\n".join(reasoning_parts)
                if tool_calls:
                    assistant["tool_calls"] = tool_calls
                messages.append(assistant)
        _prepend_system(messages)
        return messages, list(t.get("tools") or [])

    return [m for m in raw_messages if isinstance(m, dict)], list(t.get("tools") or [])


def compute_session_id(t: Trajectory) -> str | None:
    """Return ``session_<md5>`` for the trajectory's canonical shape.

    Returns ``None`` if the trajectory is unknown / unhashable (no messages).
    """
    messages, tools = canonicalize_trajectory(t)
    if not messages:
        return None
    normalized_messages = _normalize_messages_for_hash(messages)
    prefix = _extract_session_prefix(normalized_messages)
    prefix_str = _build_normalized_string(tools, prefix)
    return f"session_{hashlib.md5(prefix_str.encode('utf-8')).hexdigest()}"


# =============================================================================
# filter — CLI entry point
# =============================================================================

def _iter_files(in_dir: Path, glob_pattern: str) -> Iterable[Path]:
    yield from in_dir.rglob(glob_pattern)


def _load(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _format_breakdown(rows: list[dict]) -> dict:
    breakdown: dict[str, dict[str, int]] = {}
    for r in rows:
        fmt = r["format"]
        slot = breakdown.setdefault(fmt, {"total": 0, "kept": 0, "dropped": 0})
        slot["total"] += 1
        if r["passed"]:
            slot["kept"] += 1
        else:
            slot["dropped"] += 1
    return breakdown


def _reason_breakdown(rows: list[dict]) -> dict[str, int]:
    c: Counter = Counter()
    for r in rows:
        if r["passed"]:
            continue
        for reason in r["reasons"]:
            c[reason] += 1
    return dict(c.most_common())


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="filter_traj_multi_plat",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("input_dir", type=Path)
    p.add_argument("output_dir", type=Path, nargs="?", default=None,
                   help="If given, kept files are copied here preserving relative paths.")
    p.add_argument("--format-glob", default="*.json",
                   help="Filename pattern under input_dir (default: *.json). One JSON object per file.")
    p.add_argument("--report", type=Path, default=None,
                   help="Emit a JSON stats report at this path.")
    p.add_argument("--limit", type=int, default=None,
                   help="Stop after scanning this many files (debug aid).")
    p.add_argument("--strict-empty", action="store_true",
                   help="Exit non-zero if 0 files passed.")
    p.add_argument("--quiet", action="store_true",
                   help="Suppress the printed stats summary.")
    p.add_argument(
        "--dedup-mode",
        choices=("stats", "full", "off"),
        default="stats",
        help=(
            "How to handle session_id duplicates among rule-passing records. "
            "stats (default): compute session_id for every passing record, "
            "report the unique-session_id count + duplicate count, but do NOT "
            "actually drop duplicates — every rule-passing record is kept. "
            "This is the low-resource default, safe to run on a laptop. "
            "full: real dedup — within a session_id only the longest-messages "
            "record survives; losers are reported with reason "
            "session_id_duplicate. Needs O(unique_session_ids) memory. "
            "off: skip session_id computation entirely (fastest, no dedup "
            "stats in the report)."
        ),
    )
    p.add_argument(
        "--no-dedup",
        action="store_true",
        help="Deprecated alias for --dedup-mode=off. Kept for backward compatibility.",
    )
    args = p.parse_args(argv)

    if args.no_dedup:
        args.dedup_mode = "off"

    if not args.input_dir.is_dir():
        print(f"ERROR: input_dir is not a directory: {args.input_dir}", file=sys.stderr)
        return 2

    files = list(_iter_files(args.input_dir, args.format_glob))
    if args.limit is not None:
        files = files[: args.limit]

    rows: list[dict] = []
    parse_errors = 0
    # session_id → row index of the current "winner" (longest messages).
    # Used by --dedup-mode=full to demote prior winners as longer copies
    # arrive. Empty in stats / off modes.
    sid_winner: dict[str, int] = {}
    # session_id → count of rule-passing records carrying that hash.
    # Populated in stats / full modes; used to surface the dedup ratio
    # without dropping records in stats mode.
    sid_counts: Counter = Counter()
    for f in files:
        d = _load(f)
        if d is None:
            parse_errors += 1
            rows.append({
                "path": str(f.relative_to(args.input_dir)),
                "format": "parse_error",
                "model": "",
                "passed": False,
                "reasons": ["parse_error"],
                "session_id": None,
                "_msg_len": 0,
            })
            continue
        t = extract(d)
        passed, reasons = evaluate(t)
        session_id: str | None = None
        msg_len = 0
        if passed and args.dedup_mode != "off":
            # Only compute session_id for rule-passing records — that's all
            # dedup considers, and hashing skipped records is just wasted work.
            try:
                session_id = compute_session_id(t)
            except Exception:
                session_id = None
            msg_len = len(t.get("messages") or [])
            if session_id is not None:
                sid_counts[session_id] += 1
        rows.append({
            "path": str(f.relative_to(args.input_dir)),
            "format": t["format"],
            "model": t.get("model", ""),
            "passed": passed,
            "reasons": reasons,
            "session_id": session_id,
            "_msg_len": msg_len,
        })
        if passed and args.dedup_mode == "full" and session_id is not None:
            idx = len(rows) - 1
            cur = sid_winner.get(session_id)
            if cur is None or msg_len > rows[cur]["_msg_len"]:
                if cur is not None:
                    # Demote the previous winner.
                    rows[cur]["passed"] = False
                    rows[cur]["reasons"] = list(rows[cur]["reasons"]) + ["session_id_duplicate"]
                sid_winner[session_id] = idx
            else:
                rows[-1]["passed"] = False
                rows[-1]["reasons"] = list(rows[-1]["reasons"]) + ["session_id_duplicate"]

    # Now that dedup is done, copy kept files. We index rows by relative path
    # so we can map each surviving row back to its source file.
    if args.output_dir is not None:
        path_to_row = {r["path"]: r for r in rows}
        for f in files:
            rel = str(f.relative_to(args.input_dir))
            row = path_to_row.get(rel)
            if row is None or not row["passed"]:
                continue
            dst = args.output_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dst)

    kept = sum(1 for r in rows if r["passed"])
    total = len(rows)
    fmt_breakdown = _format_breakdown(rows)
    reason_breakdown = _reason_breakdown(rows)
    dedup_dropped = reason_breakdown.get("session_id_duplicate", 0)

    # Dedup-stats summary — number of unique session_ids among rule-passing
    # records, and how many rule-passers would collapse to a single record
    # if --dedup-mode=full were used. Populated even in stats mode so the
    # operator can decide whether to invest in real dedup downstream.
    unique_sids = len(sid_counts) if sid_counts else 0
    dup_groups = sum(1 for c in sid_counts.values() if c > 1)
    would_collapse = sum(c - 1 for c in sid_counts.values() if c > 1)

    if not args.quiet:
        print(f"scanned: {total}")
        print(f"kept:    {kept}")
        if args.output_dir is not None:
            print(f"output:  {args.output_dir}")
        if parse_errors:
            print(f"parse_errors: {parse_errors}")
        if args.dedup_mode == "full" and dedup_dropped:
            print(f"dedup_dropped: {dedup_dropped}")
        if args.dedup_mode == "stats" and sid_counts:
            print(
                f"dedup_stats: unique_session_ids={unique_sids}, "
                f"dup_groups={dup_groups}, would_collapse={would_collapse} "
                f"(use --dedup-mode=full to actually drop duplicates)"
            )
        if fmt_breakdown:
            print("\n=== per-format ===")
            print(f"{'format':<22} {'total':>7} {'kept':>7} {'dropped':>9}")
            for fmt in sorted(fmt_breakdown):
                row = fmt_breakdown[fmt]
                print(f"{fmt:<22} {row['total']:>7} {row['kept']:>7} {row['dropped']:>9}")
        if reason_breakdown:
            print("\n=== drop reasons (top 20) ===")
            for reason, n in list(reason_breakdown.items())[:20]:
                print(f"  {n:>6}  {reason}")

    if args.report is not None:
        report = {
            "input_dir":   str(args.input_dir),
            "output_dir":  str(args.output_dir) if args.output_dir else None,
            "scanned":     total,
            "kept":        kept,
            "parse_errors": parse_errors,
            "dedup_mode": args.dedup_mode,
            # Legacy flag for backward-compat report consumers — equivalent
            # to "did we actually drop duplicates?".
            "dedup_enabled": args.dedup_mode == "full",
            "dedup_dropped": dedup_dropped,
            "dedup_stats": {
                "unique_session_ids": unique_sids,
                "dup_groups": dup_groups,
                "would_collapse": would_collapse,
            } if sid_counts else None,
            "per_format":  fmt_breakdown,
            "drop_reasons": reason_breakdown,
        }
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False))
        if not args.quiet:
            print(f"\nreport: {args.report}")

    if args.strict_empty and kept == 0:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
