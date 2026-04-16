"""Native sysfs-based serial port discovery for Linux.

Walks ``/sys/class/tty`` (one directory entry per tty driver instance),
filters out virtual consoles (``/sys/devices/virtual/tty/*`` — pseudo
terminals, the kernel console, etc.), and resolves the remaining entries to
:class:`PortInfo` records, climbing the device tree to find the parent USB
device when one exists so VID / PID / serial number / manufacturer / product
strings can be reported.

Pure sync — :func:`anyserial.discovery.list_serial_ports` runs the
enumeration in a worker thread via ``anyio.to_thread.run_sync``. No AnyIO
imports here. Production callers use the zero-arg :func:`enumerate_ports`
exported below; unit tests pass ``sys_root`` / ``dev_root`` overrides
directly so they can exercise the full walk against a synthetic sysfs tree
without touching the host.

The metadata format mirrors what ``pyserial.tools.list_ports`` reports for
the same hardware (notably the ``USB VID:PID=…`` ``hwid`` string), so users
migrating from pySerial see familiar values without us depending on it.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from pathlib import Path

from anyserial.discovery import PortInfo

_DEFAULT_SYS_ROOT = Path("/sys/class/tty")
_DEFAULT_DEV_ROOT = Path("/dev")


@dataclass(frozen=True, slots=True)
class _UsbInfo:
    """Subset of :class:`PortInfo` populated from the USB ancestor, if any."""

    vid: int | None = None
    pid: int | None = None
    serial_number: str | None = None
    manufacturer: str | None = None
    product: str | None = None
    location: str | None = None
    interface: str | None = None


def resolve_port_info(
    path: str,
    *,
    sys_root: Path = _DEFAULT_SYS_ROOT,
    dev_root: Path = _DEFAULT_DEV_ROOT,
) -> PortInfo | None:
    """Resolve a single ``/dev/...`` path to its :class:`PortInfo`, or ``None``.

    Single-entry lookup so :func:`anyserial.open_serial_port` can populate
    the ``port_info`` typed attribute without paying for a full
    :func:`enumerate_ports` walk.

    Args:
        path: Device path the caller is opening (e.g. ``/dev/ttyUSB0``).
        sys_root: Root of the tty class directory; tests override.
        dev_root: Root of the device-node tree; tests override.

    Returns:
        :class:`PortInfo` for the resolved entry, or ``None`` if the path
        does not map to a ``/sys/class/tty`` entry (pseudo terminals,
        unknown paths), the entry has no ``device`` symlink, or the target
        resolves under ``/sys/devices/virtual/`` (kernel console).
    """
    name = Path(path).name
    if not name:
        return None
    entry = sys_root / name
    if not entry.is_dir():
        return None
    return _resolve_entry(entry, dev_root=dev_root)


def enumerate_ports(
    *,
    sys_root: Path = _DEFAULT_SYS_ROOT,
    dev_root: Path = _DEFAULT_DEV_ROOT,
) -> list[PortInfo]:
    """Enumerate serial ports the kernel exposes under ``/sys/class/tty``.

    Args:
        sys_root: Root of the tty class directory. Production uses
            ``/sys/class/tty``; tests substitute a tmp_path tree.
        dev_root: Root of the device-node tree. Production uses ``/dev``;
            tests substitute a tmp_path tree. The discovery layer never
            opens these nodes — it just builds the path string.

    Returns:
        A list of :class:`PortInfo`, sorted by device-node path for stable
        ordering. Empty if the sys root does not exist (containers,
        sandboxed CI runners, mock fixtures with no entries).
    """
    if not sys_root.is_dir():
        return []

    ports: list[PortInfo] = []
    for entry in sorted(sys_root.iterdir(), key=lambda p: p.name):
        info = _resolve_entry(entry, dev_root=dev_root)
        if info is not None:
            ports.append(info)
    return ports


def _resolve_entry(entry: Path, *, dev_root: Path) -> PortInfo | None:
    """Build a :class:`PortInfo` for one ``/sys/class/tty`` entry, or skip it.

    Returns ``None`` for entries that do not represent a real serial device:
    missing ``device`` symlink (kernel console, virtual ttys without a
    backing driver), ``device`` resolving under ``/sys/devices/virtual/``
    (pseudo terminals, ``console``, ``tty0..tty63``), or transient errors
    from a USB device unplugged mid-walk.
    """
    device_link = entry / "device"
    if not device_link.exists():
        return None

    try:
        device_target = device_link.resolve(strict=True)
    except OSError:
        return None

    # Filter virtual consoles up front — pyserial does the same.
    if "virtual" in device_target.parts:
        return None

    name = entry.name
    usb = _resolve_usb(device_target)
    return PortInfo(
        device=str(dev_root / name),
        name=name,
        description=usb.product,
        hwid=_format_hwid(usb),
        vid=usb.vid,
        pid=usb.pid,
        serial_number=usb.serial_number,
        manufacturer=usb.manufacturer,
        product=usb.product,
        location=usb.location,
        interface=usb.interface,
    )


def _resolve_usb(device_target: Path) -> _UsbInfo:
    """Collect USB metadata for an entry whose ``device`` resolves to ``device_target``.

    For USB-serial adapters ``device_target`` is the USB *interface* dir
    (e.g. ``…/usb1/1-1/1-1:1.0``); the interface name string lives there
    while VID / PID / serial / manufacturer / product live one level up on
    the USB *device* dir (``…/usb1/1-1``). Walks up from the interface
    looking for the first ancestor that exposes ``idVendor`` — handles
    nested hubs and multi-function devices alike.

    Non-USB devices (on-board UARTs, PCI serial, platform serial) have no
    USB ancestor; they get a :class:`_UsbInfo` with every field ``None``.
    """
    interface = _read_text(device_target / "interface")
    usb_dev = _find_usb_device(device_target)
    if usb_dev is None:
        return _UsbInfo(interface=interface)
    return _UsbInfo(
        vid=_parse_hex(_read_text(usb_dev / "idVendor")),
        pid=_parse_hex(_read_text(usb_dev / "idProduct")),
        serial_number=_read_text(usb_dev / "serial"),
        manufacturer=_read_text(usb_dev / "manufacturer"),
        product=_read_text(usb_dev / "product"),
        location=usb_dev.name,
        interface=interface,
    )


def _find_usb_device(start: Path) -> Path | None:
    """Walk up from ``start`` to the first directory containing ``idVendor``.

    Stops at the filesystem root (``parent == self``) and returns ``None``
    if no USB ancestor is reachable, which is the normal case for non-USB
    serial ports.
    """
    cur = start
    while cur != cur.parent:
        if (cur / "idVendor").is_file():
            return cur
        cur = cur.parent
    return None


def _read_text(path: Path) -> str | None:
    """Read a one-line sysfs attribute, or return ``None`` on any failure.

    sysfs files can disappear mid-read (USB unplug), be unreadable
    (permissions), or contain non-UTF-8 bytes — every such case maps to
    ``None`` so a single broken attribute never aborts the whole walk.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    return text or None


def _parse_hex(value: str | None) -> int | None:
    """Parse a 4-char sysfs hex string (``"0403"``) into an int, or ``None``."""
    if value is None:
        return None
    with contextlib.suppress(ValueError):
        return int(value, 16)
    return None


def _format_hwid(info: _UsbInfo) -> str | None:
    """Build the pyserial-compatible ``USB VID:PID=…`` string, or ``None``.

    Returns ``None`` when no USB ancestor was found — non-USB ports have
    no canonical hwid string and forcing one would mask the distinction.
    """
    if info.vid is None or info.pid is None:
        return None
    parts = [f"USB VID:PID={info.vid:04X}:{info.pid:04X}"]
    if info.serial_number:
        parts.append(f"SER={info.serial_number}")
    if info.location:
        parts.append(f"LOCATION={info.location}")
    return " ".join(parts)


__all__ = ["enumerate_ports", "resolve_port_info"]
