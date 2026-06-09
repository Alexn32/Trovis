"""Trovis SDK for AI agents.

Two-line setup: import `init` from this package and call it once at
startup. Every Agent you create after that gets registered with
Trovis and every run shows up in your dashboard.

Supports two platforms today:
  - OpenAI Agents SDK    — `init(platform="openai")` or "auto"
  - Anthropic Claude     — `init(platform="anthropic")` or "auto"
    Managed Agents

`init(platform="auto")` (the default) detects which SDK(s) are
installed and hooks into both when present.

Advanced users can import the Anthropic helpers directly:
    from trovis import monitor, track_session
These are only available when the `anthropic` extra is installed.
"""

from trovis.core import init
from trovis.propagation import continue_trace, extract, inject
from trovis.version import __version__

__all__ = ["init", "inject", "extract", "continue_trace", "__version__"]

# Optional Anthropic helpers — re-exported when the anthropic SDK is
# available so users can do `from trovis import monitor`. We swallow
# the ImportError silently; the core init() still works without them.
try:
    from trovis.anthropic import monitor, track_session  # noqa: F401

    __all__ += ["monitor", "track_session"]
except ImportError:  # pragma: no cover — only fires on broken installs
    pass
