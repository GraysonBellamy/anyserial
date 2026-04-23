"""Integration tests for :class:`LinuxBackend` against real pty fds.

Reuses the pty helpers from :mod:`test_posix_backend` semantics but narrows
the scope to Linux-specific behaviour: the custom-baud path (``TCSETS2`` +
``BOTHER``) and the Linux capability snapshot.
"""

from __future__ import annotations

import contextlib
import os
import pty
import sys
import termios
from contextlib import contextmanager
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Generator

if not sys.platform.startswith("linux"):
    pytest.skip("Linux-only", allow_module_level=True)

from anyserial._linux.backend import LinuxBackend
from anyserial._linux.baudrate import BOTHER, CBAUD, read_termios2
from anyserial.config import SerialConfig


@contextmanager
def pty_path() -> Generator[tuple[int, int, str]]:
    """Yield ``(controller, follower, path)`` — follower anchors the tty alive."""
    controller, follower = pty.openpty()
    path = os.ttyname(follower)
    try:
        yield controller, follower, path
    finally:
        for fd in (controller, follower):
            with contextlib.suppress(OSError):
                os.close(fd)


class TestStandardBaudPath:
    """Standard rates must still flow through the inherited tcsetattr path."""

    def test_inherits_posix_open(self) -> None:
        with pty_path() as (_controller, _anchor, path):
            backend = LinuxBackend()
            backend.open(path, SerialConfig(baudrate=115200))
            try:
                attrs = termios.tcgetattr(backend.fileno())
                assert attrs[4] == termios.B115200
                assert attrs[5] == termios.B115200
            finally:
                backend.close()


class TestCustomBaudPath:
    def test_open_applies_custom_rate_via_tcsets2(self) -> None:
        # 250000 has no termios.Bxxxx constant on any current kernel. The
        # LinuxBackend must detect that, route through TCSETS2 + BOTHER,
        # and leave c_ispeed / c_ospeed at exactly 250000.
        with pty_path() as (_controller, _anchor, path):
            backend = LinuxBackend()
            backend.open(path, SerialConfig(baudrate=250000))
            try:
                attrs2 = read_termios2(backend.fileno())
                assert attrs2.ispeed == 250000
                assert attrs2.ospeed == 250000
                assert attrs2.cflag & CBAUD == BOTHER
            finally:
                backend.close()

    def test_reconfigure_switches_between_standard_and_custom(self) -> None:
        with pty_path() as (_controller, _anchor, path):
            backend = LinuxBackend()
            backend.open(path, SerialConfig(baudrate=9600))
            try:
                # Standard → custom.
                backend.configure(SerialConfig(baudrate=250000))
                attrs2 = read_termios2(backend.fileno())
                assert attrs2.ispeed == 250000
                assert attrs2.cflag & CBAUD == BOTHER

                # Custom → standard. The inherited tcsetattr path should
                # rewrite the CBAUD slot back to B115200.
                backend.configure(SerialConfig(baudrate=115200))
                attrs = termios.tcgetattr(backend.fileno())
                assert attrs[4] == termios.B115200
            finally:
                backend.close()

    def test_raw_mode_bits_still_applied_under_custom_baud(self) -> None:
        # Proves the custom-baud path does not skip the shared builder
        # pipeline — the pty must end up in raw mode with CREAD + CLOCAL
        # just like the standard-baud path.
        with pty_path() as (_controller, _anchor, path):
            backend = LinuxBackend()
            backend.open(path, SerialConfig(baudrate=250000))
            try:
                attrs2 = read_termios2(backend.fileno())
                assert attrs2.cflag & termios.CREAD
                assert attrs2.cflag & termios.CLOCAL
                assert attrs2.lflag & termios.ICANON == 0
                assert attrs2.oflag & termios.OPOST == 0
            finally:
                backend.close()


class TestCapabilities:
    def test_reports_linux_backend(self) -> None:
        caps = LinuxBackend().capabilities
        assert caps.backend == "linux"
        assert caps.custom_baudrate.value == "supported"
