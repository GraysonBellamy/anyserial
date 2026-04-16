"""Native IOKit-based serial port discovery for Darwin.

Walks the ``IOSerialBSDClient`` service class, resolves each entry to its
``/dev/cu.*`` callout path, and climbs the IOService parent tree to
populate USB VID / PID / serial / manufacturer / product / location
metadata on adapters that expose it. The result format mirrors what
:mod:`anyserial._linux.discovery` returns, including the pyserial-
compatible ``USB VID:PID=…`` ``hwid`` string, so users migrating from
pySerial see familiar values without us depending on it.

Pure sync — :func:`anyserial.discovery.list_serial_ports` runs the
enumeration in a worker thread via ``anyio.to_thread.run_sync``. No
AnyIO imports here. The IOKit ctypes facade lives in
:mod:`anyserial._darwin._iokit`; this module consumes the
:class:`IOKitClient` Protocol so tests can inject an in-memory fake
that never touches real Darwin frameworks.

Design note: Darwin publishes two device nodes per serial port —
``/dev/cu.<name>`` (callout) and ``/dev/tty.<name>`` (dial-in). We
prefer the callout path because it doesn't block on carrier detect,
matching pyserial's default and what the vast majority of applications
actually want. If ``IOCalloutDevice`` is missing we fall back to
``IODialinDevice`` rather than skipping the port entirely.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from anyserial._darwin._iokit import (
    IO_CALLOUT_DEVICE_KEY,
    IO_DIALIN_DEVICE_KEY,
    USB_LOCATION_ID_KEY,
    USB_PRODUCT_ID_KEY,
    USB_PRODUCT_NAME_KEY,
    USB_SERIAL_NUMBER_KEY,
    USB_VENDOR_ID_KEY,
    USB_VENDOR_NAME_KEY,
    default_client,
)
from anyserial.discovery import PortInfo

if TYPE_CHECKING:
    from anyserial._darwin._iokit import IOKitClient, ServiceHandle


@dataclass(frozen=True, slots=True)
class _UsbInfo:
    """Subset of :class:`PortInfo` populated from the USB ancestor, if any."""

    vid: int | None = None
    pid: int | None = None
    serial_number: str | None = None
    manufacturer: str | None = None
    product: str | None = None
    location: str | None = None


def enumerate_ports(*, client: IOKitClient | None = None) -> list[PortInfo]:
    """Enumerate serial ports the Darwin kernel exposes via IOKit.

    Args:
        client: Optional :class:`IOKitClient` override. Defaults to the
            real ctypes-backed client from :func:`default_client`. Tests
            pass an in-memory fake that implements the same Protocol so
            the walk logic runs deterministically on any host.

    Returns:
        A list of :class:`PortInfo`, sorted by device-node path for
        stable ordering. Empty when IOKit reports no serial services
        (an unusual but valid state on, e.g., a container-like VM
        without a configured serial console).
    """
    real_client = client if client is not None else default_client()
    ports: list[PortInfo] = []
    for service in real_client.list_serial_services():
        try:
            info = _resolve_service(real_client, service)
            if info is not None:
                ports.append(info)
        finally:
            real_client.release(service)
    ports.sort(key=lambda p: p.device)
    return ports


def resolve_port_info(
    path: str,
    *,
    client: IOKitClient | None = None,
) -> PortInfo | None:
    """Resolve a single ``/dev/cu.*`` or ``/dev/tty.*`` path, or ``None``.

    Single-entry lookup so :func:`anyserial.open_serial_port` can
    populate the ``port_info`` typed attribute without paying for a full
    :func:`enumerate_ports` walk over every serial device on the system.

    Args:
        path: Device path the caller is opening (e.g.
            ``/dev/cu.usbserial-A12345``). Matched against the primary
            callout device and the dial-in alias.
        client: See :func:`enumerate_ports`.

    Returns:
        :class:`PortInfo` for the resolved entry, or ``None`` if no
        service reports ``path`` as either its callout or dial-in node.
    """
    real_client = client if client is not None else default_client()
    for service in real_client.list_serial_services():
        try:
            callout = real_client.get_string(service, IO_CALLOUT_DEVICE_KEY)
            dialin = real_client.get_string(service, IO_DIALIN_DEVICE_KEY)
            if path in (callout, dialin):
                return _resolve_service(real_client, service)
        finally:
            real_client.release(service)
    return None


def _resolve_service(client: IOKitClient, service: ServiceHandle) -> PortInfo | None:
    """Build a :class:`PortInfo` for one IOKit service entry, or skip it.

    Returns ``None`` when the service lacks both ``IOCalloutDevice`` and
    ``IODialinDevice`` — that almost never happens in practice (every
    ``IOSerialBSDClient`` registers at least one device node), but
    guarding against it keeps the walk robust if Apple ever loosens the
    registration contract.
    """
    callout = client.get_string(service, IO_CALLOUT_DEVICE_KEY)
    dialin = client.get_string(service, IO_DIALIN_DEVICE_KEY)
    device = callout or dialin
    if device is None:
        return None

    usb = _resolve_usb(client, service)
    name = _device_base_name(device)
    return PortInfo(
        device=device,
        name=name,
        description=usb.product,
        hwid=_format_hwid(usb),
        vid=usb.vid,
        pid=usb.pid,
        serial_number=usb.serial_number,
        manufacturer=usb.manufacturer,
        product=usb.product,
        location=usb.location,
        # Darwin doesn't split a USB "interface" string the way Linux's
        # sysfs does; leaving the field ``None`` matches pySerial's
        # Darwin output and keeps the shape of :class:`PortInfo` honest.
        interface=None,
    )


def _resolve_usb(client: IOKitClient, service: ServiceHandle) -> _UsbInfo:
    """Collect USB metadata for ``service`` by climbing its parent chain.

    Returns an all-``None`` :class:`_UsbInfo` when the service has no
    USB ancestor — the normal case for on-board UARTs, Bluetooth serial,
    and PCI serial ports, all of which we still enumerate but without
    VID/PID enrichment.
    """
    parent = client.find_usb_parent(service)
    if parent is None:
        return _UsbInfo()
    try:
        location_int = client.get_int(parent, USB_LOCATION_ID_KEY)
        return _UsbInfo(
            vid=client.get_int(parent, USB_VENDOR_ID_KEY),
            pid=client.get_int(parent, USB_PRODUCT_ID_KEY),
            serial_number=client.get_string(parent, USB_SERIAL_NUMBER_KEY),
            manufacturer=client.get_string(parent, USB_VENDOR_NAME_KEY),
            product=client.get_string(parent, USB_PRODUCT_NAME_KEY),
            # IORegistry exposes locationID as a 32-bit int; render it as
            # the 8-hex-digit string pySerial uses on Darwin so migration
            # keeps the same values in user-visible logs.
            location=_format_location(location_int),
        )
    finally:
        client.release(parent)


def _device_base_name(device: str) -> str | None:
    """Return ``"cu.usbserial-A123"`` for ``"/dev/cu.usbserial-A123"``.

    Mirrors the ``name`` field Linux discovery exposes (``ttyUSB0``).
    Returns ``None`` for an empty trailing segment — defensive only,
    IOKit has not been observed to emit one in practice.
    """
    tail = device.rsplit("/", 1)[-1]
    return tail or None


def _format_location(location_id: int | None) -> str | None:
    """Render a ``locationID`` CFNumber as an 8-hex-digit string.

    Mirrors pySerial's Darwin output. Returns ``None`` for a missing
    value so downstream code can distinguish "no metadata" from "zero".
    """
    if location_id is None:
        return None
    # locationID is 32 bits; render unsigned.
    return f"{location_id & 0xFFFF_FFFF:08x}"


def _format_hwid(info: _UsbInfo) -> str | None:
    """Build the pyserial-compatible ``USB VID:PID=…`` string, or ``None``.

    Returns ``None`` when no USB ancestor was found — non-USB ports
    have no canonical hwid string and forcing one would mask the
    distinction from actual USB-attached adapters.
    """
    if info.vid is None or info.pid is None:
        return None
    parts = [f"USB VID:PID={info.vid:04X}:{info.pid:04X}"]
    if info.serial_number:
        parts.append(f"SER={info.serial_number}")
    if info.location:
        parts.append(f"LOCATION={info.location}")
    return " ".join(parts)


__all__ = [
    "enumerate_ports",
    "resolve_port_info",
]
