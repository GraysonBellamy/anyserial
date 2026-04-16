"""Smoke tests — verify the package installs and imports cleanly."""

from __future__ import annotations

import anyio
import anyio.lowlevel
import pytest

import anyserial


def test_version_is_string() -> None:
    assert isinstance(anyserial.__version__, str)
    assert anyserial.__version__


def test_version_is_exported() -> None:
    assert "__version__" in anyserial.__all__


@pytest.mark.anyio
async def test_anyio_plugin_works() -> None:
    """Confirm AnyIO's built-in pytest plugin runs this coroutine."""
    await anyio.lowlevel.checkpoint()
