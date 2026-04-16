"""Narrow ctypes facade over IOKit + CoreFoundation for Darwin discovery.

This module is the *only* place in :mod:`anyserial` that talks to the
Apple frameworks. :mod:`anyserial._darwin.discovery` consumes the small
Protocol-shaped API exported here (:class:`IOKitClient`) and builds
:class:`~anyserial.discovery.PortInfo` records; tests substitute an
in-memory fake that satisfies the same Protocol so they never need a
real Darwin kernel or a bundled IOKit framework.

The ctypes bindings are loaded lazily via :func:`default_client`; simply
importing this module on a non-Darwin host does no work beyond the
Python-level class/Protocol definitions. That keeps the Linux CI import
graph clean — the real framework lookup happens only when the
enumerator actually runs on a Darwin host.

Reference: ``<IOKit/IOKitLib.h>``, ``<IOKit/serial/IOSerialKeys.h>``,
``<CoreFoundation/CFBase.h>``. The set of wrapped functions is
intentionally small — every addition is a new potential portability risk
(§36 risk register: "ctypes IOKit bindings fragile on macOS upgrades").
"""

from __future__ import annotations

import ctypes
import ctypes.util
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Protocol

if TYPE_CHECKING:
    from collections.abc import Iterator


# ---------------------------------------------------------------------------
# IOKit / CoreFoundation constants
# ---------------------------------------------------------------------------

# <IOKit/serial/IOSerialKeys.h> — the class every BSD-visible serial driver
# registers under. Kernel console, internal UARTs, and USB-serial adapters
# all show up in the iterator this matches.
_IOSERIAL_BSD_CLIENT: Final[bytes] = b"IOSerialBSDClient"

# Property keys — exact strings from <IOKit/serial/IOSerialKeys.h> and the
# USB device property convention used since OS X 10.0. Spaces are real:
# Apple's property names were set in stone long before modern key styles.
IO_CALLOUT_DEVICE_KEY: Final[str] = "IOCalloutDevice"
IO_DIALIN_DEVICE_KEY: Final[str] = "IODialinDevice"
IO_TTY_DEVICE_KEY: Final[str] = "IOTTYDevice"
USB_VENDOR_ID_KEY: Final[str] = "idVendor"
USB_PRODUCT_ID_KEY: Final[str] = "idProduct"
USB_SERIAL_NUMBER_KEY: Final[str] = "USB Serial Number"
USB_VENDOR_NAME_KEY: Final[str] = "USB Vendor Name"
USB_PRODUCT_NAME_KEY: Final[str] = "USB Product Name"
USB_LOCATION_ID_KEY: Final[str] = "locationID"

# <IOKit/IOKitLib.h>: the parent-plane traversal plane — "IOService" selects
# the service tree, which is what we want to climb looking for USB ancestors.
_IOSERVICE_PLANE: Final[bytes] = b"IOService"

# <CoreFoundation/CFBase.h>: kCFAllocatorDefault is NULL (the framework
# picks a sensible allocator). kCFStringEncodingUTF8 = 0x08000100.
_CF_ALLOCATOR_DEFAULT: Final[int] = 0
_CF_STRING_ENCODING_UTF8: Final[int] = 0x0800_0100

# kIOMainPortDefault replaced kIOMasterPortDefault in macOS 12 but the
# implementation behind both is the same and the value 0 (meaning "default
# port") is honoured across every macOS release we target. Passing 0
# literally avoids a name-version probe.
_IO_MAIN_PORT_DEFAULT: Final[int] = 0


# ---------------------------------------------------------------------------
# Public Protocol / handle types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ServiceHandle:
    """Opaque handle for a single IOKit registry entry.

    Real clients wrap a Darwin ``io_service_t`` (an ``mach_port_t``
    integer); test clients can use any sentinel integer. The handle is
    intentionally frozen so callers can use it as a dict key if they need
    to pre-compute derived metadata.
    """

    ref: int


class IOKitClient(Protocol):
    """Protocol the discovery layer depends on.

    The real implementation (:class:`_CtypesIOKitClient`) wraps IOKit +
    CoreFoundation; the test suite passes an in-memory fake. The contract
    is intentionally minimal — every method a production walk needs is
    here, nothing more.
    """

    def list_serial_services(self) -> Iterator[ServiceHandle]:
        """Yield one :class:`ServiceHandle` per ``IOSerialBSDClient``.

        Real clients must ``IOObjectRelease`` each handle once the
        consumer finishes with it — :meth:`release` carries that
        contract. Callers who iterate fully are expected to call
        :meth:`release` on each yielded handle.
        """
        ...

    def get_string(self, service: ServiceHandle, key: str) -> str | None:
        """Return the string-valued property ``key``, or ``None``.

        Returns ``None`` for absent properties, non-string values, or
        values that fail UTF-8 decoding. Callers treat ``None`` as
        "metadata unavailable" rather than as an error.
        """
        ...

    def get_int(self, service: ServiceHandle, key: str) -> int | None:
        """Return the integer-valued property ``key``, or ``None``.

        Handles 16-bit USB VID/PID numbers and 32-bit ``locationID``.
        """
        ...

    def find_usb_parent(self, service: ServiceHandle) -> ServiceHandle | None:
        """Walk the ``IOService`` plane to the nearest USB-device ancestor.

        Returns ``None`` when no ancestor exposes ``idVendor`` — the
        normal case for on-board UARTs, Bluetooth serial, and PCI serial.
        The returned handle must also be released by the caller.
        """
        ...

    def release(self, service: ServiceHandle) -> None:
        """Release one :class:`ServiceHandle`.

        Idempotent against already-released handles (real IOKit returns
        an error code, which the wrapper swallows — a freed handle is
        not a user-facing problem at this layer).
        """
        ...


# ---------------------------------------------------------------------------
# Default ctypes-backed implementation
# ---------------------------------------------------------------------------


def default_client() -> IOKitClient:
    """Return a real IOKit client. Raises :class:`OSError` if unavailable.

    Performs the ctypes library lookup and binding setup on first call;
    subsequent calls reuse the cached module-level client. On a non-
    Darwin host, or a Darwin host where the frameworks are missing
    (unlikely outside a broken toolchain install), :class:`OSError` is
    raised so the caller can surface a meaningful error to the user.
    """
    global _cached_client  # noqa: PLW0603 — module-level cache is the whole point
    if _cached_client is None:
        _cached_client = _CtypesIOKitClient()
    return _cached_client


_cached_client: IOKitClient | None = None


class _CtypesIOKitClient:
    """ctypes-backed :class:`IOKitClient` for real Darwin hosts.

    All frameworks are loaded in :meth:`__init__` — if the load fails,
    :func:`default_client` surfaces the :class:`OSError` before any
    enumeration can begin, so callers get a clean exception path rather
    than a random ``AttributeError`` deep inside a walk.

    Every ``# type: ignore`` in this module is localized and linked back
    to a specific ctypes idiom (per §36 risk register: "Localized
    ``# type: ignore`` with linked issues"). Nothing escapes these
    methods as an ``Any`` — the signatures line up with the Protocol.
    """

    def __init__(self) -> None:
        self._iokit = _load_framework("IOKit")
        self._cf = _load_framework("CoreFoundation")
        _configure_signatures(self._iokit, self._cf)

    # ------------------------------------------------------------------
    # IOKitClient surface
    # ------------------------------------------------------------------

    def list_serial_services(self) -> Iterator[ServiceHandle]:
        """Yield one :class:`ServiceHandle` per ``IOSerialBSDClient`` entry.

        Delegates to an inner generator so pyright can see both the
        "returns an Iterator" declaration and the generator's actual
        yield path clearly — a direct ``def`` + early ``return`` mix
        confuses its return-type inference.
        """
        return self._iter_serial_services()

    def _iter_serial_services(self) -> Iterator[ServiceHandle]:
        matching = self._iokit.IOServiceMatching(_IOSERIAL_BSD_CLIENT)
        if not matching:
            # A NULL dict means IOKit could not build the matcher — at
            # this point we have nothing to iterate over.
            return
        iterator = ctypes.c_uint32(0)
        # IOServiceGetMatchingServices consumes one reference on the
        # matching dict; there is no corresponding CFRelease here.
        kr = self._iokit.IOServiceGetMatchingServices(
            _IO_MAIN_PORT_DEFAULT,
            matching,
            ctypes.byref(iterator),
        )
        if kr != 0:
            return
        try:
            while True:
                raw = self._iokit.IOIteratorNext(iterator)
                if not raw:
                    break
                yield ServiceHandle(ref=raw)
        finally:
            self._iokit.IOObjectRelease(iterator)

    def get_string(self, service: ServiceHandle, key: str) -> str | None:
        cf_value = self._read_property(service, key)
        if not cf_value:
            return None
        try:
            return _cfstring_to_str(self._cf, cf_value)
        finally:
            self._cf.CFRelease(cf_value)

    def get_int(self, service: ServiceHandle, key: str) -> int | None:
        cf_value = self._read_property(service, key)
        if not cf_value:
            return None
        try:
            return _cfnumber_to_int(self._cf, cf_value)
        finally:
            self._cf.CFRelease(cf_value)

    def find_usb_parent(self, service: ServiceHandle) -> ServiceHandle | None:
        # Walk the IOService parent chain until we find a node with an
        # idVendor property — that's the USB device dir. A nil-parent
        # kr != 0 means we hit the top of the tree without a hit.
        cursor = service.ref
        hops = 0
        # Guard against pathological loops — the real registry is at most
        # a few dozen deep, but a corrupt state should never hang a walk.
        max_hops = 32
        while hops < max_hops:
            parent = ctypes.c_uint32(0)
            kr = self._iokit.IORegistryEntryGetParentEntry(
                cursor,
                _IOSERVICE_PLANE,
                ctypes.byref(parent),
            )
            if kr != 0 or not parent.value:
                return None
            handle = ServiceHandle(ref=parent.value)
            if self._read_property(handle, USB_VENDOR_ID_KEY):
                return handle
            # Not a USB device: release and climb one more level.
            self.release(handle)
            cursor = parent.value
            hops += 1
        return None

    def release(self, service: ServiceHandle) -> None:
        # Intentionally ignore the kernel return value: a second release
        # on a freed handle is harmless at the Python layer and there is
        # nothing useful a caller could do with the error.
        self._iokit.IOObjectRelease(service.ref)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _read_property(self, service: ServiceHandle, key: str) -> int:
        """Return the raw CFTypeRef (as an int) for ``key``, or 0 if absent.

        The returned reference carries a +1 retain that the caller is
        responsible for releasing via :meth:`CFRelease` — see
        :meth:`get_string` / :meth:`get_int` / :meth:`find_usb_parent`
        for the release discipline.
        """
        cf_key = self._cf.CFStringCreateWithCString(
            _CF_ALLOCATOR_DEFAULT,
            key.encode("utf-8"),
            _CF_STRING_ENCODING_UTF8,
        )
        if not cf_key:
            return 0
        try:
            value = self._iokit.IORegistryEntryCreateCFProperty(
                service.ref,
                cf_key,
                _CF_ALLOCATOR_DEFAULT,
                0,
            )
        finally:
            self._cf.CFRelease(cf_key)
        return int(value or 0)


# ---------------------------------------------------------------------------
# ctypes plumbing
# ---------------------------------------------------------------------------


def _load_framework(name: str) -> Any:  # ctypes.CDLL, but signature is dynamic
    """Load ``{name}.framework`` and return the :class:`ctypes.CDLL` handle.

    Uses :func:`ctypes.util.find_library` so the loader picks up the
    framework from whichever SDK the host has active (system, command-
    line tools, full Xcode). Raises :class:`OSError` on failure.
    """
    path = ctypes.util.find_library(name)
    if path is None:
        msg = f"{name}.framework not found — is this a Darwin host?"
        raise OSError(msg)
    return ctypes.CDLL(path, use_errno=True)


def _configure_signatures(iokit: Any, cf: Any) -> None:
    """Attach argtypes / restype to the IOKit + CF entry points we call.

    ctypes defaults to ``int`` for restype, which silently truncates
    64-bit pointers on arm64. Every ctypes call that returns a pointer
    or a 64-bit int needs an explicit restype — that's what this does.
    """
    # IOKit
    iokit.IOServiceMatching.argtypes = [ctypes.c_char_p]
    iokit.IOServiceMatching.restype = ctypes.c_void_p

    iokit.IOServiceGetMatchingServices.argtypes = [
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint32),
    ]
    iokit.IOServiceGetMatchingServices.restype = ctypes.c_int

    iokit.IOIteratorNext.argtypes = [ctypes.c_uint32]
    iokit.IOIteratorNext.restype = ctypes.c_uint32

    iokit.IOObjectRelease.argtypes = [ctypes.c_uint32]
    iokit.IOObjectRelease.restype = ctypes.c_int

    iokit.IORegistryEntryCreateCFProperty.argtypes = [
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_uint32,
    ]
    iokit.IORegistryEntryCreateCFProperty.restype = ctypes.c_void_p

    iokit.IORegistryEntryGetParentEntry.argtypes = [
        ctypes.c_uint32,
        ctypes.c_char_p,
        ctypes.POINTER(ctypes.c_uint32),
    ]
    iokit.IORegistryEntryGetParentEntry.restype = ctypes.c_int

    # CoreFoundation
    cf.CFStringCreateWithCString.argtypes = [
        ctypes.c_void_p,
        ctypes.c_char_p,
        ctypes.c_uint32,
    ]
    cf.CFStringCreateWithCString.restype = ctypes.c_void_p

    cf.CFStringGetLength.argtypes = [ctypes.c_void_p]
    cf.CFStringGetLength.restype = ctypes.c_long

    cf.CFStringGetCString.argtypes = [
        ctypes.c_void_p,
        ctypes.c_char_p,
        ctypes.c_long,
        ctypes.c_uint32,
    ]
    cf.CFStringGetCString.restype = ctypes.c_ubyte

    cf.CFNumberGetValue.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
    ]
    cf.CFNumberGetValue.restype = ctypes.c_ubyte

    cf.CFRelease.argtypes = [ctypes.c_void_p]
    cf.CFRelease.restype = None


def _cfstring_to_str(cf: Any, cf_string: int) -> str | None:
    """Decode a ``CFStringRef`` into a Python :class:`str`.

    ``CFStringGetLength`` returns units of UTF-16 code points; we
    allocate a generous UTF-8 buffer (4 bytes/char + terminator) and
    fall back to ``None`` if the kernel-level conversion fails.
    """
    length = cf.CFStringGetLength(cf_string)
    if length < 0:
        return None
    # Four bytes per UTF-16 code unit is the CFString worst case for
    # UTF-8 output; +1 for the nul terminator.
    buf_size = int(length) * 4 + 1
    buf = ctypes.create_string_buffer(buf_size)
    ok = cf.CFStringGetCString(cf_string, buf, buf_size, _CF_STRING_ENCODING_UTF8)
    if not ok:
        return None
    return buf.value.decode("utf-8", errors="replace") or None


def _cfnumber_to_int(cf: Any, cf_number: int) -> int | None:
    """Decode a ``CFNumberRef`` into a Python :class:`int`.

    kCFNumberSInt64Type (4) reliably fits every serial-related number
    we care about (USB VID/PID are 16-bit, ``locationID`` is 32-bit).
    Returns ``None`` if CFNumberGetValue rejects the type code — rare
    enough that falling back to "no metadata" is a reasonable default.
    """
    value = ctypes.c_int64(0)
    # kCFNumberSInt64Type = 4 per <CoreFoundation/CFNumber.h>.
    ok = cf.CFNumberGetValue(cf_number, 4, ctypes.byref(value))
    if not ok:
        return None
    return int(value.value)


__all__ = [
    "IO_CALLOUT_DEVICE_KEY",
    "IO_DIALIN_DEVICE_KEY",
    "IO_TTY_DEVICE_KEY",
    "USB_LOCATION_ID_KEY",
    "USB_PRODUCT_ID_KEY",
    "USB_PRODUCT_NAME_KEY",
    "USB_SERIAL_NUMBER_KEY",
    "USB_VENDOR_ID_KEY",
    "USB_VENDOR_NAME_KEY",
    "IOKitClient",
    "ServiceHandle",
    "default_client",
]
