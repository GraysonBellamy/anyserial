"""Optional pyudev-backed Linux discovery.

``udev`` enriches raw sysfs attributes with rules-driven metadata —
canonical USB property names (``ID_VENDOR_ID``, ``ID_SERIAL_SHORT``,
``ID_PATH``), more reliable interface descriptions, etc. Users running
udev-enabled distros who already pin ``pyudev`` for other tooling can opt
into this enumerator by passing ``backend="pyudev"`` to
:func:`anyserial.list_serial_ports`.

Linux-only by construction. Requires the ``anyserial[discovery-pyudev]``
extra; missing-package errors include the install command verbatim so the
remediation path is one copy-paste.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from anyserial.discovery import PortInfo
from anyserial.exceptions import UnsupportedPlatformError

if TYPE_CHECKING:
    from collections.abc import Iterable

_INSTALL_HINT = "pip install 'anyserial[discovery-pyudev]'"


def enumerate_ports() -> list[PortInfo]:
    """Return tty devices enumerated through ``pyudev``.

    Returns:
        Sorted list of :class:`PortInfo`. Same shape and semantics as the
        native sysfs walker, populated from udev properties when present.

    Raises:
        UnsupportedPlatformError: Called on a non-Linux host. ``pyudev`` is
            a thin wrapper around ``libudev`` and only works on Linux.
        ImportError: The ``anyserial[discovery-pyudev]`` extra is not
            installed. Message includes the exact install command.
    """
    if not sys.platform.startswith("linux"):
        msg = "pyudev discovery is Linux-only (libudev binding)"
        raise UnsupportedPlatformError(msg)

    try:
        import pyudev  # type: ignore[import-untyped]  # noqa: PLC0415 — lazy by extra
    except ImportError as exc:
        msg = f"pyudev not installed; {_INSTALL_HINT}"
        raise ImportError(msg) from exc

    context = pyudev.Context()
    devices: Iterable[object] = context.list_devices(subsystem="tty")  # pyright: ignore[reportUnknownMemberType]
    return sorted(_iter_devices(devices), key=lambda p: p.device)


def _iter_devices(devices: Iterable[object]) -> Iterable[PortInfo]:
    """Convert each ``pyudev.Device`` to a :class:`PortInfo`, skipping virtual ones."""
    for device in devices:
        info = _device_to_port_info(device)
        if info is not None:
            yield info


def _device_to_port_info(device: object) -> PortInfo | None:
    """Return a :class:`PortInfo` for a single ``pyudev.Device``, or ``None`` to skip.

    Skips entries with no ``device_node`` (no ``/dev`` entry to point at)
    and entries whose sys path is under ``/sys/devices/virtual/`` — the
    vt-console pseudo-ttys we never want to surface as serial ports.
    """
    device_node = getattr(device, "device_node", None)
    if not device_node:
        return None
    sys_path = getattr(device, "sys_path", "") or ""
    if "/devices/virtual/" in sys_path:
        return None

    name = getattr(device, "sys_name", None)
    vid = _parse_hex(_prop(device, "ID_VENDOR_ID"))
    pid = _parse_hex(_prop(device, "ID_MODEL_ID"))
    serial_number = _prop(device, "ID_SERIAL_SHORT")
    manufacturer = _prop(device, "ID_VENDOR_FROM_DATABASE") or _prop(device, "ID_VENDOR")
    product = _prop(device, "ID_MODEL_FROM_DATABASE") or _prop(device, "ID_MODEL")
    location = _prop(device, "ID_PATH")
    interface = _prop(device, "ID_USB_INTERFACE_NUM")

    return PortInfo(
        device=str(device_node),
        name=name,
        description=product,
        hwid=_format_hwid(vid, pid, serial_number, location),
        vid=vid,
        pid=pid,
        serial_number=serial_number,
        manufacturer=manufacturer,
        product=product,
        location=location,
        interface=interface,
    )


def _prop(device: object, key: str) -> str | None:
    """Read a udev property, normalizing missing/empty values to ``None``.

    ``pyudev.Device`` exposes a mapping interface; the safest call is
    ``.get(key)`` which returns ``None`` for absent keys without raising.
    """
    properties = getattr(device, "properties", None)
    if properties is None:
        return None
    try:
        value = properties.get(key)
    except (KeyError, AttributeError):
        return None
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_hex(value: str | None) -> int | None:
    """Parse a 4-char udev hex string into an int, or ``None`` on failure."""
    if value is None:
        return None
    try:
        return int(value, 16)
    except ValueError:
        return None


def _format_hwid(
    vid: int | None,
    pid: int | None,
    serial: str | None,
    location: str | None,
) -> str | None:
    """Build the same ``USB VID:PID=…`` string the native walker emits."""
    if vid is None or pid is None:
        return None
    parts = [f"USB VID:PID={vid:04X}:{pid:04X}"]
    if serial:
        parts.append(f"SER={serial}")
    if location:
        parts.append(f"LOCATION={location}")
    return " ".join(parts)


__all__ = ["enumerate_ports"]
