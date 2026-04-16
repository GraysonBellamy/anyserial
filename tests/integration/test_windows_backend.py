"""End-to-end tests for :class:`anyserial._windows.backend.WindowsBackend`.

design-windows-backend.md §10.2: integration via a com0com virtual COM-port
pair on Windows CI. The pair name is supplied via the
``ANYSERIAL_WINDOWS_PAIR`` environment variable as ``"COMA,COMB"`` —
hardcoding pair names is brittle (com0com auto-assigns names from the
first free pair; some installations get COM5/COM6, others COM50/COM51).

Tests are double-gated:

- Module-level skip when ``sys.platform != "win32"``.
- Per-test skip via the ``windows_virtual_serial`` marker when
  ``ANYSERIAL_WINDOWS_PAIR`` is unset.

Both gates are needed because the ``windll`` import in the data-path
modules will not succeed on POSIX; the module-level guard keeps the
collection from blowing up there.
"""

from __future__ import annotations

import os
import sys

import anyio
import pytest

from anyserial import SerialConfig, open_serial_port

# Read into a local so mypy doesn't narrow each branch to the type-
# checker's host platform and flag the rest of the file as unreachable.
_PLATFORM = sys.platform
if _PLATFORM != "win32":
    pytest.skip(
        "WindowsBackend integration tests require a Windows host with com0com",
        allow_module_level=True,
    )


pytestmark = [
    pytest.mark.anyio,
    pytest.mark.windows_virtual_serial,
]


def _pair_from_env() -> tuple[str, str] | None:
    raw = os.environ.get("ANYSERIAL_WINDOWS_PAIR")
    if not raw:
        return None
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if len(parts) != 2:
        msg = f"ANYSERIAL_WINDOWS_PAIR must be 'COMA,COMB'; got {raw!r}"
        raise RuntimeError(msg)
    return parts[0], parts[1]


@pytest.fixture
def com_pair() -> tuple[str, str]:
    pair = _pair_from_env()
    if pair is None:
        pytest.skip("ANYSERIAL_WINDOWS_PAIR not set; skipping com0com integration")
    return pair


class TestRoundTrip:
    async def test_send_then_receive(self, com_pair: tuple[str, str]) -> None:
        a_path, b_path = com_pair
        cfg = SerialConfig(baudrate=115_200)
        async with (
            await open_serial_port(a_path, cfg) as a,
            await open_serial_port(b_path, cfg) as b,
        ):
            await a.send(b"hello\n")
            data = await b.receive(64)
            assert data.startswith(b"hello")

    async def test_bidirectional_round_trip(self, com_pair: tuple[str, str]) -> None:
        a_path, b_path = com_pair
        cfg = SerialConfig(baudrate=115_200)
        async with (
            await open_serial_port(a_path, cfg) as a,
            await open_serial_port(b_path, cfg) as b,
            anyio.create_task_group() as tg,
        ):
            tg.start_soon(a.send, b"ping\n")
            received = await b.receive(64)
            assert received.startswith(b"ping")
            await b.send(b"pong\n")
            back = await a.receive(64)
            assert back.startswith(b"pong")

    async def test_receive_into_zero_copy(self, com_pair: tuple[str, str]) -> None:
        a_path, b_path = com_pair
        cfg = SerialConfig(baudrate=115_200)
        async with (
            await open_serial_port(a_path, cfg) as a,
            await open_serial_port(b_path, cfg) as b,
        ):
            await a.send(b"buffer-me")
            buf = bytearray(32)
            n = await b.receive_into(buf)
            assert bytes(buf[:n]).startswith(b"buffer-me"[:n])


class TestLifecycle:
    async def test_aclose_is_idempotent(self, com_pair: tuple[str, str]) -> None:
        a_path, _b_path = com_pair
        port = await open_serial_port(a_path, SerialConfig())
        await port.aclose()
        await port.aclose()  # second call must not raise.

    async def test_cancel_mid_receive_releases_resources(
        self,
        com_pair: tuple[str, str],
    ) -> None:
        # design-windows-backend.md §10.4 stress: parked receive, cancel,
        # verify clean teardown — neither ResourceWarning nor handle leak.
        a_path, _b_path = com_pair
        async with await open_serial_port(a_path, SerialConfig()) as port:
            with anyio.move_on_after(0.05):
                await port.receive(64)

    async def test_idle_receive_retries_empty_completions(
        self,
        com_pair: tuple[str, str],
    ) -> None:
        # design-windows-backend.md §6.3 regression guard: with the
        # MAXDWORD/MAXDWORD/1 "wait-for-any" COMMTIMEOUTS triple, the
        # kernel completes overlapped reads after ~1 ms with zero bytes
        # on an idle port. The backend must reissue the read internally
        # instead of treating zero bytes as EOF; otherwise receive() on
        # any idle port would raise SerialDisconnectedError after 1 ms.
        a_path, b_path = com_pair
        async with (
            await open_serial_port(a_path, SerialConfig()) as a,
            await open_serial_port(b_path, SerialConfig()) as b,
            anyio.create_task_group() as tg,
        ):

            async def delayed_send() -> None:
                # Sleep well past the 1 ms timeout floor so the receive
                # side absorbs multiple empty completions before data
                # arrives. If the retry loop is broken, the assert in
                # the main task never runs — SerialDisconnectedError
                # propagates out of the task group first.
                await anyio.sleep(0.05)
                await a.send(b"late")

            tg.start_soon(delayed_send)
            data = await b.receive(16)
            assert data.startswith(b"late")


class TestControlPlane:
    async def test_modem_lines_returns_snapshot(
        self,
        com_pair: tuple[str, str],
    ) -> None:
        a_path, _b_path = com_pair
        async with await open_serial_port(a_path, SerialConfig()) as port:
            lines = await port.modem_lines()
            # com0com's loopback wiring isn't standardised; we just verify
            # the call succeeds and returns a fully-populated dataclass.
            assert lines.cts is not None
            assert lines.dsr is not None
            assert lines.ri is not None
            assert lines.cd is not None

    async def test_set_control_lines_round_trip(
        self,
        com_pair: tuple[str, str],
    ) -> None:
        a_path, _b_path = com_pair
        async with await open_serial_port(a_path, SerialConfig()) as port:
            await port.set_control_lines(rts=True, dtr=True)
            await port.set_control_lines(rts=False, dtr=False)

    async def test_input_output_waiting_after_burst(
        self,
        com_pair: tuple[str, str],
    ) -> None:
        a_path, b_path = com_pair
        async with (
            await open_serial_port(a_path, SerialConfig()) as a,
            await open_serial_port(b_path, SerialConfig()) as b,
        ):
            await a.send(b"x" * 16)
            # Give the kernel a moment to surface the bytes on the peer.
            await anyio.sleep(0.05)
            assert b.input_waiting() > 0
            _ = await b.receive(64)


class TestReconfigure:
    async def test_configure_changes_baudrate(self, com_pair: tuple[str, str]) -> None:
        a_path, _b_path = com_pair
        async with await open_serial_port(a_path, SerialConfig()) as port:
            assert port.config.baudrate == 115_200
            await port.configure(SerialConfig(baudrate=9600))
            assert port.config.baudrate == 9600
