"""Workloop attributes shared by every trovis-agents platform adapter.

Named Work on the Trovis dashboard is a loop whose *creating* span carried
`trovis.loop.title`. Ingest stamps `title_source=provided` at INSERT only
(adopt-onto-open-untitled is a backend follow-on, not this package).

This module:

  - normalizes a short human title (whitespace collapse, 80-char cap,
    reject generic SDK defaults like "Agent workflow")
  - queues an explicit title / handoff for the next span an adapter emits
  - applies `trovis.run.id` / `trovis.loop.external_id` / title / handoff
    attrs onto a span (empty values are omitted — never "")

Privacy: titles derived from user prompts or first messages follow the
same opt-in as content capture (`capture_outputs` / `TROVIS_CAPTURE_OUTPUTS`).
Adapters must not copy private prompt text into `trovis.loop.title` unless
capture is on, or the caller used `set_loop_title()` (explicit).
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Optional

logger = logging.getLogger("trovis")

# Match the OpenClaw plugin's title budget.
TITLE_CHAR_LIMIT = 80

# SDK-default workflow names. Emitting these would flood Named Work with
# shells, which the lean /work/items contract excludes (and a backend
# follow-on will reject at ingest). Skip rather than stamp.
_GENERIC_TITLES = frozenset(
    {
        "agent workflow",
        "agent run",
        "workflow",
        "run",
        "agent",
        "default",
        "untitled",
    }
)

HANDOFF_DIRECTIONS = ("to_human", "to_agent", "to_system")

_pending_title: Optional[str] = None
_pending_handoff: Optional[dict[str, str]] = None


def human_title(raw: Any) -> Optional[str]:
    """Collapse whitespace, trim, cap at 80 chars. None if empty or generic."""
    if raw is None:
        return None
    text = " ".join(str(raw).split()).strip()
    if not text:
        return None
    text = text[:TITLE_CHAR_LIMIT]
    if text.lower() in _GENERIC_TITLES:
        return None
    return text


def first_user_text(value: Any) -> Optional[str]:
    """Best-effort first user-task string from an SDK input payload.

    Handles a bare string, OpenAI-style message lists
    (`[{role, content}]`), and objects with `.content` / `.text`.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return human_title(value)
    if isinstance(value, list):
        for item in value:
            text = first_user_text(item)
            if text:
                return text
        return None
    if isinstance(value, dict):
        role = value.get("role")
        if role not in (None, "user"):
            return None
        return first_user_text(value.get("content") or value.get("text"))
    content = getattr(value, "content", None)
    if content is not None and content is not value:
        return first_user_text(content)
    text = getattr(value, "text", None)
    if isinstance(text, str):
        return human_title(text)
    return None


def set_loop_title(title: str) -> Optional[str]:
    """Queue a human title for the next Trovis span this process emits.

    Use this when you have a task name that isn't private user content,
    or when `capture_outputs` is off but the run should still land as
    named Work. The title is applied to the next creating/run span the
    platform adapter emits (ingest reads it only at loop INSERT).

    Returns the normalized title, or None if the input was empty/generic.
    """
    global _pending_title
    normalized = human_title(title)
    if not normalized:
        logger.debug("[Trovis] set_loop_title: empty or generic title — ignored")
        return None
    _pending_title = normalized
    return normalized


def mark_handoff(
    direction: str = "to_human",
    target: Optional[str] = None,
    reason: Optional[str] = None,
) -> Optional[str]:
    """Queue a workloop handoff for the next Trovis span.

    `direction` is `to_human`, `to_agent`, or `to_system`. Returns the
    generated handoff id, or None if the direction is invalid (warned,
    no-op — agent code never raises).
    """
    global _pending_handoff
    if direction not in HANDOFF_DIRECTIONS:
        logger.warning(
            "[Trovis] mark_handoff: direction must be %s (got %r) — ignored",
            "/".join(HANDOFF_DIRECTIONS),
            direction,
        )
        return None
    hid = str(uuid.uuid4())
    payload: dict[str, str] = {"direction": direction, "id": hid}
    if target and str(target).strip():
        payload["target"] = str(target).strip()
    if reason and str(reason).strip():
        payload["reason"] = str(reason).strip()
    _pending_handoff = payload
    return hid


def consume_pending_title() -> Optional[str]:
    """Return and clear the queued title (one-shot)."""
    global _pending_title
    title = _pending_title
    _pending_title = None
    return title


def consume_pending_handoff() -> Optional[dict[str, str]]:
    """Return and clear the queued handoff (one-shot)."""
    global _pending_handoff
    h = _pending_handoff
    _pending_handoff = None
    return h


def peek_pending_title() -> Optional[str]:
    return _pending_title


def apply_loop_attrs(
    span: Any,
    *,
    run_id: Optional[str] = None,
    external_id: Optional[str] = None,
    title: Optional[str] = None,
    consume_title: bool = True,
    consume_handoff: bool = True,
) -> None:
    """Stamp workloop attrs onto an OTEL span. Empty values are omitted."""
    if consume_title:
        # Explicit set_loop_title() wins over a content-derived title —
        # the operator opted into a name without sending the prompt.
        pending = consume_pending_title()
        if pending:
            title = pending
    rid = _nonempty(run_id)
    if rid:
        span.set_attribute("trovis.run.id", rid)
    eid = _nonempty(external_id)
    if eid:
        span.set_attribute("trovis.loop.external_id", eid)
    normalized = human_title(title)
    if normalized:
        span.set_attribute("trovis.loop.title", normalized)
    if consume_handoff:
        _apply_handoff(span, consume_pending_handoff())


def apply_handoff_attrs(
    span: Any,
    *,
    direction: str,
    target: Optional[str] = None,
    reason: Optional[str] = None,
    handoff_id: Optional[str] = None,
) -> Optional[str]:
    """Stamp trovis.handoff.* onto a span that the SDK surfaced as a handoff."""
    if direction not in HANDOFF_DIRECTIONS:
        return None
    hid = _nonempty(handoff_id) or str(uuid.uuid4())
    span.set_attribute("trovis.handoff.direction", direction)
    span.set_attribute("trovis.handoff.id", hid)
    tgt = _nonempty(target)
    if tgt:
        span.set_attribute("trovis.handoff.target_id", tgt)
    rsn = _nonempty(reason)
    if rsn:
        span.set_attribute("trovis.handoff.reason", rsn)
    return hid


def _apply_handoff(span: Any, h: Optional[dict[str, str]]) -> None:
    if not h:
        return
    apply_handoff_attrs(
        span,
        direction=h["direction"],
        target=h.get("target"),
        reason=h.get("reason"),
        handoff_id=h.get("id"),
    )


def _nonempty(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _reset_for_tests() -> None:
    global _pending_title, _pending_handoff
    _pending_title = None
    _pending_handoff = None
