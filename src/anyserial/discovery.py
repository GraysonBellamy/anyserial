"""Async port-discovery API.

:class:`PortInfo` describes a single discovered serial port;
:func:`list_serial_ports` enumerates every port the host platform exposes;
:func:`find_serial_port` returns the first match against caller-supplied
filters. Discovery is always live — no caching, per :doc:`DESIGN` §23.

The public functions are async because enumeration performs filesystem and
platform-metadata I/O (sysfs walks on Linux, IOKit calls on macOS, USB-bus
enumeration). Wrapping the per-platform sync enumerator in
:func:`anyio.to_thread.run_sync` keeps the AnyIO-first promise honest and
lets callers run discovery inside cancellation scopes.

Platform implementations are lazy-imported through :func:`_select_discovery`,
mirroring :mod:`anyserial._backend.selector`. Platforms without a shipped
enumerator raise :class:`UnsupportedPlatformError`.
"""

from __future__ import annotations

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
    filters are AND-ed together. Equality is exact: integer ``vid`` /
    ``pid`` must match the integer parsed from sysfs / IOKit; string fields
    are compared verbatim.

    Args:
        vid: USB vendor ID to match (e.g. ``0x0403`` for FTDI).
        pid: USB product ID to match.
        serial_number: USB device serial-number string to match.
        device: Filesystem device path to match (e.g. ``"/dev/ttyUSB0"``).
        backend: Discovery backend selector; see :func:`list_serial_ports`.

    Returns:
        The first :class:`PortInfo` from :func:`list_serial_ports` that
        satisfies every filter, or ``None`` if no port matched.

    Raises:
        UnsupportedPlatformError: The selected backend is not implemented
            for the current platform.
        ImportError: An optional-extra backend was selected but the
            third-party package is not installed.
    """
    ports = await list_serial_ports(backend=backend)
    return next(
        (
            p
            for p in ports
            if (vid is None or p.vid == vid)
            and (pid is None or p.pid == pid)
            and (serial_number is None or p.serial_number == serial_number)
            and (device is None or p.device == device)
        ),
        None,
    )


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
    "find_serial_port",
    "list_serial_ports",
]
