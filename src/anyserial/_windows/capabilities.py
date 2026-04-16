"""Capability snapshot for :class:`WindowsBackend`.

Mirrors design-windows-backend.md §7. Most rows are firm answers because
the Win32 surface is uniform across drivers — the unknowns sit at the
device level (whether a USB-serial adapter actually honours an unusual
baud or hardware-handshake setting), where they surface at apply time as
:class:`anyserial.exceptions.UnsupportedConfigurationError` rather than
through a tri-state capability.
"""

from __future__ import annotations

from anyserial._types import Capability
from anyserial.capabilities import SerialCapabilities


def windows_capabilities() -> SerialCapabilities:
    """Return the capability snapshot every :class:`WindowsBackend` reports."""
    return SerialCapabilities(
        platform="windows",
        backend="windows",
        # DCB.BaudRate is an integer; the driver decides what it accepts.
        custom_baudrate=Capability.SUPPORTED,
        # MARKPARITY / SPACEPARITY are first-class DCB.Parity values.
        mark_space_parity=Capability.SUPPORTED,
        # ONE5STOPBITS is a first-class DCB.StopBits value.
        one_point_five_stop_bits=Capability.SUPPORTED,
        # DCB.fOutX / fInX.
        xon_xoff=Capability.SUPPORTED,
        # DCB.fOutxCtsFlow + fRtsControl=RTS_CONTROL_HANDSHAKE.
        rts_cts=Capability.SUPPORTED,
        # DCB.fOutxDsrFlow + fDtrControl=DTR_CONTROL_HANDSHAKE.
        dtr_dsr=Capability.SUPPORTED,
        # GetCommModemStatus.
        modem_lines=Capability.SUPPORTED,
        # SetCommBreak / ClearCommBreak.
        break_signal=Capability.SUPPORTED,
        # CreateFileW with dwShareMode=0 — always exclusive on Windows.
        # design-windows-backend.md §7 marks this SUPPORTED-BY-DEFAULT;
        # there is no Win32 way to disable it, so we report SUPPORTED.
        exclusive_access=Capability.SUPPORTED,
        # No Win32 equivalent to ASYNC_LOW_LATENCY; FTDI's latency timer
        # is a driver-GUI setting. Documented in design-windows-backend §7.
        low_latency=Capability.UNSUPPORTED,
        # Out of scope — design-windows-backend.md §12 / §7.
        rs485=Capability.UNSUPPORTED,
        # ClearCommError → COMSTAT.cbInQue / cbOutQue.
        input_waiting=Capability.SUPPORTED,
        output_waiting=Capability.SUPPORTED,
        # SetupAPI-based enumeration via GUID_DEVINTERFACE_COMPORT.
        port_discovery=Capability.SUPPORTED,
    )


__all__ = ["windows_capabilities"]
