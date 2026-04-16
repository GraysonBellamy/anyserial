"""Shared pytest fixtures.

The ``anyio_backend`` fixture is parametrized across the full backend matrix —
every async test runs against asyncio (default), asyncio+uvloop (POSIX), and
trio. This uses AnyIO's built-in pytest plugin; do NOT add `pytest-anyio` as a
separate dependency.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from _pytest.mark.structures import ParameterSet

_PARAMS: list[ParameterSet] = [
    pytest.param(("asyncio", {"use_uvloop": False}), id="asyncio"),
    pytest.param("trio", id="trio"),
]

# uvloop is POSIX-only
if sys.platform != "win32":
    _PARAMS.insert(
        1,
        pytest.param(("asyncio", {"use_uvloop": True}), id="asyncio+uvloop"),
    )


@pytest.fixture(params=_PARAMS)
def anyio_backend(request: pytest.FixtureRequest) -> object:
    """Run async tests against asyncio, asyncio+uvloop (POSIX only), and trio."""
    return request.param
