"""Unit tests for :func:`anyserial._darwin.discovery.enumerate_ports`.

Hermetic by construction: the tests inject an in-memory
:class:`FakeIOKitClient` that satisfies the same Protocol as the real
ctypes-backed client, so the walk logic — service iteration, property
reads, USB-ancestor resolution, callout-vs-dial-in preference, hwid
string formatting — runs deterministically on Linux CI without ever
loading the IOKit framework.

The fake intentionally mimics the *invariants* of real IOKit (each
``ServiceHandle`` is yielded once, callers must :meth:`release` what
they receive, the parent walk terminates) so a test that passes here
is strong evidence the real walk works too.
"""

from __future__ import annotations

from dataclasses import dataclass, field
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
    ServiceHandle,
)
from anyserial._darwin.discovery import enumerate_ports, resolve_port_info

if TYPE_CHECKING:
    from collections.abc import Iterator


@dataclass
class _FakeEntry:
    """One node in the fake IOKit registry — either a serial service or a parent."""

    ref: int
    properties: dict[str, str | int] = field(default_factory=lambda: {})
    parent: int | None = None


class FakeIOKitClient:
    """In-memory :class:`IOKitClient` for deterministic tests.

    Builds a tiny synthetic registry out of :class:`_FakeEntry` nodes.
    Tracks released handles so tests can assert the walk balances its
    release calls — a real leak on a Darwin host would accumulate
    kernel objects silently.
    """

    def __init__(self, entries: dict[int, _FakeEntry], serial_refs: list[int]) -> None:
        self._entries = entries
        self._serial_refs = serial_refs
        self.released: list[int] = []

    def list_serial_services(self) -> Iterator[ServiceHandle]:
        for ref in self._serial_refs:
            yield ServiceHandle(ref=ref)

    def get_string(self, service: ServiceHandle, key: str) -> str | None:
        entry = self._entries.get(service.ref)
        if entry is None:
            return None
        value = entry.properties.get(key)
        return value if isinstance(value, str) else None

    def get_int(self, service: ServiceHandle, key: str) -> int | None:
        entry = self._entries.get(service.ref)
        if entry is None:
            return None
        value = entry.properties.get(key)
        return value if isinstance(value, int) else None

    def find_usb_parent(self, service: ServiceHandle) -> ServiceHandle | None:
        # Walk parent refs until we find one with idVendor set — same
        # semantics as the real IORegistryEntryGetParentEntry loop.
        cursor = self._entries.get(service.ref)
        if cursor is None:
            return None
        while cursor.parent is not None:
            parent_entry = self._entries.get(cursor.parent)
            if parent_entry is None:
                return None
            if USB_VENDOR_ID_KEY in parent_entry.properties:
                return ServiceHandle(ref=parent_entry.ref)
            cursor = parent_entry
        return None

    def release(self, service: ServiceHandle) -> None:
        self.released.append(service.ref)


def _usb_serial_entry(
    *,
    ref: int,
    callout: str,
    dialin: str | None = None,
    usb_parent_ref: int | None = None,
) -> _FakeEntry:
    """Build a serial-service entry with the two standard device-node keys."""
    properties: dict[str, str | int] = {IO_CALLOUT_DEVICE_KEY: callout}
    if dialin is not None:
        properties[IO_DIALIN_DEVICE_KEY] = dialin
    return _FakeEntry(ref=ref, properties=properties, parent=usb_parent_ref)


def _ftdi_usb_parent_entry(ref: int) -> _FakeEntry:
    """Representative FTDI-style USB device parent entry."""
    return _FakeEntry(
        ref=ref,
        properties={
            USB_VENDOR_ID_KEY: 0x0403,
            USB_PRODUCT_ID_KEY: 0x6001,
            USB_SERIAL_NUMBER_KEY: "A12345BC",
            USB_VENDOR_NAME_KEY: "FTDI",
            USB_PRODUCT_NAME_KEY: "FT232R USB UART",
            USB_LOCATION_ID_KEY: 0x14100000,
        },
    )


class TestEnumeratePortsEmpty:
    def test_no_services_yields_empty_list(self) -> None:
        client = FakeIOKitClient(entries={}, serial_refs=[])
        assert enumerate_ports(client=client) == []


class TestEnumeratePortsNonUsb:
    def test_entry_without_usb_parent_has_null_metadata(self) -> None:
        # On-board UART has no USB ancestor — we still enumerate it,
        # just with every VID / PID / serial field as None.
        entries = {
            10: _usb_serial_entry(ref=10, callout="/dev/cu.Bluetooth-Incoming-Port"),
        }
        client = FakeIOKitClient(entries=entries, serial_refs=[10])

        ports = enumerate_ports(client=client)
        assert len(ports) == 1
        port = ports[0]
        assert port.device == "/dev/cu.Bluetooth-Incoming-Port"
        assert port.name == "cu.Bluetooth-Incoming-Port"
        assert port.vid is None
        assert port.pid is None
        assert port.serial_number is None
        assert port.manufacturer is None
        assert port.product is None
        assert port.hwid is None

    def test_releases_each_yielded_handle(self) -> None:
        # The walk must release every handle it receives from the
        # iterator; the fake records each release so we can check the
        # balance. A real leak would show up as unbounded kernel object
        # growth on Darwin.
        entries = {
            10: _usb_serial_entry(ref=10, callout="/dev/cu.Foo"),
        }
        client = FakeIOKitClient(entries=entries, serial_refs=[10])

        enumerate_ports(client=client)
        assert client.released == [10]


class TestEnumeratePortsUsb:
    def test_ftdi_adapter_metadata_round_trips(self) -> None:
        entries = {
            20: _usb_serial_entry(
                ref=20,
                callout="/dev/cu.usbserial-A12345BC",
                dialin="/dev/tty.usbserial-A12345BC",
                usb_parent_ref=21,
            ),
            21: _ftdi_usb_parent_entry(ref=21),
        }
        client = FakeIOKitClient(entries=entries, serial_refs=[20])

        ports = enumerate_ports(client=client)
        assert len(ports) == 1
        port = ports[0]
        assert port.device == "/dev/cu.usbserial-A12345BC"
        assert port.name == "cu.usbserial-A12345BC"
        assert port.vid == 0x0403
        assert port.pid == 0x6001
        assert port.serial_number == "A12345BC"
        assert port.manufacturer == "FTDI"
        assert port.product == "FT232R USB UART"
        assert port.description == "FT232R USB UART"
        assert port.location == "14100000"
        assert port.hwid == "USB VID:PID=0403:6001 SER=A12345BC LOCATION=14100000"

    def test_releases_service_and_parent_handles(self) -> None:
        entries = {
            20: _usb_serial_entry(
                ref=20,
                callout="/dev/cu.foo",
                usb_parent_ref=21,
            ),
            21: _ftdi_usb_parent_entry(ref=21),
        }
        client = FakeIOKitClient(entries=entries, serial_refs=[20])

        enumerate_ports(client=client)
        # Both the service and its USB-parent handle must be released.
        assert sorted(client.released) == [20, 21]


class TestEnumeratePortsCalloutPreference:
    def test_callout_path_preferred_over_dialin(self) -> None:
        # Both keys present: prefer the callout (cu.*) entry.
        entries = {
            30: _usb_serial_entry(
                ref=30,
                callout="/dev/cu.usbmodem1234",
                dialin="/dev/tty.usbmodem1234",
            ),
        }
        client = FakeIOKitClient(entries=entries, serial_refs=[30])

        port = enumerate_ports(client=client)[0]
        assert port.device == "/dev/cu.usbmodem1234"

    def test_dialin_used_when_callout_missing(self) -> None:
        # Hypothetical edge case — Apple's driver registers only the
        # dial-in path. Still enumerate the port so users of exotic
        # drivers see it.
        entry = _FakeEntry(
            ref=31,
            properties={IO_DIALIN_DEVICE_KEY: "/dev/tty.weird"},
        )
        client = FakeIOKitClient(entries={31: entry}, serial_refs=[31])

        port = enumerate_ports(client=client)[0]
        assert port.device == "/dev/tty.weird"

    def test_entry_without_any_device_path_is_skipped(self) -> None:
        # Defensive: if neither key is present, skip rather than crash.
        entry = _FakeEntry(ref=32, properties={})
        client = FakeIOKitClient(entries={32: entry}, serial_refs=[32])

        assert enumerate_ports(client=client) == []


class TestEnumeratePortsOrdering:
    def test_results_sorted_by_device_path(self) -> None:
        entries = {
            40: _usb_serial_entry(ref=40, callout="/dev/cu.bbb"),
            41: _usb_serial_entry(ref=41, callout="/dev/cu.aaa"),
            42: _usb_serial_entry(ref=42, callout="/dev/cu.ccc"),
        }
        client = FakeIOKitClient(entries=entries, serial_refs=[40, 41, 42])

        ports = enumerate_ports(client=client)
        assert [p.device for p in ports] == [
            "/dev/cu.aaa",
            "/dev/cu.bbb",
            "/dev/cu.ccc",
        ]


class TestResolvePortInfo:
    def test_matches_callout_path(self) -> None:
        entries = {
            50: _usb_serial_entry(
                ref=50,
                callout="/dev/cu.usbserial-A12345BC",
                dialin="/dev/tty.usbserial-A12345BC",
                usb_parent_ref=51,
            ),
            51: _ftdi_usb_parent_entry(ref=51),
        }
        client = FakeIOKitClient(entries=entries, serial_refs=[50])

        info = resolve_port_info("/dev/cu.usbserial-A12345BC", client=client)
        assert info is not None
        assert info.vid == 0x0403

    def test_matches_dialin_path(self) -> None:
        # Some tools open the tty.* alias; resolve_port_info should
        # still return the same PortInfo.
        entries = {
            60: _usb_serial_entry(
                ref=60,
                callout="/dev/cu.usbserial-A12345BC",
                dialin="/dev/tty.usbserial-A12345BC",
                usb_parent_ref=61,
            ),
            61: _ftdi_usb_parent_entry(ref=61),
        }
        client = FakeIOKitClient(entries=entries, serial_refs=[60])

        info = resolve_port_info("/dev/tty.usbserial-A12345BC", client=client)
        assert info is not None
        assert info.pid == 0x6001

    def test_unknown_path_returns_none(self) -> None:
        entries = {70: _usb_serial_entry(ref=70, callout="/dev/cu.known")}
        client = FakeIOKitClient(entries=entries, serial_refs=[70])

        assert resolve_port_info("/dev/cu.does-not-exist", client=client) is None

    def test_releases_every_iterated_service(self) -> None:
        # Even when resolve_port_info short-circuits on the first
        # match, every iterated handle must be released.
        entries = {
            80: _usb_serial_entry(ref=80, callout="/dev/cu.first"),
            81: _usb_serial_entry(ref=81, callout="/dev/cu.second"),
        }
        client = FakeIOKitClient(entries=entries, serial_refs=[80, 81])

        resolve_port_info("/dev/cu.second", client=client)
        # Both visited services released, even though the walk stopped
        # at the second one after matching.
        assert sorted(client.released) == [80, 81]
