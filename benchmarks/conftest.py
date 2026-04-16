"""Shared fixtures for the benchmark suite.

The async tests under ``tests/`` use AnyIO's pytest plugin, which makes
each test coroutine the unit of work. ``pytest-benchmark``'s ``benchmark``
fixture only times *sync* callables, so the two integrate badly when used
naively (every iteration would pay an ``anyio.run`` startup cost ≈50 ms,
swamping the actual workload).

Instead we hold one persistent event loop per benchmark via
:func:`anyio.from_thread.start_blocking_portal`. Each iteration's payload
is a single ``portal.call(...)`` — round-trip overhead in the tens of µs,
small enough that the timed body still reflects the workload we care
about. The portal is parametrized across the same AnyIO backend matrix
the rest of the test suite uses.

Linux-only by construction: the pty fixture relies on ``pty.openpty()``
and the integration test's ``raw_pty_pair`` helper. The whole module
short-circuits via ``pytest.skip`` on non-Linux hosts.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

import pytest

if not sys.platform.startswith("linux"):
    pytest.skip(
        "pty-backed benchmarks are Linux-only",
        allow_module_level=True,
    )

# Past the platform gate.
import contextlib
import fcntl
import os
import pty
import termios
from contextlib import contextmanager

import anyio.from_thread

if TYPE_CHECKING:
    from collections.abc import Iterator


# Pty helper duplicated from tests/integration/conftest.py — pytest's
# benchmark testpath doesn't share sys.path with tests/, and a copy is
# cheaper than wiring up a shared package. See DESIGN §27.2.1 for the
# four pty gotchas these eight lines guard against.
def _apply_raw_mode(fd: int) -> None:
    attrs = termios.tcgetattr(fd)
    attrs[0] &= ~(
        termios.IGNBRK
        | termios.BRKINT
        | termios.PARMRK
        | termios.ISTRIP
        | termios.INLCR
        | termios.IGNCR
        | termios.ICRNL
        | termios.IXON
    )
    attrs[1] &= ~termios.OPOST
    attrs[3] &= ~(termios.ECHO | termios.ECHONL | termios.ICANON | termios.ISIG | termios.IEXTEN)
    attrs[2] = (attrs[2] & ~(termios.CSIZE | termios.PARENB)) | termios.CS8
    termios.tcsetattr(fd, termios.TCSANOW, attrs)


@contextmanager
def raw_pty_pair() -> Iterator[tuple[int, str]]:
    """Yield ``(controller_fd, follower_path)`` in raw mode."""
    controller, follower = pty.openpty()
    path = os.ttyname(follower)
    _apply_raw_mode(follower)
    os.close(follower)
    flags = fcntl.fcntl(controller, fcntl.F_GETFL)
    fcntl.fcntl(controller, fcntl.F_SETFL, flags | os.O_NONBLOCK)
    try:
        yield controller, path
    finally:
        with contextlib.suppress(OSError):
            os.close(controller)


_BACKEND_PARAMS = [
    pytest.param(("asyncio", {"use_uvloop": False}), id="asyncio"),
    pytest.param(("asyncio", {"use_uvloop": True}), id="asyncio+uvloop"),
    pytest.param(("trio", {}), id="trio"),
]


@pytest.fixture(params=_BACKEND_PARAMS)
def bench_portal(
    request: pytest.FixtureRequest,
) -> Iterator[anyio.from_thread.BlockingPortal]:
    """Yield a :class:`BlockingPortal` for the parametrized AnyIO backend.

    The portal owns a background thread running an event loop on the
    requested backend; tests submit coroutines via ``portal.call(...)``.
    Setup / teardown happens once per benchmark — well outside the timed
    iterations.
    """
    backend, options = request.param
    with anyio.from_thread.start_blocking_portal(backend, backend_options=options) as portal:
        yield portal


@pytest.fixture
def pty_pair() -> Iterator[tuple[int, str]]:
    """Yield ``(controller_fd, follower_path)`` for a raw-mode pty pair."""
    with raw_pty_pair() as pair:
        yield pair
