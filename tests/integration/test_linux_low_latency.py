"""Integration tests for ``low_latency=True`` against a real pty fd.

A Linux pty does not implement ``TIOCSSERIAL`` — the kernel returns
``ENOTTY``. That makes the pty an excellent stand-in for "driver does
not support low-latency": the same code path runs as on a real adapter
that lacks the ioctl, so we can verify each :class:`UnsupportedPolicy`
branch end-to-end without needing hardware.

The FTDI sysfs path is exercised separately with a tmp_path tree in
:mod:`tests.unit.test_linux_low_latency`, since a pty has no usb-serial
sysfs entry to point at.
"""

from __future__ import annotations

import contextlib
import os
import pty
import sys
import warnings
from contextlib import contextmanager
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

if not sys.platform.startswith("linux"):
    pytest.skip("Linux-only", allow_module_level=True)

from anyserial._linux.backend import LinuxBackend
from anyserial._types import UnsupportedPolicy
from anyserial.config import SerialConfig
from anyserial.exceptions import UnsupportedFeatureError


@contextmanager
def pty_path() -> Iterator[tuple[int, int, str]]:
    """Yield ``(controller, follower, path)`` — follower anchors the tty alive."""
    controller, follower = pty.openpty()
    path = os.ttyname(follower)
    try:
        yield controller, follower, path
    finally:
        for fd in (controller, follower):
            with contextlib.suppress(OSError):
                os.close(fd)


class TestRaisePolicy:
    def test_pty_open_with_low_latency_raises(self) -> None:
        with pty_path() as (_controller, _anchor, path):
            backend = LinuxBackend()
            config = SerialConfig(
                baudrate=115200,
                low_latency=True,
                unsupported_policy=UnsupportedPolicy.RAISE,
            )
            with pytest.raises(UnsupportedFeatureError, match="ASYNC_LOW_LATENCY"):
                backend.open(path, config)
            # Open path failed; backend must be back to a closed state so
            # a retry (or a fallback open with low_latency=False) starts
            # from clean ground.
            assert not backend.is_open


class TestWarnPolicy:
    def test_pty_open_with_low_latency_warns_and_succeeds(self) -> None:
        with pty_path() as (_controller, _anchor, path):
            backend = LinuxBackend()
            config = SerialConfig(
                baudrate=115200,
                low_latency=True,
                unsupported_policy=UnsupportedPolicy.WARN,
            )
            with pytest.warns(RuntimeWarning, match="ASYNC_LOW_LATENCY"):
                backend.open(path, config)
            try:
                assert backend.is_open
            finally:
                backend.close()


class TestIgnorePolicy:
    def test_pty_open_with_low_latency_silent(self) -> None:
        with pty_path() as (_controller, _anchor, path):
            backend = LinuxBackend()
            config = SerialConfig(
                baudrate=115200,
                low_latency=True,
                unsupported_policy=UnsupportedPolicy.IGNORE,
            )
            with warnings.catch_warnings():
                warnings.simplefilter("error")
                backend.open(path, config)
            try:
                assert backend.is_open
            finally:
                backend.close()


class TestStandardOpenPath:
    def test_low_latency_false_unchanged(self) -> None:
        # Sanity: explicit low_latency=False keeps the standard open path
        # exactly as-is; no TIOCSSERIAL is attempted.
        with pty_path() as (_controller, _anchor, path):
            backend = LinuxBackend()
            backend.open(path, SerialConfig(baudrate=115200, low_latency=False))
            try:
                assert backend.is_open
            finally:
                backend.close()
