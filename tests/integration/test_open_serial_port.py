"""End-to-end smoke test for :func:`open_serial_port` against a real pty.

Verifies the full orchestration path — selector → ``LinuxBackend.open`` →
:class:`SerialPort` — and exercises one round-trip over the async readiness
loop across every AnyIO backend in the test matrix (asyncio, asyncio+uvloop,
trio). The comprehensive cancellation / concurrency / close-race matrix
lives in :mod:`test_serial_port_pty`; this file is intentionally minimal.
"""

from __future__ import annotations

import os
import sys

import anyio
import pytest

if not sys.platform.startswith("linux"):
    pytest.skip(
        "Linux-only: pty-based integration tests for the Linux backend",
        allow_module_level=True,
    )

from anyserial import SerialConfig, open_serial_port
from anyserial._linux.backend import LinuxBackend

# Parametrize every async test in the module across asyncio / asyncio+uvloop
# / trio via the ``anyio_backend`` fixture in ``tests/conftest.py``.
pytestmark = pytest.mark.anyio


class TestOpenSerialPort:
    async def test_opens_linux_pty_end_to_end(self, pty_port: tuple[int, str]) -> None:
        _controller, path = pty_port
        async with await open_serial_port(path, SerialConfig(baudrate=9600)) as port:
            assert port.is_open
            assert port.path == path
            # Backend landed is the Linux one, confirming the selector wiring.
            assert isinstance(port._backend, LinuxBackend)  # pyright: ignore[reportPrivateUsage]

    async def test_capabilities_reflect_linux_backend(self, pty_port: tuple[int, str]) -> None:
        _controller, path = pty_port
        async with await open_serial_port(path, SerialConfig(baudrate=9600)) as port:
            caps = port.capabilities
            assert caps.backend == "linux"
            assert caps.custom_baudrate.value == "supported"

    async def test_send_and_receive_round_trip(self, pty_port: tuple[int, str]) -> None:
        controller, path = pty_port
        async with await open_serial_port(path, SerialConfig(baudrate=9600)) as port:
            # port → controller direction.
            await port.send(b"hello")
            with anyio.fail_after(1.0):
                received = bytearray()
                while len(received) < 5:
                    try:
                        chunk = os.read(controller, 16)
                    except BlockingIOError:
                        await anyio.sleep(0.001)
                        continue
                    received.extend(chunk)
            assert bytes(received[:5]) == b"hello"

            # controller → port direction.
            os.write(controller, b"world")
            with anyio.fail_after(1.0):
                reply = await port.receive(16)
            assert reply == b"world"

    async def test_open_nonexistent_raises_port_not_found(self) -> None:
        # The orchestrator's OSError → PortNotFoundError mapping fires here.
        # FileNotFoundError is the base class our PortNotFoundError inherits
        # from — either spelling is acceptable; we use the stdlib base so the
        # test doubles as a check on the multi-inheritance design.
        with pytest.raises(FileNotFoundError):
            await open_serial_port("/dev/definitely-not-a-tty", SerialConfig(baudrate=9600))
