"""Hardware test for kernel-level RS-485 on a real adapter.

Opt-in via the ``ANYSERIAL_RS485_PORT`` environment variable, gated by
the ``hardware`` marker. The variable must point at a device whose
kernel driver implements ``TIOCSRS485`` (genuine FTDI chips on recent
kernels, most industrial PCIe cards, some PL-series adapters). Most
consumer USB-serial dongles will fail the capability probe and the test
will skip — that is the intended behaviour, not a bug.

What gets verified:

1. ``TIOCGRS485`` round-trips — the kernel hands back the exact state
   we just wrote (flags + delays), modulo driver-reserved bits.
2. Enabling then disabling RS-485 via ``configure()`` restores the
   pre-touch state on the fd. ``close()`` is covered by the same
   codepath; the integration suite (``test_linux_rs485.py``) asserts
   the close-time restore against a stub driver.

Run via::

    ANYSERIAL_RS485_PORT=/dev/ttyUSB0 uv run pytest -m hardware \
        tests/hardware/test_rs485_adapter.py

A loopback cable is not required; the test only writes to the
``struct serial_rs485`` and never moves bytes over the wire.
"""

from __future__ import annotations

import contextlib
import errno
import os
import sys

import anyio
import pytest

if not sys.platform.startswith("linux"):
    pytest.skip("TIOCSRS485 is Linux-only", allow_module_level=True)

from anyserial._linux import rs485
from anyserial._linux.backend import LinuxBackend
from anyserial.config import RS485Config, SerialConfig
from anyserial.stream import open_serial_port

pytestmark = pytest.mark.hardware

_ENV_VAR = "ANYSERIAL_RS485_PORT"


def _port_from_env() -> str:
    """Return the env-supplied device path or skip the test."""
    path = os.environ.get(_ENV_VAR)
    if not path:
        pytest.skip(f"set {_ENV_VAR} to a device whose driver supports TIOCSRS485")
    return path


@pytest.fixture
def rs485_port() -> str:
    """Resolve the device path and skip unless the driver accepts TIOCSRS485."""
    path = _port_from_env()

    # Probe the ioctl directly — open the fd briefly just to ask. Most
    # adapters respond with ENOTTY; real RS-485 drivers answer cleanly.
    fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    try:
        try:
            rs485.read_rs485(fd)
        except OSError as exc:
            if exc.errno in {errno.ENOTTY, errno.EINVAL}:
                pytest.skip(
                    f"{path} driver does not implement TIOCSRS485 (errno={exc.errno})",
                )
            raise
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)
    return path


class TestTIOCRS485RoundTrip:
    def test_write_then_read_returns_same_flags(self, rs485_port: str) -> None:
        backend = LinuxBackend()
        backend.open(
            rs485_port,
            SerialConfig(
                rs485=RS485Config(
                    enabled=True,
                    rts_on_send=True,
                    rts_after_send=False,
                    delay_before_send=0.001,
                    delay_after_send=0.002,
                ),
            ),
        )
        try:
            current = rs485.read_rs485(backend.fileno())
            assert current.enabled
            assert current.flags & rs485.SER_RS485_RTS_ON_SEND
            assert not current.flags & rs485.SER_RS485_RTS_AFTER_SEND
            # Drivers may round delays to their hardware granularity; we
            # assert the ballpark rather than an exact match.
            assert current.delay_rts_before_send >= 1
            assert current.delay_rts_after_send >= 2
        finally:
            backend.close()


class TestRestoreOnConfigureNone:
    def test_disabling_rs485_restores_previous_state(self, rs485_port: str) -> None:
        # Snapshot the true pre-open state, then enable → disable via
        # configure() and verify the kernel returned to the snapshot.
        fd = os.open(rs485_port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            pristine = rs485.read_rs485(fd)
        finally:
            os.close(fd)

        async def _exercise() -> rs485.RS485State:
            async with await open_serial_port(
                rs485_port,
                SerialConfig(rs485=RS485Config()),
            ) as port:
                await port.configure(SerialConfig())  # rs485=None → restore
                # Peek the kernel state through the open fd. The backend
                # exposes its fd via port.extra(FileStreamAttribute.fileno).
                from anyio.streams.file import FileStreamAttribute  # noqa: PLC0415

                return rs485.read_rs485(port.extra(FileStreamAttribute.fileno))

        after = anyio.run(_exercise)
        assert after == pristine
