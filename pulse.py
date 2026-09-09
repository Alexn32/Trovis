"""Home's fleet-pulse insight: one generated sentence, truth-locked.

The pulse itself is computed from the record and never waits on a model. This
module adds at most ONE short sentence on top of it, and its whole job is to
make that sentence safe to show.

The rule is entailment, not plausibility. The model sees a DATA packet of
proven fields and may only say things that follow from it; anything else is
dropped before it reaches a screen. `validate_insight` is the gate, and it
fails CLOSED — a sentence we cannot verify is worth less than no sentence,
because a dashboard that invents one number stops being evidence for any of
them.

Latency. First paint must never wait on this, and it does not: the browser
draws the pulse from the record and fills the sentence in whenever the answer
lands. Because of that, the request's wait budget
(TROVIS_PULSE_INSIGHT_WAIT_S) is NOT a paint deadline — it only decides
whether the sentence arrives on this load or the next. A very short budget
therefore costs the feature its first impression while buying nothing, so it
sits just under the model call's own timeout. Anything still running when the
budget expires keeps going and fills the cache; abandoning the call outright,
as a strict 1s cap would, means the cache never fills and the feature never
appears at all.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

logger = logging.getLogger("trovis")

# A one-sentence structured extraction — the fast model, not the smart one.
FAST_MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS = 300
# Thinking is explicit here for the same reason it is in describer.py: omitting
# it means the model thinks, and thinking shares the token budget with the
# reply. 300 tokens of budget would vanish into it.
THINKING = {"type": "disabled"}


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except (TypeError, ValueError):
        return default


# How long a request will WAIT. Generation is not cancelled at this point.
#
# This does NOT gate the paint. Home renders its workforce, graphic and caption
# from the record and fills the sentence in whenever the response lands, so a
# short budget buys nothing — it only guarantees that the FIRST view of any new
# packet has no sentence, which for someone who looks once is every view. The
# budget therefore sits just under the call timeout: most generations land
# inside the same request, and the ones that do not still fill the cache.
def wait_budget_s() -> float:
    return _f("TROVIS_PULSE_INSIGHT_WAIT_S", 6.0)


# How long the model call itself may take before we give up on it entirely.
def call_timeout_s() -> float:
    return _f("TROVIS_PULSE_INSIGHT_TIMEOUT_S", 8.0)


CACHE_TTL_S = 900
_CACHE_MAX = 256

# The packet arrives from the browser and is spent on model tokens, so it is
# bounded here rather than trusted. Home's real packet is a few hundred bytes;
# anything past this is not a packet we built.
MAX_PACKET_BYTES = 4096
MAX_PACKET_KEYS = 24

# The graphics a caller can actually draw. A model naming anything else is
# overruled by the deterministic chooser.
#
# `week_stuck` is plumbed end to end and tested, but NOTHING FEEDS IT TODAY:
# stuck is derived live from loops.cached_state, which recompute_loop_state
# overwrites in place, and the `stall_detected` event type that would record
# the transition is declared in loops.py but never written by any production
# path. So a packet never carries stuck_this_week / stuck_last_week and the
# chooser always falls past this option. Making it real needs a history —
# most cheaply a daily counts snapshot written by the loop sweep, which would
# give every strip count a trend rather than only this one. Until then the
# guard in choose_graphic() is what keeps a model naming it from drawing an
# empty frame.
GRAPHICS = ("week_finished", "week_stuck", "need_a_look", "none")

SYSTEM_PROMPT = (
    "You write one insight for the person who owns this agent fleet.\n\n"
    "Pick the single most USEFUL true thing in DATA — a change, a "
    "concentration, a risk, or a win — and write it so they would want to "
    "click and ask more. Not a status report. Not cheerleading. Not a list "
    "of everything you were given.\n\n"
    "You may only use the facts in DATA. Every claim must be entailed by "
    "DATA. If DATA is too thin for a real insight, return "
    '{"insight":"","graphic":"none"}. An empty answer is correct and '
    "expected when there is nothing worth saying — do not manufacture one.\n\n"
    "Do not invent: percentages, dollars, agents, kinds of work, causes, "
    '"efficiency", health, uptime, or motives that are not in DATA.\n'
    "Do not say work is moving/healthy unless DATA supports it.\n"
    "Do not mention fields that are missing.\n"
    "Do not do arithmetic — no differences, totals, or rates. If you use a "
    "number, copy it exactly from DATA.\n\n"
    "Return JSON only:\n"
    "{\n"
    '  "insight": "one or two short sentences, human language, no jargon",\n'
    '  "graphic": "week_finished" | "week_stuck" | "need_a_look" | "none",\n'
    '  "used": ["list of DATA keys you relied on"]\n'
    "}"
)

# Never allowed in the output, whatever DATA says. The money and percent signs
# because the packet carries neither in a form worth quoting; the rest because
# they are claims about health and internals the record cannot support.
BANNED_RE = re.compile(
    r"[$%]|\b(efficienc\w*|uptime|healthy|health|sla|downtime|"
    r"loops?|workloops?|possession|segments?|stations?|handoffs?)\b",
    re.I,
)

# Words that may start with a capital in ordinary prose, so a capitalised token
# is only treated as a name when it is none of these. The list is short on
# purpose: the check fails closed, and a dropped good sentence costs nothing.
_ORDINARY = {
    "a", "agents", "all", "an", "and", "as", "at", "but", "finished", "for",
    "from", "in", "is", "it", "last", "more", "most", "no", "nothing", "of",
    "on", "one", "or", "same", "since", "so", "stuck", "than", "that", "the",
    "there", "they", "this", "to", "two", "up", "waiting", "was", "week",
    "were", "with", "work", "you", "your", "monday", "tuesday", "wednesday",
    "thursday", "friday", "saturday", "sunday", "today", "yesterday",
}

_NUM_RE = re.compile(r"\d+(?:\.\d+)?")
_CAP_RE = re.compile(r"(?<![.!?]\s)(?<!^)\b([A-Z][A-Za-z0-9_.-]*)\b")


def packet_key(packet: dict[str, Any]) -> str:
    """Stable hash of a packet — the cache key. Same facts, same sentence."""
    blob = json.dumps(packet, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def _numbers_in(packet: dict[str, Any]) -> set[str]:
    """Every number a sentence is allowed to print, as it would be written."""
    out: set[str] = set()

    def add(v: Any) -> None:
        if isinstance(v, bool) or v is None:
            return
        if isinstance(v, (int, float)):
            out.add(_NUM_RE.sub(lambda m: m.group(0), f"{v:g}"))
            out.add(str(int(v)) if float(v).is_integer() else f"{v}")

    for v in packet.values():
        if isinstance(v, list):
            # A list's own length is a fact about DATA, so "2 agents need a
            # look" is entailed even though no field literally holds 2.
            add(len(v))
            for entry in v:
                if isinstance(entry, dict):
                    for iv in entry.values():
                        add(iv)
                else:
                    add(entry)
        else:
            add(v)
    return {n for n in out if n}


def _names_in(packet: dict[str, Any]) -> set[str]:
    """Proper nouns the sentence may use: agent names, and nothing else."""
    out: set[str] = set()
    for v in packet.values():
        if isinstance(v, list):
            for entry in v:
                if isinstance(entry, dict):
                    for key in ("name", "agent", "title"):
                        if entry.get(key):
                            out.add(str(entry[key]))
                elif isinstance(entry, str):
                    out.add(entry)
        elif isinstance(v, str):
            out.add(v)
    # Individual words too, so "Support Bot is quiet" clears on a packet
    # holding "Support Bot".
    words = set()
    for n in out:
        words.update(re.findall(r"[A-Za-z0-9_.-]+", n))
    return {w for w in out | words if w}


def validate_insight(text: str, packet: dict[str, Any]) -> tuple[bool, str]:
    """Is this sentence entailed by the packet? Returns (ok, reason).

    Fails closed on anything it cannot prove. The reason is for the log, not
    for a user — a rejected sentence is never shown in any form.
    """
    s = (text or "").strip()
    if not s:
        return False, "empty"
    if len(s) > 320:
        return False, "too long"
    if BANNED_RE.search(s):
        return False, f"banned token: {BANNED_RE.search(s).group(0)!r}"

    allowed_nums = _numbers_in(packet)
    for n in _NUM_RE.findall(s):
        if n not in allowed_nums:
            return False, f"number {n!r} is not in DATA"

    allowed_names = {n.lower() for n in _names_in(packet)}
    for cap in _CAP_RE.findall(s):
        if cap.lower() in _ORDINARY or cap.lower() in allowed_names:
            continue
        return False, f"name {cap!r} is not in DATA"
    return True, ""


def choose_graphic(packet: dict[str, Any], model_choice: str | None = None) -> str:
    """The deterministic chooser. A valid model choice wins; anything else
    falls back to the order the brief locks."""
    have_finished = (
        "finished_this_week" in packet and "finished_last_week" in packet
    )
    have_stuck = "stuck_this_week" in packet and "stuck_last_week" in packet
    need_look = bool(packet.get("need_a_look"))

    def usable(g: str | None) -> bool:
        if g == "week_finished":
            return have_finished
        if g == "week_stuck":
            return have_stuck
        if g == "need_a_look":
            return need_look
        return g == "none"

    if model_choice in GRAPHICS and usable(model_choice):
        return model_choice
    if have_finished:
        return "week_finished"
    if have_stuck:
        return "week_stuck"
    if need_look:
        return "need_a_look"
    return "none"


# --- generation ------------------------------------------------------------

_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_inflight: dict[str, Future] = {}
_lock = threading.Lock()
_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="pulse")


def _cache_get(key: str) -> dict[str, Any] | None:
    with _lock:
        hit = _cache.get(key)
    if not hit:
        return None
    at, value = hit
    if time.time() - at > CACHE_TTL_S:
        with _lock:
            _cache.pop(key, None)
        return None
    return value


def _cache_put(key: str, value: dict[str, Any]) -> None:
    with _lock:
        if len(_cache) >= _CACHE_MAX:
            oldest = min(_cache, key=lambda k: _cache[k][0])
            _cache.pop(oldest, None)
        _cache[key] = (time.time(), value)


def _call_model(packet: dict[str, Any]) -> dict[str, Any]:
    """One Haiku call. Never raises — a failure here is simply no insight."""
    import anthropic  # noqa: PLC0415

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return {}
    try:
        client = anthropic.Anthropic(api_key=api_key, timeout=call_timeout_s())
        resp = client.messages.create(
            model=FAST_MODEL,
            thinking=THINKING,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[{
                "role": "user",
                "content": "DATA:\n" + json.dumps(packet, sort_keys=True, default=str),
            }],
        )
        raw = "".join(
            b.text for b in resp.content if getattr(b, "type", None) == "text"
        ).strip()
    except Exception as exc:  # noqa: BLE001 — no insight is an acceptable answer
        logger.info("[pulse] insight call failed: %s", exc)
        return {}
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    try:
        out = json.loads(raw)
    except (TypeError, ValueError):
        logger.info("[pulse] insight was not JSON")
        return {}
    return out if isinstance(out, dict) else {}


def _generate(packet: dict[str, Any], key: str) -> dict[str, Any]:
    """Call, validate, cache. The validated result is what anyone ever sees."""
    out = _call_model(packet)
    text = str(out.get("insight") or "").strip()
    graphic = choose_graphic(packet, out.get("graphic"))
    if text:
        ok, why = validate_insight(text, packet)
        if not ok:
            logger.info("[pulse] dropped insight (%s): %r", why, text[:160])
            text = ""
    result = {"insight": text, "graphic": graphic, "used": out.get("used") or []}
    _cache_put(key, result)
    with _lock:
        _inflight.pop(key, None)
    return result


def insight_for(packet: dict[str, Any]) -> dict[str, Any]:
    """The endpoint's whole body.

    Returns the cached result instantly when we have one. Otherwise starts (or
    joins) a generation and waits only the short budget — on timeout the
    caller gets the graphic and no sentence, while the worker finishes and
    fills the cache for the next load.
    """
    if len(packet) > MAX_PACKET_KEYS:
        return {"insight": "", "graphic": "none", "used": [], "cached": False}
    try:
        blob = json.dumps(packet, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return {"insight": "", "graphic": "none", "used": [], "cached": False}
    if len(blob) > MAX_PACKET_BYTES:
        return {"insight": "", "graphic": "none", "used": [], "cached": False}

    key = packet_key(packet)
    hit = _cache_get(key)
    if hit is not None:
        return {**hit, "cached": True}

    fallback = {"insight": "", "graphic": choose_graphic(packet), "used": [], "cached": False}
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return fallback

    with _lock:
        fut = _inflight.get(key)
        if fut is None:
            fut = _pool.submit(_generate, packet, key)
            _inflight[key] = fut
    try:
        return {**fut.result(timeout=wait_budget_s()), "cached": False}
    except Exception:  # noqa: BLE001 — timeout included; the worker carries on
        return fallback
