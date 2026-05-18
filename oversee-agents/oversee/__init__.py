"""Oversee SDK for the OpenAI Agents framework.

Two-line setup: import `init` from this package and call it once at
startup. Every Agent you create after that gets registered with
Oversee and every run shows up in your dashboard.
"""

from oversee.core import init
from oversee.version import __version__

__all__ = ["init", "__version__"]
