"""Transactional email via Resend — fail-soft.

Mirrors billing.py's "configured? else no-op" shape. Two things are OPTIONAL at
import time: the ``resend`` package and ``RESEND_API_KEY``. If either is missing
the module imports cleanly and ``is_configured()`` is False; ``send_email`` then
logs and returns False instead of raising. That keeps password-reset / invite
flows from 500-ing on an unconfigured deploy — the feature simply "turns on" the
moment the key + a verified sending domain are set.

Config (read live, never cached):
  RESEND_API_KEY   re_...                         — Resend API key
  EMAIL_FROM       "Trovis <noreply@trovisai.com>"  — verified sender; defaults
                   to Resend's shared sandbox sender for dev.
"""
from __future__ import annotations

import logging
import os

import database

logger = logging.getLogger("trovis")

# Resend's shared sandbox sender works without domain verification but only
# delivers to the account owner's address — fine for local/dev, replace in prod
# by setting EMAIL_FROM to a verified address on your domain.
_DEFAULT_FROM = "Trovis <onboarding@resend.dev>"


def _resend():
    """Import the Resend SDK lazily; return None if it isn't installed."""
    try:
        import resend  # noqa: PLC0415
    except ImportError:
        return None
    return resend


def api_key() -> str | None:
    # Prefer the plain, conventional name (RESEND_API_KEY — matches Resend's
    # docs and how Stripe keys are read); also accept the TROVIS_/OVERSEE_-
    # prefixed variants so either works.
    return os.getenv("RESEND_API_KEY") or database.env("RESEND_API_KEY") or None


def from_address() -> str:
    return os.getenv("EMAIL_FROM") or database.env("EMAIL_FROM") or _DEFAULT_FROM


def is_configured() -> bool:
    """True if email can actually send: SDK installed and an API key set."""
    return _resend() is not None and bool(api_key())


def send_email(to: str, subject: str, html: str) -> bool:
    """Send one transactional email. Returns True on send, False when email
    isn't configured or the send fails — NEVER raises, so callers stay
    fail-soft. (A failed reset email must not break the request; the user can
    retry, and we don't leak delivery state to the caller anyway.)"""
    if not to:
        return False
    resend = _resend()
    key = api_key()
    if resend is None or not key:
        logger.info("[email] not configured — skipping send to %s (%r)", to, subject)
        return False
    try:
        resend.api_key = key
        resend.Emails.send(
            {"from": from_address(), "to": [to], "subject": subject, "html": html}
        )
        return True
    except Exception as exc:  # noqa: BLE001 — fail-soft by design
        logger.warning("[email] send to %s failed: %s", to, exc)
        return False
