"""Capability snapshot for :class:`WindowsBackend`.

Pin the design-windows-backend.md §7 matrix here so a refactor that
silently downgrades a row (e.g. flips ``rts_cts`` to UNKNOWN) gets
caught at unit-test time.
"""

from __future__ import annotations

from anyserial._types import Capability
from anyserial._windows.capabilities import windows_capabilities


def test_platform_and_backend_strings() -> None:
    caps = windows_capabilities()
    assert caps.platform == "windows"
    assert caps.backend == "windows"


def test_supported_rows() -> None:
    caps = windows_capabilities()
    for row in (
        caps.custom_baudrate,
        caps.mark_space_parity,
        caps.one_point_five_stop_bits,
        caps.xon_xoff,
        caps.rts_cts,
        caps.dtr_dsr,
        caps.modem_lines,
        caps.break_signal,
        caps.exclusive_access,
        caps.input_waiting,
        caps.output_waiting,
        caps.port_discovery,
    ):
        assert row is Capability.SUPPORTED


def test_unsupported_rows() -> None:
    caps = windows_capabilities()
    # No Win32 ASYNC_LOW_LATENCY equivalent; FTDI's latency timer is a
    # driver-GUI setting (design-windows-backend.md §7).
    assert caps.low_latency is Capability.UNSUPPORTED
    # RS-485 is out of scope (design-windows-backend.md §12).
    assert caps.rs485 is Capability.UNSUPPORTED
