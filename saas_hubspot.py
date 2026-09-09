"""HubSpot SaaS adapter — OAuth, webhook verify, deal/ticket → Work map.

Reuses ``saas.apply_work_effect`` and ``saas_connections`` from Stripe PR A
(#143). Never invents a loop. No CRM / contact / catalog sync. Isolated from
``/billing/webhook`` and Stripe secrets.

Connect mapping contract (V1, folded here — surviving HubSpot PR B):

  Link metadata/properties (require one, first present wins) on the deal
  or ticket:
    trovis_loop_external_id | trovis.loop.external_id |
    trovis_run_id | trovis.run.id
    → open loop on this Trovis account; else no-op. Never invent a loop.
    No CRM sync.

  Events (minimal deal + ticket propertyChange only):
    dealstage waiting / pending-style  → wait
    dealstage closed won               → clear
    dealstage closed lost              → stuck
    ticket waiting (on contact/us/agent) → wait
    ticket solved / closed             → clear
    ticket escalated / failed hold     → stuck

  Locks:
    reuse #143 SaaS spine (saas.py / apply_work_effect / saas_connections);
    HubSpot brand coming→live only after E2E;
    no Intercom / Slack / GitHub / Shopify adapters;
    never touch /billing/webhook or Stripe SaaS secrets.

Env (read live, never cached):
  HUBSPOT_SAAS_CLIENT_ID
  HUBSPOT_SAAS_CLIENT_SECRET   — OAuth + webhook HMAC (v3 / v1)
  HUBSPOT_SAAS_REDIRECT_URI    — optional override of the OAuth callback
  HUBSPOT_SAAS_WEBHOOK_URI     — optional public URL HubSpot signed (v3)
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

import database
import saas

logger = logging.getLogger("trovis.saas.hubspot")

PROVIDER = "hubspot"
AUTHORIZE_URL = "https://app.hubspot.com/oauth/authorize"
TOKEN_URL = "https://api.hubapi.com/oauth/v1/token"
TOKEN_INFO_URL = "https://api.hubapi.com/oauth/v1/access-tokens"
CRM_OBJECTS_URL = "https://api.hubapi.com/crm/v3/objects"
PIPELINES_URL = "https://api.hubapi.com/crm/v3/pipelines"
# Read-only CRM objects + pipeline labels so we can resolve custom stage ids.
# No contacts / companies — this adapter is not CRM sync.
OAUTH_SCOPES = (
    "crm.objects.deals.read "
    "crm.objects.tickets.read "
    "crm.schemas.deals.read "
    "crm.schemas.tickets.read"
)
_SIG_TOLERANCE_MS = 300_000

# HubSpot objectTypeId → CRM object path.
_OBJECT_TYPE_IDS = {
    "0-3": "deals",
    "0-5": "tickets",
}

# Property we listen to per object.
DEAL_STAGE_PROPERTY = "dealstage"
TICKET_STAGE_PROPERTY = "hs_pipeline_stage"

# Properties fetched so the spine can find a loop key. Dotted names are
# requested too — HubSpot usually stores the underscore form.
_LINK_PROPERTIES = list(saas.LINK_KEYS)

# Default Sales Pipeline internal ids (HubSpot docs / a fresh portal).
# Unlisted default stages (appointmentscheduled, qualifiedtobuy, …) are
# pipeline progress and stay unmapped.
DEAL_WAIT_IDS = frozenset({
    "contractsent",
    "pending",
    "waiting",
    "wait",
    "payment",
    "paymentpending",
    "awaitingpayment",
    "invoiced",
    "invoicesent",
})
DEAL_CLEAR_IDS = frozenset({
    "closedwon",
    "won",
    "completed",
    "complete",
})
DEAL_STUCK_IDS = frozenset({
    "closedlost",
    "lost",
})

# Default Support Pipeline numeric ids. Only used when we have no label —
# custom pipelines reuse 1/2/3/4, so label match wins when present.
TICKET_WAIT_IDS = frozenset({"2", "3"})
TICKET_CLEAR_IDS = frozenset({"4"})
TICKET_DEFAULT_LABELS = {
    "1": "New",
    "2": "Waiting on contact",
    "3": "Waiting on us",
    "4": "Closed",
}
DEAL_DEFAULT_LABELS = {
    "contractsent": "Contract Sent",
    "closedwon": "Closed Won",
    "closedlost": "Closed Lost",
}

# Label / token patterns (custom pipelines).
_WAIT_LABEL_RE = re.compile(
    r"\b(waiting|pending|payment|contract\s*sent|invoice(?:d| sent)?)\b",
    re.I,
)
_CLEAR_LABEL_RE = re.compile(
    r"\b(closed\s*won|completed?|solved|closed)\b",
    re.I,
)
_STUCK_DEAL_LABEL_RE = re.compile(r"\b(closed\s*lost|lost)\b", re.I)
# Ticket stuck is escalated / failed hold only — bare on-hold is unmapped.
_STUCK_TICKET_LABEL_RE = re.compile(
    r"\b(escalat(?:e|ed|ion)|"
    r"(?:on[\s-]?hold|hold)\s+fail(?:ure|ed)?|"
    r"fail(?:ure|ed)\s+(?:on[\s-]?hold|hold)|"
    r"failed\s*hold)\b",
    re.I,
)
_TICKET_STUCK_IDS = frozenset({
    "escalated", "escalation", "onholdfailure", "failedhold", "holdfailure",
})


class HubSpotSaaSError(RuntimeError):
    """OAuth or webhook verify failed."""


class HubSpotSaaSNotConfigured(HubSpotSaaSError):
    """SaaS HubSpot env is missing. Fail closed for verify / OAuth start."""


class HubSpotSaaSUnauthorized(HubSpotSaaSError):
    """Access token rejected (401). Caller may refresh once."""


def client_id() -> str | None:
    return os.getenv("HUBSPOT_SAAS_CLIENT_ID") or None


def client_secret() -> str | None:
    return os.getenv("HUBSPOT_SAAS_CLIENT_SECRET") or None


def oauth_configured() -> bool:
    return bool(client_id() and client_secret())


def webhook_configured() -> bool:
    return bool(client_secret())


def authorize_url(*, redirect_uri: str, state: str) -> str:
    if not oauth_configured():
        raise HubSpotSaaSNotConfigured("HubSpot SaaS OAuth is not configured")
    qs = urllib.parse.urlencode(
        {
            "client_id": client_id(),
            "redirect_uri": redirect_uri,
            "scope": OAUTH_SCOPES,
            "state": state,
        }
    )
    return f"{AUTHORIZE_URL}?{qs}"


def _oauth_token_post(fields: dict[str, str]) -> dict[str, Any]:
    """POST to HubSpot's token endpoint. Tests patch this."""
    if not oauth_configured():
        raise HubSpotSaaSNotConfigured("HubSpot SaaS OAuth is not configured")
    body = urllib.parse.urlencode(fields).encode("utf-8")
    req = urllib.request.Request(
        TOKEN_URL,
        data=body,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:300]
        raise HubSpotSaaSError(f"HubSpot OAuth token exchange failed: {detail}") from e
    except urllib.error.URLError as e:
        raise HubSpotSaaSError(f"HubSpot OAuth token exchange failed: {e}") from e
    try:
        data = json.loads(raw)
    except (TypeError, ValueError) as e:
        raise HubSpotSaaSError("HubSpot OAuth returned non-JSON") from e
    if not isinstance(data, dict) or not data.get("access_token"):
        raise HubSpotSaaSError("HubSpot OAuth returned no access_token")
    return data


def _oauth_token_exchange(code: str, redirect_uri: str) -> dict[str, Any]:
    return _oauth_token_post(
        {
            "grant_type": "authorization_code",
            "client_id": client_id() or "",
            "client_secret": client_secret() or "",
            "redirect_uri": redirect_uri,
            "code": code,
        }
    )


def _refresh_access_token(refresh_token: str) -> dict[str, Any]:
    return _oauth_token_post(
        {
            "grant_type": "refresh_token",
            "client_id": client_id() or "",
            "client_secret": client_secret() or "",
            "refresh_token": refresh_token,
        }
    )


def _fetch_token_info(access_token: str) -> dict[str, Any]:
    """Resolve hub_id (portal id) from an access token. Tests patch this."""
    token = (access_token or "").strip()
    if not token:
        raise HubSpotSaaSError("missing access token")
    req = urllib.request.Request(
        f"{TOKEN_INFO_URL}/{urllib.parse.quote(token)}",
        method="GET",
        headers={"Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:300]
        raise HubSpotSaaSError(f"HubSpot token info failed: {detail}") from e
    except urllib.error.URLError as e:
        raise HubSpotSaaSError(f"HubSpot token info failed: {e}") from e
    try:
        data = json.loads(raw)
    except (TypeError, ValueError) as e:
        raise HubSpotSaaSError("HubSpot token info returned non-JSON") from e
    if not isinstance(data, dict) or data.get("hub_id") in (None, ""):
        raise HubSpotSaaSError("HubSpot token info returned no hub_id")
    return data


def exchange_code(code: str, *, redirect_uri: str) -> dict[str, Any]:
    """Exchange an OAuth code. Returns token payload + hub_id (no DB write)."""
    code = (code or "").strip()
    if not code:
        raise HubSpotSaaSError("missing OAuth code")
    tokens = _oauth_token_exchange(code, redirect_uri)
    info = _fetch_token_info(str(tokens.get("access_token") or ""))
    tokens["hub_id"] = str(info["hub_id"])
    if info.get("hub_domain"):
        tokens["hub_domain"] = info["hub_domain"]
    return tokens


def _compact(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _sign_v3(method: str, uri: str, payload: bytes, timestamp: str, secret: str) -> str:
    source = f"{method.upper()}{uri}{payload.decode('utf-8')}{timestamp}"
    digest = hmac.new(
        secret.encode("utf-8"), source.encode("utf-8"), hashlib.sha256,
    ).digest()
    return base64.b64encode(digest).decode("ascii")


def _sign_v1(payload: bytes, secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8") + bytes(payload)).hexdigest()


def parse_webhook_events(
    payload: bytes,
    *,
    signature_v3: str | None = None,
    signature_v1: str | None = None,
    timestamp: str | None = None,
    method: str = "POST",
    uri: str = "",
) -> list[dict[str, Any]]:
    """Verify HubSpot signature over the RAW body. Fail closed.

    Prefers v3 (HMAC-SHA256 + timestamp). Accepts v1 (SHA-256 of
    secret+body) when v3 headers are absent — still the app client secret,
    never a Stripe billing secret.
    """
    secret = client_secret()
    if not secret:
        raise HubSpotSaaSNotConfigured("HubSpot SaaS client secret is not configured")
    if not isinstance(payload, (bytes, bytearray)):
        raise HubSpotSaaSError("webhook payload must be raw bytes")

    v3 = (signature_v3 or "").strip()
    v1 = (signature_v1 or "").strip()
    if v3:
        ts = (timestamp or "").strip()
        if not ts:
            raise HubSpotSaaSError("missing X-HubSpot-Request-Timestamp")
        try:
            ts_ms = int(ts)
        except (TypeError, ValueError) as e:
            raise HubSpotSaaSError("invalid HubSpot timestamp") from e
        if abs(int(time.time() * 1000) - ts_ms) > _SIG_TOLERANCE_MS:
            raise HubSpotSaaSError("HubSpot signature timestamp is too old")
        expected = _sign_v3(method or "POST", uri or "", bytes(payload), ts, secret)
        if not hmac.compare_digest(expected, v3):
            raise HubSpotSaaSError("invalid HubSpot webhook signature")
    elif v1:
        expected = _sign_v1(bytes(payload), secret)
        if not hmac.compare_digest(expected, v1):
            raise HubSpotSaaSError("invalid HubSpot webhook signature")
    else:
        raise HubSpotSaaSError("missing HubSpot signature header")

    try:
        parsed = json.loads(bytes(payload).decode("utf-8"))
    except (TypeError, ValueError) as e:
        raise HubSpotSaaSError(f"invalid HubSpot webhook payload: {e}") from e
    if isinstance(parsed, list):
        return [e for e in parsed if isinstance(e, dict)]
    if isinstance(parsed, dict):
        return [parsed]
    raise HubSpotSaaSError("HubSpot webhook payload must be an object or array")


def _subscription_object(event: dict[str, Any]) -> str | None:
    """Return 'deals' or 'tickets', else None (contacts/companies ignored)."""
    stype = str(event.get("subscriptionType") or "").strip().lower()
    if stype.startswith("deal."):
        return "deals"
    if stype.startswith("ticket."):
        return "tickets"
    type_id = str(event.get("objectTypeId") or "").strip()
    return _OBJECT_TYPE_IDS.get(type_id)


def _stage_property_for(object_type: str) -> str:
    return DEAL_STAGE_PROPERTY if object_type == "deals" else TICKET_STAGE_PROPERTY


def _is_watched_property(event: dict[str, Any], object_type: str) -> bool:
    stype = str(event.get("subscriptionType") or "").strip().lower()
    pname = str(event.get("propertyName") or "").strip().lower()
    if stype and not (
        stype.endswith(".propertychange") or stype.endswith("propertychange")
    ):
        return False
    watched = _stage_property_for(object_type)
    if pname and pname != watched:
        return False
    # Missing propertyName + a propertyChange type: still try (legacy payloads).
    return True


def map_stage(object_type: str, stage_id: Any, stage_label: str | None = None) -> str | None:
    """Map a dealstage / ticket pipeline stage per the folded V1 contract.

    Deals: waiting/pending → wait; closed won → clear; closed lost → stuck.
    Tickets: waiting → wait; solved/closed → clear; escalated/failed hold → stuck.
    """
    raw = str(stage_id or "").strip()
    label = str(stage_label or "").strip()
    compact = _compact(raw)
    compact_label = _compact(label)
    kind = (object_type or "").strip().lower()

    if kind == "deals":
        # Specific outcomes first so "closed won" is never a wait.
        if compact in DEAL_CLEAR_IDS or compact_label in DEAL_CLEAR_IDS:
            return saas.EFFECT_CLEAR
        if label and _CLEAR_LABEL_RE.search(label) and not _STUCK_DEAL_LABEL_RE.search(label):
            if re.search(r"closed\s*won|completed?", label, re.I):
                return saas.EFFECT_CLEAR
        if compact in DEAL_STUCK_IDS or compact_label in DEAL_STUCK_IDS:
            return saas.EFFECT_STUCK
        if label and _STUCK_DEAL_LABEL_RE.search(label):
            return saas.EFFECT_STUCK
        if compact in DEAL_WAIT_IDS or compact_label in DEAL_WAIT_IDS:
            return saas.EFFECT_WAIT
        if label and _WAIT_LABEL_RE.search(label):
            return saas.EFFECT_WAIT
        return None

    if kind == "tickets":
        if label and _STUCK_TICKET_LABEL_RE.search(label):
            return saas.EFFECT_STUCK
        if compact_label in _TICKET_STUCK_IDS or compact in _TICKET_STUCK_IDS:
            return saas.EFFECT_STUCK
        if label and re.search(r"\b(solved|closed|completed?)\b", label, re.I):
            return saas.EFFECT_CLEAR
        if compact in TICKET_CLEAR_IDS or compact_label in {"closed", "solved", "complete", "completed"}:
            return saas.EFFECT_CLEAR
        if label and re.search(
            r"waiting\s+on\s+(contact|customer|us|agent)|waiting|pending",
            label, re.I,
        ):
            return saas.EFFECT_WAIT
        if compact in TICKET_WAIT_IDS or compact_label in {
            "waiting", "pending", "waitingoncontact", "waitingoncustomer",
            "waitingonus", "waitingonagent",
        }:
            return saas.EFFECT_WAIT
        return None

    return None


def _effect_copy(object_type: str, effect: str, stage_id: str, stage_label: str | None) -> tuple[str | None, str | None]:
    defaults = DEAL_DEFAULT_LABELS if object_type == "deals" else TICKET_DEFAULT_LABELS
    label = (stage_label or defaults.get(str(stage_id), "") or stage_id or "").strip()
    if effect == saas.EFFECT_WAIT:
        if object_type == "tickets":
            waiting = label or "ticket"
            return waiting, f"waiting on {waiting}"
        waiting = label or "deal stage"
        return waiting, f"waiting on {waiting}"
    if effect == saas.EFFECT_STUCK:
        if object_type == "tickets":
            reason = label or "ticket needs attention"
            return reason, reason
        reason = label or "deal lost"
        return reason, reason
    return None, "cleared"


def _http_json(url: str, access_token: str) -> tuple[int, Any]:
    req = urllib.request.Request(
        url,
        method="GET",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8")
            status = getattr(resp, "status", 200)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")[:300]
    except urllib.error.URLError:
        return 599, None
    try:
        return status, json.loads(raw)
    except (TypeError, ValueError):
        return status, None


def _fetch_crm_properties(
    access_token: str, object_type: str, object_id: str,
    extra_properties: list[str] | None = None,
) -> dict[str, Any]:
    """GET deal/ticket properties. Tests patch this. Empty dict on failure."""
    props = list(_LINK_PROPERTIES)
    props.append(_stage_property_for(object_type))
    if extra_properties:
        props.extend(extra_properties)
    # Dedupe, keep order.
    seen: set[str] = set()
    ordered = []
    for p in props:
        if p and p not in seen:
            seen.add(p)
            ordered.append(p)
    qs = urllib.parse.urlencode({"properties": ",".join(ordered)})
    url = f"{CRM_OBJECTS_URL}/{urllib.parse.quote(object_type)}/{urllib.parse.quote(str(object_id))}?{qs}"
    status, data = _http_json(url, access_token)
    if status == 401:
        raise HubSpotSaaSUnauthorized("HubSpot CRM returned 401")
    if status != 200 or not isinstance(data, dict):
        logger.info(
            "[saas.hubspot] fetch %s/%s failed status=%s — no properties",
            object_type, object_id, status,
        )
        return {}
    props_out = data.get("properties")
    return props_out if isinstance(props_out, dict) else {}


def _fetch_stage_label(
    access_token: str, object_type: str, stage_id: str,
) -> str | None:
    """Resolve a pipeline stage id to its label. Tests patch this."""
    sid = str(stage_id or "").strip()
    if not sid:
        return None
    url = f"{PIPELINES_URL}/{urllib.parse.quote(object_type)}"
    status, data = _http_json(url, access_token)
    if status == 401:
        raise HubSpotSaaSUnauthorized("HubSpot pipelines returned 401")
    if status != 200 or not isinstance(data, dict):
        return None
    results = data.get("results")
    if not isinstance(results, list):
        return None
    for pipe in results:
        if not isinstance(pipe, dict):
            continue
        stages = pipe.get("stages")
        if not isinstance(stages, list):
            continue
        for stage in stages:
            if not isinstance(stage, dict):
                continue
            if str(stage.get("id") or "").strip() == sid:
                label = stage.get("label")
                return str(label) if label else None
    return None


def _refresh_connection_token(secrets: dict[str, Any]) -> str | None:
    refresh = (secrets.get("refresh_token") or "").strip()
    if not refresh:
        return None
    try:
        tokens = _refresh_access_token(refresh)
    except HubSpotSaaSError as e:
        logger.info("[saas.hubspot] token refresh failed: %s", e)
        return None
    access = str(tokens.get("access_token") or "").strip() or None
    if not access:
        return None
    database.upsert_saas_connection(
        int(secrets["account_id"]),
        PROVIDER,
        provider_account_id=secrets.get("provider_account_id"),
        access_token=access,
        refresh_token=str(tokens.get("refresh_token") or refresh),
        token_type=tokens.get("token_type") or secrets.get("token_type"),
        scope=tokens.get("scope") or secrets.get("scope"),
        status="connected",
    )
    secrets["access_token"] = access
    if tokens.get("refresh_token"):
        secrets["refresh_token"] = tokens["refresh_token"]
    return access


def _object_properties(
    secrets: dict[str, Any], object_type: str, object_id: str,
) -> dict[str, Any]:
    token = (secrets.get("access_token") or "").strip()
    if not token:
        logger.info("[saas.hubspot] no access token for account=%s — no properties",
                    secrets.get("account_id"))
        return {}
    try:
        return _fetch_crm_properties(token, object_type, object_id)
    except HubSpotSaaSUnauthorized:
        # HubSpot access tokens last ~30 minutes. One refresh + retry.
        refreshed = _refresh_connection_token(secrets)
        if not refreshed:
            return {}
        try:
            return _fetch_crm_properties(refreshed, object_type, object_id)
        except HubSpotSaaSUnauthorized:
            return {}


def _stage_label(
    secrets: dict[str, Any], object_type: str, stage_id: str,
) -> str | None:
    token = (secrets.get("access_token") or "").strip()
    if not token:
        return None
    try:
        return _fetch_stage_label(token, object_type, stage_id)
    except HubSpotSaaSUnauthorized:
        refreshed = _refresh_connection_token(secrets)
        if not refreshed:
            return None
        try:
            return _fetch_stage_label(refreshed, object_type, stage_id)
        except HubSpotSaaSUnauthorized:
            return None


def map_event(
    event: dict[str, Any],
    *,
    properties: dict[str, Any] | None = None,
    stage_label: str | None = None,
) -> dict[str, Any] | None:
    """Map a verified HubSpot event to a spine effect, or None to ignore."""
    object_type = _subscription_object(event)
    if object_type is None:
        return None
    if not _is_watched_property(event, object_type):
        return None

    props = properties if isinstance(properties, dict) else {}
    stage_prop = _stage_property_for(object_type)
    stage_id = event.get("propertyValue")
    if stage_id in (None, ""):
        stage_id = props.get(stage_prop)
    if stage_id in (None, ""):
        return None

    effect = map_stage(object_type, stage_id, stage_label)
    if effect is None:
        return None

    waiting_on, reason = _effect_copy(object_type, effect, str(stage_id), stage_label)

    occurred = event.get("occurredAt")
    event_time_unix = None
    if occurred is not None:
        try:
            n = int(occurred)
            # HubSpot sends ms; tolerate seconds.
            event_time_unix = n * 1_000_000 if n > 10_000_000_000 else n * 1_000_000_000
        except (TypeError, ValueError):
            event_time_unix = None

    object_id = event.get("objectId")
    if object_id is None:
        object_id = event.get("objectid")
    eid = event.get("eventId")
    portal = event.get("portalId")
    event_id = None
    if eid is not None:
        event_id = f"{portal}:{eid}" if portal is not None else str(eid)

    stype = str(event.get("subscriptionType") or f"{object_type}.propertyChange")
    return {
        "effect": effect,
        "event_type": stype,
        "event_id": event_id,
        "object_id": str(object_id) if object_id is not None else None,
        "object_type": object_type,
        "metadata": props,
        "waiting_on": waiting_on,
        "reason": reason,
        "event_time_unix": event_time_unix,
        "portal_id": str(portal) if portal is not None else None,
        "stage_id": str(stage_id),
        "stage_label": stage_label,
    }


def apply_verified_event(
    event: dict[str, Any],
    *,
    account_id: int,
    secrets: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Fetch deal/ticket properties (link key lives there), map, apply."""
    object_type = _subscription_object(event)
    if object_type is None or not _is_watched_property(event, object_type):
        logger.info(
            "[saas.hubspot] ignoring unmapped type=%s property=%s event=%s",
            event.get("subscriptionType"), event.get("propertyName"),
            event.get("eventId"),
        )
        return {
            "status": "ignored_unmapped",
            "event_type": event.get("subscriptionType"),
        }

    object_id = event.get("objectId")
    props: dict[str, Any] = {}
    label = None
    if secrets and object_id is not None:
        props = _object_properties(secrets, object_type, str(object_id))
        stage_id = event.get("propertyValue") or props.get(_stage_property_for(object_type))
        if stage_id not in (None, ""):
            label = _stage_label(secrets, object_type, str(stage_id))

    mapped = map_event(event, properties=props, stage_label=label)
    if mapped is None:
        logger.info(
            "[saas.hubspot] ignoring unmapped stage type=%s value=%s event=%s",
            event.get("subscriptionType"), event.get("propertyValue"),
            event.get("eventId"),
        )
        return {
            "status": "ignored_unmapped",
            "event_type": event.get("subscriptionType"),
        }
    return saas.apply_work_effect(
        account_id,
        provider=PROVIDER,
        effect=mapped["effect"],
        object_id=mapped.get("object_id"),
        waiting_on=mapped.get("waiting_on"),
        reason=mapped.get("reason"),
        event_id=mapped.get("event_id"),
        event_type=mapped.get("event_type"),
        event_time_unix=mapped.get("event_time_unix"),
        metadata=mapped.get("metadata"),
    )


def handle_webhook(
    payload: bytes,
    *,
    signature_v3: str | None = None,
    signature_v1: str | None = None,
    timestamp: str | None = None,
    method: str = "POST",
    uri: str = "",
) -> dict[str, Any]:
    """Verify, resolve the Trovis account via HubSpot portalId, apply.

    Account resolution: ``event.portalId`` → saas_connections.
    No matching **connected** row → ignore (log). Never guess a tenant.
    Missing loop metadata on the deal/ticket → no-op (log). Never invent.
    """
    events = parse_webhook_events(
        payload,
        signature_v3=signature_v3,
        signature_v1=signature_v1,
        timestamp=timestamp,
        method=method,
        uri=uri,
    )
    if not events:
        return {"status": "ignored_empty", "results": []}

    results: list[dict[str, Any]] = []
    for event in events:
        portal = event.get("portalId")
        if portal in (None, ""):
            logger.info(
                "[saas.hubspot] event %s type=%s has no portalId — ignore",
                event.get("eventId"), event.get("subscriptionType"),
            )
            results.append({
                "status": "ignored_no_account",
                "event_id": event.get("eventId"),
            })
            continue

        secrets = database.get_saas_connection_secrets_by_provider_account(
            PROVIDER, str(portal),
        )
        if secrets is None or secrets.get("status") != "connected":
            logger.info(
                "[saas.hubspot] no connected Trovis account for portal=%s event=%s — ignore",
                portal, event.get("eventId"),
            )
            results.append({
                "status": "ignored_no_connection",
                "event_id": event.get("eventId"),
                "portal_id": str(portal),
            })
            continue

        results.append(apply_verified_event(
            event,
            account_id=int(secrets["account_id"]),
            secrets=secrets,
        ))

    # Single-event batches expose that event's status at the top level so
    # the HTTP contract matches the Stripe adapter.
    primary = results[-1] if results else {"status": "ignored_empty"}
    return {
        "status": primary.get("status"),
        "results": results,
        **{k: v for k, v in primary.items() if k != "status"},
    }
