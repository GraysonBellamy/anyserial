"""Shared fixtures for pty-backed integration tests.

Integration tests that open a real :class:`SerialPort` need a kernel pty
plus a peer fd they can drive directly to simulate the remote end. The
pty's line discipline defaults to cooked mode — the kernel buffers writes
until a newline arrives and translates ``\\n`` to ``\\r\\n`` on the output
path. Byte-oriented protocols don't want either, so the helper puts the
follower side into raw mode *before* closing its fd so the kernel state
persists on the pts path.

Module-level skip keeps Windows out of the ``pty`` / ``termios`` import
chain entirely — those stdlib modules don't exist there. Linux and
Darwin both ship :mod:`pty`; per-test-file skips in this directory
pin down the tests that remain Linux-specific (e.g., ``TCSETS2`` /
``TIOCSRS485`` / ``ASYNC_LOW_LATENCY`` coverage).
"""

from __future__ import annotations

import sys

import pytest

if sys.platform == "win32":
    pytest.skip(
        "pty-backed integration tests require POSIX ``pty`` / ``termios``",
        allow_module_level=True,
    )

# Past the skip gate: safe to import POSIX-only modules and expose helpers.
import contextlib
import fcntl
import os
import pty
import termios
from contextlib import contextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator


def _apply_raw_mode(fd: int) -> None:
    """In-place cfmakeraw for a pty follower fd."""
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
    """Yield ``(controller_fd, follower_path)`` in raw mode.

    The follower fd is closed before the yield so the caller's
    :func:`open_serial_port` can open its own fd on the same
    ``/dev/pts/N`` path. The controller fd stays open as the peer side
    of the pty so data flows in both directions.
    """
    controller, follower = pty.openpty()
    path = os.ttyname(follower)
    _apply_raw_mode(follower)
    os.close(follower)
    # Force the controller into non-blocking mode — otherwise an ``os.read``
    # from the test's event loop can park a kernel thread and deadlock the
    # concurrent writer on the port side (the writer is parked in
    # ``wait_writable`` waiting for the pty buffer to drain, which only
    # happens once the reader hands control back to the loop).
    flags = fcntl.fcntl(controller, fcntl.F_GETFL)
    fcntl.fcntl(controller, fcntl.F_SETFL, flags | os.O_NONBLOCK)
    try:
        yield controller, path
    finally:
        with contextlib.suppress(OSError):
            os.close(controller)


@pytest.fixture
def pty_port() -> Iterator[tuple[int, str]]:
    """Pytest-fixture wrapper around :func:`raw_pty_pair`."""
    with raw_pty_pair() as pair:
        yield pair
