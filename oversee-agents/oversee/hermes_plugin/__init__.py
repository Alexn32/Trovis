"""Hermes Agent plugin for Oversee.

This package is the entry-point target referenced from
pyproject.toml under `[project.entry-points."hermes_agent.plugins"]`.
Hermes' plugin loader imports `register` from this module and calls
it once at gateway start. The real logic lives in
`oversee.hermes` — this file just re-exports it so the plugin can
be discovered by Hermes' importlib-based loader.
"""

from oversee.hermes import register

__all__ = ["register"]
