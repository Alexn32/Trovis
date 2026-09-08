"""Shared SaaS → Work-event spine.

Many transports (Stripe today; HubSpot later) collapse onto one Work Event.
This module is the only writer of those effects. Adapters verify, map, and
hand a structured event here. The spine then:

  1. Requires an explicit metadata link key. No key → no-op (log).
  2. Resolves that key to an **open** loop on this Trovis account.
     No match → ignore the event. Never invent a loop from SaaS alone.
  3. Applies wait / clear / stuck via existing loop_events
     (to_system handoff toward the SaaS holder).

V1 link keys (require one; first present wins):
  trovis_loop_external_id | trovis.loop.external_id |
  trovis_run_id | trovis.run.id
  → open loop on this Trovis account; else no-op.

Effects:
  wait  — unresolved to_system (lean holder kind tool) toward the SaaS.
  clear — resolve that SaaS wait so work can move.
  stuck — needs human attention (failure / dispute) on the same loop.
"""
from __future__ import annotations

import logging
from typing import Any

import database

logger = logging.getLogger("trovis.saas")

EFFECT_WAIT = "wait"
EFFECT_CLEAR = "clear"
EFFECT_STUCK = "stuck"
EFFECTS = frozenset({EFFECT_WAIT, EFFECT_CLEAR, EFFECT_STUCK})

# Preferred first. Underscore and dotted forms are both accepted because
# Stripe metadata keys cannot contain dots in some Dashboard UIs, while
# agents emitting OTEL-shaped keys will send the dotted form.
LINK_KEYS = (
    "trovis_loop_external_id",
    "trovis.loop.external_id",
    "trovis_run_id",
    "trovis.run.id",
)

PROVIDER_LABELS = {
    "stripe": "Stripe",
}


def extract_link_key(metadata: Any) -> str | None:
    """Return the first non-empty V1 link key from a metadata dict, or None.

    Callers must pass the object's own metadata. The spine never invents a
    key from surrounding Stripe/HubSpot fields.
    """
    if not isinstance(metadata, dict):
        return None
    for key in LINK_KEYS:
        raw = metadata.get(key)
        if raw is None:
            continue
        val = str(raw).strip()
        if val:
            return val
    return None


def metadata_of(obj: Any) -> dict[str, Any]:
    """Pull a metadata dict off a PaymentIntent/Invoice/Charge/Dispute-shaped
    object. Missing / non-dict → empty (caller then no-ops)."""
    if not isinstance(obj, dict):
        return {}
    meta = obj.get("metadata")
    return meta if isinstance(meta, dict) else {}


def apply_work_effect(
    account_id: int,
    *,
    provider: str,
    effect: str,
    object_id: str | None = None,
    waiting_on: str | None = None,
    reason: str | None = None,
    event_id: str | None = None,
    event_type: str | None = None,
    event_time_unix: int | None = None,
    metadata: dict[str, Any] | None = None,
    link_key: str | None = None,
) -> dict[str, Any]:
    """Attach a verified SaaS event to an existing open Work loop.

    Returns a status dict (never raises on a business no-op):

      ignored_no_metadata / ignored_no_loop / ignored_unknown_effect
      ignored_duplicate / applied / noop_already
    """
    provider = (provider or "").strip().lower()
    effect = (effect or "").strip().lower()
    if effect not in EFFECTS:
        logger.info(
            "[saas] unknown effect=%r provider=%s event=%s — ignore",
            effect, provider, event_id,
        )
        return {"status": "ignored_unknown_effect", "effect": effect}

    if event_id and not database.claim_saas_event(
        account_id, provider, event_id, event_type=event_type,
    ):
        logger.info(
            "[saas] duplicate event provider=%s event=%s — ignore",
            provider, event_id,
        )
        return {"status": "ignored_duplicate", "event_id": event_id}

    key = (link_key or "").strip() or extract_link_key(metadata)
    if not key:
        logger.info(
            "[saas] no metadata link key provider=%s event=%s type=%s — no-op",
            provider, event_id, event_type,
        )
        return {"status": "ignored_no_metadata", "event_id": event_id}

    loop = database.find_open_loop_by_external_id(account_id, key)
    if loop is None:
        logger.info(
            "[saas] no open loop for key=%r account=%s provider=%s event=%s — ignore",
            key, account_id, provider, event_id,
        )
        return {
            "status": "ignored_no_loop",
            "event_id": event_id,
            "link_key": key,
        }

    loop_id = int(loop["id"])
    object_id = (object_id or "").strip() or None
    label = PROVIDER_LABELS.get(provider, provider)
    result = database.apply_saas_loop_effect(
        account_id,
        loop_id,
        provider=provider,
        effect=effect,
        object_id=object_id,
        target_id=label,
        waiting_on=waiting_on,
        reason=reason,
        event_id=event_id,
        event_type=event_type,
        event_time_unix=event_time_unix,
    )
    logger.info(
        "[saas] %s effect=%s provider=%s loop=%s object=%s event=%s type=%s",
        result.get("status"), effect, provider, loop_id, object_id,
        event_id, event_type,
    )
    result["loop_id"] = loop_id
    result["link_key"] = key
    result["effect"] = effect
    return result
