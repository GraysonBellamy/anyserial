"""Hardware test for discovery against a real USB-serial adapter.

Opt-in via the ``ANYSERIAL_TEST_PORT`` environment variable; the path
should point at a USB serial adapter the running user can read. By default
the test expects an FTDI FT232R (VID 0x0403 / PID 0x6001) — the same
device the existing low-latency hardware test uses — but you can override
either ID via ``ANYSERIAL_TEST_VID`` / ``ANYSERIAL_TEST_PID`` if you're
testing a CP210x, CH340, etc.

Verifies five things:

1. :func:`list_serial_ports` includes the adapter at the expected path
   with the expected VID / PID.
2. :func:`find_serial_port` returns the adapter when filtered by VID + PID.
3. :func:`find_serial_port` returns the adapter when filtered by device path.
4. :func:`open_serial_port` populates :attr:`SerialPort.port_info` with
   metadata equivalent to what discovery reported.
5. The native and pyserial backends agree on VID / PID / serial number
   (when ``pyserial`` is installed; otherwise that test skips).

Run via::

    ANYSERIAL_TEST_PORT=/dev/ttyUSB0 uv run pytest -m hardware
"""

from __future__ import annotations

import os
import sys

import pytest

if not sys.platform.startswith("linux"):
    pytest.skip("native FTDI discovery fixtures are Linux-only", allow_module_level=True)

from anyserial import (
    SerialConfig,
    SerialStreamAttribute,
    find_serial_port,
    list_serial_ports,
    open_serial_port,
)

pytestmark = [pytest.mark.hardware, pytest.mark.anyio]

_ENV_PORT = "ANYSERIAL_TEST_PORT"
_ENV_VID = "ANYSERIAL_TEST_VID"
_ENV_PID = "ANYSERIAL_TEST_PID"
# Defaults match the existing FTDI low-latency hardware test so a single
# `ANYSERIAL_TEST_PORT` value drives both suites without further setup.
_DEFAULT_VID = 0x0403
_DEFAULT_PID = 0x6001


def _expected_id(env: str, default: int) -> int:
    raw = os.environ.get(env)
    if raw is None:
        return default
    try:
        # Accept both "0x0403" and "0403" — both parse as base-16.
        return int(raw, 16)
    except ValueError:
        pytest.fail(f"{env}={raw!r} is not a valid hex USB ID")


@pytest.fixture
def adapter_path() -> str:
    """Return the env-supplied device path or skip the test."""
    path = os.environ.get(_ENV_PORT)
    if not path:
        pytest.skip(f"set {_ENV_PORT} to a USB-serial adapter path")
    return path


@pytest.fixture
def expected_vid_pid() -> tuple[int, int]:
    return _expected_id(_ENV_VID, _DEFAULT_VID), _expected_id(_ENV_PID, _DEFAULT_PID)


class TestNativeDiscoveryFindsAdapter:
    async def test_list_includes_adapter_with_expected_ids(
        self,
        adapter_path: str,
        expected_vid_pid: tuple[int, int],
    ) -> None:
        ports = await list_serial_ports()
        match = next((p for p in ports if p.device == adapter_path), None)
        assert match is not None, (
            f"{adapter_path} not in list_serial_ports() — is it visible under /sys/class/tty/?"
        )
        expected_vid, expected_pid = expected_vid_pid
        assert match.vid == expected_vid
        assert match.pid == expected_pid

    async def test_find_by_vid_pid_returns_adapter(
        self,
        adapter_path: str,
        expected_vid_pid: tuple[int, int],
    ) -> None:
        vid, pid = expected_vid_pid
        match = await find_serial_port(vid=vid, pid=pid)
        assert match is not None
        # If multiple adapters with the same VID/PID are connected the env
        # variable disambiguates which one we're asserting against.
        if match.device != adapter_path:
            pytest.skip(
                f"multiple {vid:04X}:{pid:04X} adapters connected; "
                f"first-match returned {match.device!r}, env points at {adapter_path!r}"
            )

    async def test_find_by_device_path_returns_adapter(self, adapter_path: str) -> None:
        match = await find_serial_port(device=adapter_path)
        assert match is not None
        assert match.device == adapter_path


class TestPortInfoEnrichmentOnOpen:
    async def test_open_populates_port_info_typed_attribute(
        self,
        adapter_path: str,
        expected_vid_pid: tuple[int, int],
    ) -> None:
        async with await open_serial_port(adapter_path, SerialConfig(baudrate=9600)) as port:
            info = port.port_info
            assert info is not None
            expected_vid, expected_pid = expected_vid_pid
            assert info.vid == expected_vid
            assert info.pid == expected_pid
            # Extra() lookup must agree with the property.
            assert port.extra(SerialStreamAttribute.port_info) is info

    async def test_open_metadata_matches_discovery_metadata(
        self,
        adapter_path: str,
    ) -> None:
        # The single-entry resolver and the full enumerator must produce
        # equivalent records for the same device — regression guard against
        # the two paths drifting apart.
        listed = await find_serial_port(device=adapter_path)
        assert listed is not None
        async with await open_serial_port(adapter_path, SerialConfig(baudrate=9600)) as port:
            assert port.port_info == listed


class TestBackendAgreement:
    async def test_pyserial_backend_agrees_on_vid_pid_serial(
        self,
        adapter_path: str,
        expected_vid_pid: tuple[int, int],
    ) -> None:
        pytest.importorskip("serial.tools.list_ports")
        native = await find_serial_port(device=adapter_path, backend="native")
        pyserial = await find_serial_port(device=adapter_path, backend="pyserial")
        assert native is not None, "native backend did not find the adapter"
        assert pyserial is not None, "pyserial backend did not find the adapter"
        expected_vid, expected_pid = expected_vid_pid
        assert (native.vid, native.pid) == (expected_vid, expected_pid)
        assert (pyserial.vid, pyserial.pid) == (expected_vid, expected_pid)
        # Serial numbers come from the same sysfs source on Linux, so they
        # must match exactly. Manufacturer / product strings can legitimately
        # differ (pyserial sometimes joins with "_", native preserves spaces),
        # so we don't assert on those.
        assert native.serial_number == pyserial.serial_number

    async def test_pyudev_backend_agrees_on_vid_pid(
        self,
        adapter_path: str,
        expected_vid_pid: tuple[int, int],
    ) -> None:
        pytest.importorskip("pyudev")
        native = await find_serial_port(device=adapter_path, backend="native")
        pyudev_match = await find_serial_port(device=adapter_path, backend="pyudev")
        assert native is not None
        assert pyudev_match is not None
        expected_vid, expected_pid = expected_vid_pid
        assert (pyudev_match.vid, pyudev_match.pid) == (expected_vid, expected_pid)
        assert pyudev_match.serial_number == native.serial_number
