"""Thin shim — all metadata lives in pyproject.toml. Kept so that
`pip install -e .` works on older toolchains that don't fully honor
PEP 660 editable installs from a pure pyproject."""

from setuptools import setup

setup()
