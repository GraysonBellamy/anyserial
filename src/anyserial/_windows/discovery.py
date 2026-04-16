r"""Native SetupAPI-based serial port discovery for Windows.

Enumerates COM ports via ``GUID_DEVINTERFACE_COMPORT`` using the
SetupAPI device-interface enumeration surface, then queries registry
properties (``FRIENDLYNAME``, ``HARDWAREID``, ``LOCATION_INFORMATION``)
for metadata. USB VID / PID / serial number are parsed from the hardware
ID string, which follows the format ``USB\VID_xxxx&PID_xxxx\serial``.

Pure sync — :func:`anyserial.discovery.list_serial_ports` runs the
enumeration in a worker thread via ``anyio.to_thread.run_sync``. No
AnyIO imports here. The SetupAPI ctypes bindings live in
:mod:`anyserial._windows._setupapi`; this module consumes them directly
(no Protocol indirection — SetupAPI is stable enough that a full
abstraction layer would be pure overhead, unlike IOKit where the ctypes
surface benefits from a testable Protocol).

Fallback: if SetupAPI enumeration returns zero devices (missing driver,
broken installation), a registry-based fallback reads
``HKLM\HARDWARE\DEVICEMAP\SERIALCOMM`` to discover device names
without any metadata. This matches pySerial's fallback behaviour.

References:
- design-windows-backend.md §8 (Port discovery).
- MS Learn: GUID_DEVINTERFACE_COMPORT, SetupDi* API family.
"""

from __future__ import annotations

import re
from ctypes import byref, c_uint32, c_void_p, create_unicode_buffer, sizeof
from typing import Any

from anyserial._windows._setupapi import (
    DETAIL_CB_SIZE,
    DIGCF_DEVICEINTERFACE,
    DIGCF_PRESENT,
    GUID_DEVINTERFACE_COMPORT,
    INVALID_HANDLE_VALUE,
    SP_DEVICE_INTERFACE_DATA,
    SP_DEVICE_INTERFACE_DETAIL_DATA_W,
    SP_DEVINFO_DATA,
    SPDRP_FRIENDLYNAME,
    SPDRP_HARDWAREID,
    SPDRP_LOCATION_INFORMATION,
    SetupApiBindings,
    load_setupapi,
)
from anyserial.discovery import PortInfo

# Hardware ID pattern: USB\VID_xxxx&PID_xxxx\serial_string
# The VID and PID are 4-char hex; the trailing segment (after the second
# backslash) is the device serial number, which may be absent.
_USB_HWID_RE = re.compile(
    r"USB\\VID_([0-9A-Fa-f]{4})&PID_([0-9A-Fa-f]{4})(?:\\(.+))?",
)


def enumerate_ports() -> list[PortInfo]:
    """Enumerate serial ports via SetupAPI ``GUID_DEVINTERFACE_COMPORT``.

    Returns a list of :class:`PortInfo`, sorted by device path for stable
    ordering. Falls back to the registry-based enumerator if SetupAPI
    yields nothing (§8 fallback).
    """
    ports = _enumerate_setupapi()
    if not ports:
        ports = _enumerate_registry_fallback()
    ports.sort(key=lambda p: p.device)
    return ports


def resolve_port_info(path: str) -> PortInfo | None:
    """Resolve a single COM-port path to its :class:`PortInfo`, or ``None``.

    Single-entry lookup so :func:`anyserial.open_serial_port` can
    populate the ``port_info`` typed attribute without paying for a full
    enumeration walk.
    """
    # Normalise: strip the \\.\ prefix for comparison against SetupAPI
    # device paths, which use the short form.
    normalised = _strip_dos_prefix(path).upper()
    for info in _enumerate_setupapi():
        if _strip_dos_prefix(info.device).upper() == normalised:
            return info
    # Fallback: if SetupAPI didn't find it, try the registry path.
    for info in _enumerate_registry_fallback():
        if _strip_dos_prefix(info.device).upper() == normalised:
            return info
    return None


# ---------------------------------------------------------------------------
# SetupAPI enumeration
# ---------------------------------------------------------------------------


def _enumerate_setupapi() -> list[PortInfo]:
    """Walk SetupAPI device interfaces for COM ports."""
    setupapi = load_setupapi()

    dev_info = setupapi.SetupDiGetClassDevsW(
        byref(GUID_DEVINTERFACE_COMPORT),
        None,
        None,
        DIGCF_PRESENT | DIGCF_DEVICEINTERFACE,
    )
    if dev_info is None or dev_info == c_void_p(INVALID_HANDLE_VALUE).value:
        return []

    ports: list[PortInfo] = []
    try:
        index = 0
        while True:
            iface_data = SP_DEVICE_INTERFACE_DATA()
            iface_data.cbSize = sizeof(SP_DEVICE_INTERFACE_DATA)

            ok = setupapi.SetupDiEnumDeviceInterfaces(
                dev_info,
                None,
                byref(GUID_DEVINTERFACE_COMPORT),
                index,
                byref(iface_data),
            )
            if not ok:
                break  # ERROR_NO_MORE_ITEMS — enumeration complete

            info = _resolve_interface(setupapi, dev_info, iface_data)
            if info is not None:
                ports.append(info)
            index += 1
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(dev_info)

    return ports


def _resolve_interface(
    setupapi: SetupApiBindings,
    dev_info: int,
    iface_data: SP_DEVICE_INTERFACE_DATA,
) -> PortInfo | None:
    """Build a :class:`PortInfo` for one device interface, or ``None``.

    Calls ``SetupDiGetDeviceInterfaceDetailW`` to get the device path,
    then queries registry properties for metadata.
    """
    # Get the device interface detail (contains the device path) and the
    # devinfo data (needed for registry property queries).
    detail = SP_DEVICE_INTERFACE_DETAIL_DATA_W()
    detail.cbSize = DETAIL_CB_SIZE
    devinfo = SP_DEVINFO_DATA()
    devinfo.cbSize = sizeof(SP_DEVINFO_DATA)
    required = c_uint32(0)

    ok = setupapi.SetupDiGetDeviceInterfaceDetailW(
        dev_info,
        byref(iface_data),
        byref(detail),
        sizeof(detail),
        byref(required),
        byref(devinfo),
    )
    if not ok:
        return None

    device_path = detail.DevicePath

    # Extract a short COM name from the friendly name or device path.
    friendly = _get_registry_string(setupapi, dev_info, devinfo, SPDRP_FRIENDLYNAME)
    hardware_id = _get_registry_string(setupapi, dev_info, devinfo, SPDRP_HARDWAREID)
    location = _get_registry_string(setupapi, dev_info, devinfo, SPDRP_LOCATION_INFORMATION)

    # The device path from SetupAPI is the long-form interface path
    # (e.g. \\?\usb#vid_0403&pid_6001#...). We need the short COM name.
    com_name = _extract_com_name(friendly) or _extract_com_name_from_path(device_path)
    device = com_name or device_path

    # Parse USB VID/PID/serial from hardware ID.
    vid, pid, serial_number = _parse_hardware_id(hardware_id)

    return PortInfo(
        device=device,
        name=com_name,
        description=friendly,
        hwid=_format_hwid(vid, pid, serial_number, location),
        vid=vid,
        pid=pid,
        serial_number=serial_number,
        manufacturer=None,  # Not available via SetupAPI registry properties
        product=_strip_com_suffix(friendly),
        location=location,
        interface=None,
    )


# ---------------------------------------------------------------------------
# Registry property helpers
# ---------------------------------------------------------------------------


def _get_registry_string(
    setupapi: SetupApiBindings,
    dev_info: int,
    devinfo: SP_DEVINFO_DATA,
    prop: int,
) -> str | None:
    """Read a string registry property, or ``None`` on any failure."""
    buf = create_unicode_buffer(1024)
    reg_type = c_uint32(0)
    required = c_uint32(0)

    ok = setupapi.SetupDiGetDeviceRegistryPropertyW(
        dev_info,
        byref(devinfo),
        prop,
        byref(reg_type),
        buf,
        sizeof(buf),
        byref(required),
    )
    if not ok:
        return None

    value = buf.value.strip()
    return value or None


# ---------------------------------------------------------------------------
# Registry fallback
# ---------------------------------------------------------------------------


def _enumerate_registry_fallback() -> list[PortInfo]:
    r"""Fallback: read ``HKLM\HARDWARE\DEVICEMAP\SERIALCOMM`` via ``winreg``.

    This key lists active COM ports as value-name → device-name pairs.
    No metadata is available — only the device name. Used when SetupAPI
    enumeration returns nothing (e.g. missing driver).
    """
    # ``winreg`` is a Windows-only stdlib module; mypy/pyright on POSIX
    # hosts have no stubs for its attributes. Cast through ``Any`` at the
    # single import point so the rest of the function stays readable
    # instead of littered with per-access ``# type: ignore`` hints.
    try:
        import winreg as _winreg  # noqa: PLC0415 — Windows-only stdlib
    except ImportError:
        return []
    winreg: Any = _winreg

    ports: list[PortInfo] = []
    try:
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"HARDWARE\DEVICEMAP\SERIALCOMM",
        )
    except OSError:
        return []

    with key:
        index = 0
        while True:
            try:
                _name, value, _type = winreg.EnumValue(key, index)
            except OSError:
                break
            if isinstance(value, str) and value:
                ports.append(
                    PortInfo(
                        device=value,
                        name=value,
                    )
                )
            index += 1

    return ports


# ---------------------------------------------------------------------------
# String parsing helpers
# ---------------------------------------------------------------------------


def _parse_hardware_id(hwid: str | None) -> tuple[int | None, int | None, str | None]:
    r"""Parse ``USB\VID_xxxx&PID_xxxx\serial`` into (vid, pid, serial).

    Returns ``(None, None, None)`` for non-USB or unparseable strings.
    """
    if hwid is None:
        return None, None, None
    m = _USB_HWID_RE.search(hwid)
    if m is None:
        return None, None, None
    vid = int(m.group(1), 16)
    pid = int(m.group(2), 16)
    serial = m.group(3) or None
    return vid, pid, serial


def _extract_com_name(friendly: str | None) -> str | None:
    """Extract ``COM3`` from ``"USB Serial Port (COM3)"`` or similar.

    Windows friendly names for serial ports almost always end with
    ``(COMn)`` where *n* is the port number.
    """
    if friendly is None:
        return None
    m = re.search(r"\(COM\d+\)", friendly)
    if m is None:
        return None
    # Strip the parentheses.
    return m.group(0)[1:-1]


def _extract_com_name_from_path(device_path: str) -> str | None:
    r"""Try to extract a COM name from a SetupAPI device interface path.

    The device path is typically something like
    ``\\?\usb#vid_0403&pid_6001#...#{guid}``. This is a last resort;
    the friendly-name extraction is preferred.
    """
    # Some drivers embed the COM number in the path, but this is not
    # guaranteed. Return None so the caller falls back to the raw path.
    return None


def _strip_com_suffix(friendly: str | None) -> str | None:
    """Return ``"USB Serial Port"`` from ``"USB Serial Port (COM3)"``."""
    if friendly is None:
        return None
    result = re.sub(r"\s*\(COM\d+\)", "", friendly).strip()
    return result or None


def _strip_dos_prefix(path: str) -> str:
    r"""Strip the ``\\.\`` or ``\\?\`` DOS device prefix if present."""
    if path.startswith(("\\\\.\\", "\\\\?\\")):
        return path[4:]
    return path


def _format_hwid(
    vid: int | None,
    pid: int | None,
    serial_number: str | None,
    location: str | None,
) -> str | None:
    """Build the pyserial-compatible ``USB VID:PID=…`` string, or ``None``."""
    if vid is None or pid is None:
        return None
    parts = [f"USB VID:PID={vid:04X}:{pid:04X}"]
    if serial_number:
        parts.append(f"SER={serial_number}")
    if location:
        parts.append(f"LOCATION={location}")
    return " ".join(parts)


__all__ = ["enumerate_ports", "resolve_port_info"]
