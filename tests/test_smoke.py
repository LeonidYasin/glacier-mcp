"""Smoke test: package imports and version are readable."""

from __future__ import annotations

import glacier_mcp


def test_version_is_string() -> None:
    assert isinstance(glacier_mcp.__version__, str)
    assert glacier_mcp.__version__


def test_main_is_importable() -> None:
    from glacier_mcp.__main__ import main

    assert callable(main)
