# pyright: reportPrivateUsage=false
"""Syscall-budget enforcement for the receive path.

DESIGN §26.1 calls out a specific contract for
:meth:`SerialPort.receive_available`:

    Syscall rate for ``receive(1)`` bursts: 1 per ``receive_available()`` call

The test counts ``read_nonblocking`` invocations via a class-level
monkeypatch while draining a burst through a real Linux pty. Regressions
(a stray extra ``os.read`` on every call, for example) surface as a
failed assertion rather than a subtle performance bump that would only
show up as a 2x slowdown months later in a bench chart.

Sibling sanity check: draining the same burst via ``receive(1)`` costs
at least N syscalls — the whole reason ``receive_available`` exists.
"""

from __future__ import annotations

import os
import sys

import pytest

if not sys.platform.startswith("linux"):
    pytest.skip("pty-backed syscall counting is Linux-only", allow_module_level=True)

from anyserial import SerialConfig, open_serial_port
from anyserial._backend import SyncSerialBackend

pytestmark = pytest.mark.anyio

_CFG = SerialConfig(baudrate=115_200)


class TestReceiveAvailableSyscallBudget:
    async def test_single_read_nonblocking_per_call(
        self,
        pty_port: tuple[int, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        controller, path = pty_port
        # 256 B fits inside one pty kernel buffer — the controller write
        # lands atomically, so TIOCINQ reports exactly 256 and the whole
        # payload drains in one os.read.
        payload = b"\xcc" * 256
        async with await open_serial_port(path, _CFG) as port:
            # Linux always selects the SyncSerialBackend path; narrow so the
            # ``read_nonblocking`` attribute access type-checks.
            backend = port._backend
            assert isinstance(backend, SyncSerialBackend)
            backend_cls = type(backend)
            original = backend_cls.read_nonblocking
            counter = [0]

            def counted_read(self: object, buf: bytearray | memoryview) -> int:
                counter[0] += 1
                return original(self, buf)  # type: ignore[arg-type]

            monkeypatch.setattr(backend_cls, "read_nonblocking", counted_read)

            os.write(controller, payload)
            got = bytearray()
            while len(got) < len(payload):
                chunk = await port.receive_available()
                got.extend(chunk)

            assert bytes(got[: len(payload)]) == payload
            # One receive_available call → one read_nonblocking syscall.
            assert counter[0] == 1, (
                f"expected 1 read_nonblocking call, got {counter[0]} — "
                "DESIGN §26.1 syscall budget regression"
            )

    async def test_receive_one_costs_n_syscalls_for_comparison(
        self,
        pty_port: tuple[int, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Sanity: ``receive(1)`` N times costs at least N ``os.read`` calls.

        Not a regression guard on ``receive(1)`` — its behaviour is "one
        syscall per call by design." This test anchors the *other*
        direction: if it ever costs fewer than N syscalls, something is
        wrong (possibly a hidden buffering layer breaking cancellation).
        """
        controller, path = pty_port
        payload = b"\xcc" * 64
        async with await open_serial_port(path, _CFG) as port:
            # Linux always selects the SyncSerialBackend path; narrow so the
            # ``read_nonblocking`` attribute access type-checks.
            backend = port._backend
            assert isinstance(backend, SyncSerialBackend)
            backend_cls = type(backend)
            original = backend_cls.read_nonblocking
            counter = [0]

            def counted_read(self: object, buf: bytearray | memoryview) -> int:
                counter[0] += 1
                return original(self, buf)  # type: ignore[arg-type]

            monkeypatch.setattr(backend_cls, "read_nonblocking", counted_read)

            os.write(controller, payload)
            got = bytearray()
            while len(got) < len(payload):
                chunk = await port.receive(1)
                got.extend(chunk)

            assert bytes(got[: len(payload)]) == payload
            assert counter[0] >= len(payload), (
                f"receive(1) x {len(payload)} should trigger >={len(payload)} "
                f"read_nonblocking calls, got {counter[0]}"
            )
