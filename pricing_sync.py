"""Daily model-pricing sync.

The major LLM providers don't publish a machine-readable price list —
their prices live on marketing HTML pages. The community-maintained
LiteLLM price file is the de-facto source of truth: one JSON covering
~2,700 models across OpenAI, Anthropic, Gemini, Bedrock, Azure, etc.,
updated continuously.

This module fetches that file, normalizes it to Oversee's per-1,000-token
schema, and UPSERTs it into the `model_pricing` table. A scheduler in
main.py calls refresh_pricing() on startup and once a day after that.

Cost is computed and frozen at span-ingest time (see database.insert_spans),
so a refresh only affects *future* spans — historical cost is never
rewritten. Keep that in mind: this keeps new estimates accurate, it does
not retroactively reprice past traces.

Stdlib only (urllib) — no new dependency added to the backend.
"""

from __future__ import annotations

import json
import logging
import ssl
import urllib.request

import database

logger = logging.getLogger("oversee.pricing")

# Raw GitHub URL for the LiteLLM price list (MIT-licensed). `main` always
# points at the latest published prices.
LITELLM_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/"
    "model_prices_and_context_window.json"
)

# LiteLLM tags every entry with a `mode`. We only want text-generation
# models — embeddings/audio/image/rerank price per-second or per-image and
# would pollute the table. Anything in this set is skipped; entries with no
# mode are kept (older entries omit it) as long as they carry token costs.
_EXCLUDED_MODES = {
    "embedding",
    "image_generation",
    "audio_transcription",
    "audio_speech",
    "moderation",
    "rerank",
    "completion_legacy",
}


def _ssl_context() -> ssl.SSLContext:
    """A verifying TLS context backed by certifi's CA bundle when present.

    The python.org macOS builds ship without a usable system CA store, so a
    plain urlopen() fails cert verification. certifi rides in via the
    backend's anthropic→httpx dependency, so we prefer it; if it's somehow
    absent we fall back to the stdlib default (correct on Linux/Railway,
    where the OS cert store is wired up)."""
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa: BLE001
        return ssl.create_default_context()


def fetch_litellm_pricing(url: str = LITELLM_URL, timeout: int = 30) -> dict:
    """Download and parse the LiteLLM price JSON. Raises on network/parse
    error — callers decide whether to swallow it (the scheduler does)."""
    req = urllib.request.Request(
        url, headers={"User-Agent": "oversee-pricing-sync"}
    )
    with urllib.request.urlopen(  # noqa: S310
        req, timeout=timeout, context=_ssl_context()
    ) as resp:
        return json.loads(resp.read().decode("utf-8"))


def normalize_pricing(raw: dict) -> list[tuple[str, float, float]]:
    """Flatten the LiteLLM map into (model_name, input/1k, output/1k) rows.

    LiteLLM quotes cost *per token*; Oversee stores cost *per 1,000 tokens*,
    so we multiply by 1,000. Only entries with both an input and output
    token cost are kept (this naturally drops embeddings, which have input
    cost only).

    Provider-prefixed ids ('gemini/gemini-1.5-pro') also get a bare alias
    ('gemini-1.5-pro') so they match the unprefixed model strings our agents
    actually emit on `gen_ai.request.model`. Explicit bare keys always win
    over derived aliases.
    """
    priced: dict[str, tuple[float, float]] = {}
    aliases: dict[str, tuple[float, float]] = {}

    for name, info in raw.items():
        if not isinstance(info, dict):
            continue  # the file has a non-model "sample_spec" meta key
        in_tok = info.get("input_cost_per_token")
        out_tok = info.get("output_cost_per_token")
        if in_tok is None or out_tok is None:
            continue
        mode = info.get("mode")
        if mode in _EXCLUDED_MODES:
            continue

        key = str(name).strip().lower()
        in_1k = round(float(in_tok) * 1000.0, 9)
        out_1k = round(float(out_tok) * 1000.0, 9)
        priced[key] = (in_1k, out_1k)

        if "/" in key:
            alias = key.split("/")[-1]
            aliases.setdefault(alias, (in_1k, out_1k))

    # Merge aliases under explicit keys (explicit wins on collision).
    for alias, rate in aliases.items():
        priced.setdefault(alias, rate)

    return [(name, rate[0], rate[1]) for name, rate in priced.items()]


def refresh_pricing(url: str = LITELLM_URL) -> dict:
    """Fetch → normalize → UPSERT. Returns a small summary dict.

    Raises if the fetch or DB write fails; the scheduler logs-and-continues
    so a transient outage never takes the app down (and the last good prices
    stay in the table)."""
    raw = fetch_litellm_pricing(url)
    rows = normalize_pricing(raw)
    upserted = database.upsert_pricing(rows, source="litellm")
    summary = {
        "source": "litellm",
        "entries_fetched": len(raw),
        "models_upserted": upserted,
    }
    logger.info("[Oversee] pricing refresh ok: %s", summary)
    return summary
