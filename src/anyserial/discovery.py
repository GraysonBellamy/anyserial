"""Async port-discovery API.

:class:`PortInfo` describes a single discovered serial port;
:func:`list_serial_ports` enumerates every port the host platform exposes;
:func:`find_serial_port` returns the first match against caller-supplied
filters; :func:`canonical_port_name` maps every name of one port to a
single comparison key. Discovery is always live — no caching, per
:doc:`DESIGN` §23.

The enumeration functions are async because enumeration performs filesystem
and platform-metadata I/O (sysfs walks on Linux, IOKit calls on macOS,
USB-bus enumeration). Wrapping the per-platform sync enumerator in
:func:`anyio.to_thread.run_sync` keeps the AnyIO-first promise honest and
lets callers run discovery inside cancellation scopes.

Platform implementations are lazy-imported through :func:`_select_discovery`,
mirroring :mod:`anyserial._backend.selector`. Platforms without a shipped
enumerator raise :class:`UnsupportedPlatformError`.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

import anyio.to_thread

from anyserial.exceptions import UnsupportedPlatformError

if TYPE_CHECKING:
    from collections.abc import Callable

type DiscoveryBackend = Literal["native", "pyudev", "pyserial"]
"""Selector tag for :func:`list_serial_ports` / :func:`find_serial_port`.

- ``"native"`` (default): use the platform-specific enumerator
  (:mod:`anyserial._linux.discovery` on Linux, :mod:`anyserial._darwin.discovery`
  on macOS, :mod:`anyserial._bsd.discovery` on the BSDs,
  :mod:`anyserial._windows.discovery` on Windows).
- ``"pyudev"``: Linux-only fallback via the ``pyudev`` extra. Richer USB
  metadata where ``udev`` rules apply.
- ``"pyserial"``: cross-platform fallback via the ``pyserial`` extra.
  Useful on platforms whose native enumerator hasn't landed yet.
"""

# Win32 device-namespace prefixes. Both are four characters long.
_WINDOWS_DEVICE_PREFIXES = ("\\\\.\\", "\\\\?\\")


@dataclass(frozen=True, slots=True, kw_only=True)
class PortInfo:
    """Metadata for a discovered serial port.

    Every field except :attr:`device` is optional because the available
    metadata varies by platform, transport, and driver. USB-attached adapters
    typically populate ``vid`` / ``pid`` / ``serial_number`` / ``manufacturer``
    / ``product``; on-board UARTs and virtual ports usually leave them
    ``None``.

    Equality is by-value (frozen dataclass), so :class:`PortInfo` is safe to
    place in sets and use as a dict key. The slot layout keeps the per-port
    overhead small enough for sub-second enumeration of a USB hub full of
    adapters.
    """

    device: str
    name: str | None = None
    description: str | None = None
    hwid: str | None = None
    vid: int | None = None
    pid: int | None = None
    serial_number: str | None = None
    manufacturer: str | None = None
    product: str | None = None
    location: str | None = None
    interface: str | None = None


async def list_serial_ports(*, backend: DiscoveryBackend = "native") -> list[PortInfo]:
    """Enumerate every serial port the host platform exposes.

    Args:
        backend: Which enumerator to use. Defaults to ``"native"`` (the
            platform-specific implementation). Pass ``"pyudev"`` for the
            Linux-only udev backend or ``"pyserial"`` for the cross-platform
            ``pyserial.tools.list_ports`` backend; both require the
            corresponding optional extra.

    Returns:
        A fresh list of :class:`PortInfo`. Empty when the platform exposes
        no ports. Order is platform-defined and not guaranteed stable across
        calls; callers that need a stable ordering should sort on
        :attr:`PortInfo.device`.

    Raises:
        UnsupportedPlatformError: The selected backend is not implemented
            for the current platform.
        ImportError: An optional-extra backend was selected but the
            third-party package is not installed; the message includes
            the install command.
    """
    enumerate_fn = _select_discovery(backend)
    return await anyio.to_thread.run_sync(enumerate_fn)


async def find_serial_port(
    *,
    vid: int | None = None,
    pid: int | None = None,
    serial_number: str | None = None,
    device: str | None = None,
    backend: DiscoveryBackend = "native",
) -> PortInfo | None:
    """Return the first port matching every supplied filter, or ``None``.

    All keyword arguments default to ``None`` (no constraint). Multiple
    filters are AND-ed together. Integer ``vid`` / ``pid`` must equal the
    integer parsed from the platform metadata and ``serial_number`` is
    compared verbatim. ``device`` matches any name of the same port: both
    sides go through :func:`canonical_port_name`, so ``"com8"`` finds
    ``COM8`` on Windows and a ``/dev/serial/by-id/...`` symlink finds the
    ``/dev/ttyUSB0`` it points at.

    Args:
        vid: USB vendor ID to match (e.g. ``0x0403`` for FTDI).
        pid: USB product ID to match.
        serial_number: USB device serial-number string to match.
        device: Device path or port name to match (e.g. ``"/dev/ttyUSB0"``
            or ``"COM8"``).
        backend: Discovery backend selector; see :func:`list_serial_ports`.

    Returns:
        The first :class:`PortInfo`, in :func:`list_serial_ports` order,
        that satisfies every filter, or ``None`` if no port matched.

    Raises:
        UnsupportedPlatformError: The selected backend is not implemented
            for the current platform.
        ImportError: An optional-extra backend was selected but the
            third-party package is not installed.
    """
    enumerate_fn = _select_discovery(backend)

    def first_match() -> PortInfo | None:
        # Runs in the worker thread: canonicalizing a POSIX path reads the
        # filesystem, like the enumeration itself.
        wanted = None if device is None else canonical_port_name(device)
        return next(
            (
                p
                for p in enumerate_fn()
                if (vid is None or p.vid == vid)
                and (pid is None or p.pid == pid)
                and (serial_number is None or p.serial_number == serial_number)
                and (wanted is None or canonical_port_name(p.device) == wanted)
            ),
            None,
        )

    return await anyio.to_thread.run_sync(first_match)


def canonical_port_name(path: str, *, platform: str | None = None) -> str:
    r"""Return the one name shared by every spelling of serial port ``path``.

    A port answers to several names: ``COM8``, ``com8``, ``\\.\COM8`` and
    ``\\?\COM8`` on Windows; a ``/dev/serial/by-id/...`` symlink and the
    ``/dev/ttyUSB0`` it points at on POSIX. Two names refer to the same
    port when their canonical names are equal, so the result works as a
    key for sharing one connection per port, refusing a second open, or
    reporting the port.

    - **Windows:** leading ``\\.\`` and ``\\?\`` device prefixes are
      removed and the rest upper-cased, since Win32 device names are
      case-insensitive. For a ``COMn`` port the result equals the
      :attr:`PortInfo.device` that :func:`list_serial_ports` reports.
    - **Elsewhere:** the resolved path, with every symlink followed, when
      ``path`` exists; otherwise ``path`` unchanged (a port that is
      unplugged, or a name that is not a filesystem path).

    Compare canonical names with canonical names: a :attr:`PortInfo.device`
    that is not a ``COMn`` name (a Windows device-interface path, say) is
    not itself canonical. :attr:`SerialPort.path` returns the name the port
    was opened with, so ``canonical_port_name(port.path)`` is the key for an
    open port.

    The function never raises. On POSIX it reads the filesystem to resolve
    symlinks; it performs no other I/O.

    Args:
        path: The port name as given, e.g. ``"com8"`` or
            ``"/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_A12345-if00-port0"``.
        platform: The :data:`sys.platform` value whose naming rules apply.
            Defaults to the running platform, read at call time.

    Returns:
        The canonical name of the port.
    """
    if (sys.platform if platform is None else platform) == "win32":
        name = path
        # A loop, not a single strip, so the result is canonical itself.
        while name.startswith(_WINDOWS_DEVICE_PREFIXES):
            name = name[4:]
        return name.upper()
    # ``os.path`` rather than ``pathlib``: ``Path("")`` means ``"."``, which
    # exists, and ``os.path.exists`` reports every OSError as ``False``.
    if os.path.exists(path):  # noqa: PTH110
        return os.path.realpath(path)
    return path


def _select_discovery(backend: DiscoveryBackend = "native") -> Callable[[], list[PortInfo]]:
    """Return a sync enumeration callable for ``backend`` on the current platform.

    Lazy-imported per backend so non-Linux installs do not pay for the
    Linux sysfs walker, and installs without ``pyudev`` / ``pyserial``
    don't pay for those either. Tests substitute this function via
    :func:`monkeypatch.setattr` to inject deterministic port lists without
    depending on real hardware or third-party packages.

    Args:
        backend: Which enumerator to return; see :data:`DiscoveryBackend`.

    Returns:
        A zero-argument callable that returns a fresh ``list[PortInfo]``.
        The caller (:func:`list_serial_ports`) runs it in a worker thread.

    Raises:
        UnsupportedPlatformError: ``backend="native"`` and no native
            enumerator is implemented for this platform yet, or
            ``backend="pyudev"`` was requested off Linux. Message names
            the platform / backend so callers can grep for it in logs.
    """
    if backend == "pyserial":
        # pyserial is cross-platform — import succeeds (or fails with a
        # clear ImportError) regardless of host OS.
        from anyserial._discovery.pyserial import enumerate_ports  # noqa: PLC0415

        return enumerate_ports
    if backend == "pyudev":
        # pyudev wraps libudev — Linux only. The module raises
        # UnsupportedPlatformError on non-Linux when called.
        from anyserial._discovery.pyudev import enumerate_ports  # noqa: PLC0415

        return enumerate_ports

    # backend == "native"
    platform = sys.platform
    if platform.startswith("linux"):
        from anyserial._linux.discovery import enumerate_ports  # noqa: PLC0415 — lazy by platform

        return enumerate_ports
    if platform == "darwin":
        from anyserial._darwin.discovery import enumerate_ports  # noqa: PLC0415 — lazy by platform

        return enumerate_ports
    if "bsd" in platform or platform.startswith("dragonfly"):
        from anyserial._bsd.discovery import enumerate_ports  # noqa: PLC0415 — lazy by platform

        return enumerate_ports
    if platform == "win32":
        from anyserial._windows.discovery import enumerate_ports  # noqa: PLC0415 — lazy by platform

        return enumerate_ports
    else:
        msg = f"No discovery backend available for platform {platform!r}"
    raise UnsupportedPlatformError(msg)


__all__ = [
    "DiscoveryBackend",
    "PortInfo",
    "canonical_port_name",
    "find_serial_port",
    "list_serial_ports",
]
