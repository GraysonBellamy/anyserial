r"""Native SetupAPI-based serial port discovery for Windows.

Enumerates COM ports via ``GUID_DEVINTERFACE_COMPORT`` using the
SetupAPI device-interface enumeration surface, then queries registry
properties (``FRIENDLYNAME``, ``HARDWAREID``, ``LOCATION_INFORMATION``)
and the device instance ID for metadata. USB VID / PID / serial number
come from the instance ID — ``USB\VID_xxxx&PID_xxxx\<serial>``, or
``FTDIBUS\VID_xxxx+PID_xxxx+<serial><port letter>\0000`` for ports of
FTDI's VCP driver — with the hardware ID as the fallback for VID / PID.
Hardware IDs never carry a serial number.

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
    MAX_DEVICE_ID_LEN,
    SP_DEVICE_INTERFACE_DATA,
    SP_DEVICE_INTERFACE_DETAIL_DATA_W,
    SP_DEVINFO_DATA,
    SPDRP_FRIENDLYNAME,
    SPDRP_HARDWAREID,
    SPDRP_LOCATION_INFORMATION,
    SetupApiBindings,
    load_setupapi,
)
from anyserial.discovery import PortInfo, canonical_port_name

# Device instance IDs that name a USB serial port:
#
#   USB\VID_10C4&PID_EA60\0001                    serial number "0001"
#   USB\VID_067B&PID_2303\6&406CFD0&0&3           no serial; Windows made one up
#   USB\VID_2341&PID_8036&MI_00\7&2A4F6E1&0&0000  one interface of a composite device
#   FTDIBUS\VID_0403+PID_6001+BG00VBZSA\0000      FTDI VCP: serial "BG00VBZS", port A
#
# The last USB segment is the device's serial number unless it contains "&",
# which marks an ID Windows generated for a device without one.
_USB_INSTANCE_ID_RE = re.compile(
    r"USB\\VID_([0-9A-F]{4})&PID_([0-9A-F]{4})(?:&MI_[0-9A-F]{2})?(?:\\([^\\]+))?",
    re.IGNORECASE,
)
_FTDIBUS_INSTANCE_ID_RE = re.compile(
    r"FTDIBUS\\VID_([0-9A-F]{4})\+PID_([0-9A-F]{4})(?:\+([^\\]+))?",
    re.IGNORECASE,
)

# VID / PID anywhere in a hardware ID: ``USB\VID_067B&PID_2303&REV_0400``,
# ``FTDIBUS\COMPORT&VID_0403&PID_6001``.
_HWID_VID_PID_RE = re.compile(r"VID_([0-9A-F]{4})[&+]PID_([0-9A-F]{4})", re.IGNORECASE)


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
    r"""Resolve a single COM-port path to its :class:`PortInfo`, or ``None``.

    Used by :func:`anyserial.open_serial_port` to populate the
    ``port_info`` typed attribute. Walks the SetupAPI enumeration (then
    the registry fallback) and stops at the first entry whose
    :func:`canonical_port_name` matches ``path``'s, so ``com8``,
    ``\\.\COM8`` and ``\\?\COM8`` all resolve to the ``COM8`` entry.
    """
    wanted = canonical_port_name(path, platform="win32")
    for info in _enumerate_setupapi():
        if canonical_port_name(info.device, platform="win32") == wanted:
            return info
    # Fallback: if SetupAPI didn't find it, try the registry path.
    for info in _enumerate_registry_fallback():
        if canonical_port_name(info.device, platform="win32") == wanted:
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
    instance_id = _get_instance_id(setupapi, dev_info, devinfo)

    # The device path from SetupAPI is the long-form interface path
    # (e.g. \\?\usb#vid_0403&pid_6001#...). We need the short COM name.
    com_name = _extract_com_name(friendly) or _extract_com_name_from_path(device_path)
    device = com_name or device_path

    vid, pid, serial_number = _parse_device_ids(instance_id, hardware_id)

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


def _get_instance_id(
    setupapi: SetupApiBindings,
    dev_info: int,
    devinfo: SP_DEVINFO_DATA,
) -> str | None:
    r"""Read the device instance ID (``USB\VID_…\…``), or ``None`` on failure."""
    buf = create_unicode_buffer(MAX_DEVICE_ID_LEN + 1)
    ok = setupapi.SetupDiGetDeviceInstanceIdW(
        dev_info,
        byref(devinfo),
        buf,
        len(buf),
        None,
    )
    if not ok:
        return None
    return buf.value or None


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


def _parse_device_ids(
    instance_id: str | None,
    hardware_id: str | None,
) -> tuple[int | None, int | None, str | None]:
    """Return ``(vid, pid, serial_number)`` for a port's device.

    The instance ID comes first: it is the only ID that records the serial
    number. When it names no VID / PID, they come from the hardware ID and
    the serial number stays ``None``. Ports that are not USB (PCI, ACPI)
    give ``(None, None, None)``.
    """
    ids = _parse_instance_id(instance_id)
    if ids[0] is not None:
        return ids
    vid, pid = _parse_hardware_id(hardware_id)
    return vid, pid, None


def _parse_instance_id(instance_id: str | None) -> tuple[int | None, int | None, str | None]:
    r"""Parse a USB or FTDIBUS device instance ID into ``(vid, pid, serial)``.

    Returns ``(None, None, None)`` for any other bus.
    """
    if instance_id is None:
        return None, None, None
    if (m := _USB_INSTANCE_ID_RE.match(instance_id)) is not None:
        return int(m.group(1), 16), int(m.group(2), 16), _usb_serial_number(m.group(3))
    if (m := _FTDIBUS_INSTANCE_ID_RE.match(instance_id)) is not None:
        return int(m.group(1), 16), int(m.group(2), 16), _ftdi_serial_number(m.group(3))
    return None, None, None


def _parse_hardware_id(hwid: str | None) -> tuple[int | None, int | None]:
    r"""Return ``(vid, pid)`` named anywhere in a hardware ID, or ``(None, None)``.

    Matches both ``USB\VID_xxxx&PID_xxxx…`` and FTDI's
    ``FTDIBUS\COMPORT&VID_xxxx&PID_xxxx``.
    """
    if hwid is None:
        return None, None
    m = _HWID_VID_PID_RE.search(hwid)
    if m is None:
        return None, None
    return int(m.group(1), 16), int(m.group(2), 16)


def _usb_serial_number(segment: str | None) -> str | None:
    """Return the serial number in a USB instance ID's last segment.

    ``None`` when the segment is missing or contains ``&``, which marks an
    ID Windows generated for a device that reports no serial number.
    """
    if not segment or "&" in segment:
        return None
    return segment


def _ftdi_serial_number(segment: str | None) -> str | None:
    """Return the chip serial number in an FTDIBUS instance ID segment.

    FTDI's driver appends a port letter to the serial number (``A`` for
    the first port), so an FT232R with serial ``BG00VBZS`` appears as
    ``BG00VBZSA`` and the ports of an FT2232H with serial ``FT5ABCDE`` as
    ``FT5ABCDEA`` and ``FT5ABCDEB``. The letter is removed so the value is
    the USB serial number other platforms report. ``None`` when the
    segment is missing or is an ID containing ``&`` rather than a serial
    number.
    """
    if not segment or "&" in segment:
        return None
    if len(segment) > 1 and "A" <= segment[-1] <= "Z":
        return segment[:-1]
    return segment


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
