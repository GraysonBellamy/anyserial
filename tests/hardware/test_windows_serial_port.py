# pyright: reportPrivateUsage=false
"""Hardware tests for the Windows backend on a real COM port.

Opt-in via the ``ANYSERIAL_TEST_PORT`` environment variable (for example
``COM8``), matching the ``hardware`` marker registered in
:file:`pyproject.toml`. Nothing is written to the port and no loopback
wiring is needed; opening it does apply the default :class:`SerialConfig`
and assert DTR / RTS, so point it at an adapter whose peer tolerates that.

Every test runs under each AnyIO backend (asyncio and Trio). Real serial
drivers end an idle read under the "wait-for-any" ``COMMTIMEOUTS`` policy
with ``STATUS_TIMEOUT``, which virtual COM-port drivers may not reproduce,
so these paths need an actual adapter. The discovery check confirms that
``port.port_info`` is the entry :func:`find_serial_port` reports for the
same port.

Run via::

    $env:ANYSERIAL_TEST_PORT = "COM8"; uv run pytest -m hardware
"""

from __future__ import annotations

import os
import sys

import anyio
import pytest

from anyserial import (
    CommEvent,
    SerialConfig,
    SerialPort,
    canonical_port_name,
    find_serial_port,
    open_serial_port,
)
from anyserial._windows.backend import WindowsBackend

# Read into a local so mypy doesn't narrow each branch to the type-
# checker's host platform and flag the rest of the file as unreachable.
_PLATFORM = sys.platform
if _PLATFORM != "win32":
    pytest.skip("Windows COM-port hardware tests require a Windows host", allow_module_level=True)

pytestmark = [pytest.mark.hardware, pytest.mark.anyio]

_ENV_VAR = "ANYSERIAL_TEST_PORT"


@pytest.fixture
def port_path() -> str:
    """Return the env-supplied COM port or skip the test."""
    path = os.environ.get(_ENV_VAR)
    if not path:
        pytest.skip(f"set {_ENV_VAR} to a Windows COM port, e.g. COM8")
    return path


def _windows_backend(port: SerialPort) -> WindowsBackend:
    backend = port._backend
    assert isinstance(backend, WindowsBackend)
    return backend


async def test_port_info_matches_discovery(port_path: str) -> None:
    listed = await find_serial_port(device=port_path)
    assert listed is not None, f"{port_path} is not in list_serial_ports()"
    assert listed.device == canonical_port_name(port_path)
    async with await open_serial_port(port_path, SerialConfig()) as port:
        assert port.port_info == listed
    if listed.vid is not None:
        assert listed.pid is not None
        assert listed.hwid is not None
        assert listed.hwid.startswith(f"USB VID:PID={listed.vid:04X}:{listed.pid:04X}")


async def test_idle_receive_waits_until_cancelled(port_path: str) -> None:
    async with await open_serial_port(port_path, SerialConfig()) as port:
        with anyio.move_on_after(0.2) as scope:
            await port.receive()
        assert scope.cancelled_caught


async def test_idle_receive_into_waits_until_cancelled(port_path: str) -> None:
    async with await open_serial_port(port_path, SerialConfig()) as port:
        buffer = bytearray(64)
        with anyio.move_on_after(0.2) as scope:
            await port.receive_into(buffer)
        assert scope.cancelled_caught


async def test_modem_event_wait_is_cancellable(port_path: str) -> None:
    async with await open_serial_port(port_path, SerialConfig()) as port:
        backend = _windows_backend(port)
        with anyio.move_on_after(0.2):
            await backend.wait_modem_event()


async def test_close_releases_a_pending_modem_event_wait(port_path: str) -> None:
    port = await open_serial_port(port_path, SerialConfig())
    backend = _windows_backend(port)
    events: list[CommEvent] = []

    async def wait() -> None:
        events.append(await backend.wait_modem_event())

    with anyio.fail_after(5):
        async with anyio.create_task_group() as tg:
            _ = tg.start_soon(wait)
            await anyio.sleep(0.1)
            await port.aclose()
    assert len(events) == 1
