"""Native ``/dev``-scan serial port discovery for the BSDs.

Scans ``/dev`` for the device-node naming conventions each BSD variant
uses for its callout (``cua*``) and dial-in (``tty*``) serial nodes,
then returns :class:`PortInfo` records with just the device path and
basename populated. USB VID/PID/serial metadata is *not* populated —
each BSD variant exposes USB metadata through a different mechanism
(``usbconfig``, ``drvctl``, ``sysctl``), and hardware testing is needed
to validate each one per §36 risk register. Users who want VID/PID on
BSD today can fall back to the ``pyserial`` extra via
``list_serial_ports(backend="pyserial")``.

Naming conventions (current as of the major releases supported):

- **FreeBSD / DragonFly**: ``/dev/cuau*`` (on-board serial),
  ``/dev/cuaU*`` (USB-serial), ``/dev/cuad*`` (legacy on-board), plus
  the ``/dev/ttyu*`` / ``/dev/ttyU*`` dial-in aliases.
- **OpenBSD**: ``/dev/cua*`` covers both on-board and USB-serial.
- **NetBSD**: ``/dev/dty*`` for callout, ``/dev/tty*`` for dial-in.

Pure sync — :func:`anyserial.discovery.list_serial_ports` runs the
enumeration in a worker thread via ``anyio.to_thread.run_sync``. No
AnyIO imports here; pure :mod:`pathlib`, so the module imports and
tests clean on any host (Linux CI included).
"""

from __future__ import annotations

import sys
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Final

from anyserial.discovery import PortInfo

_DEFAULT_DEV_ROOT: Final[Path] = Path("/dev")


# Patterns per BSD variant. Callout nodes are preferred (they don't
# block on carrier detect); dial-in aliases are also listed so a port
# without a callout node still surfaces. Order matters — the first
# match wins when the same underlying device has multiple nodes.
_PATTERNS_FREEBSD: Final[tuple[str, ...]] = (
    "cuaU*",  # USB-serial callout
    "cuau*",  # On-board serial callout (modern)
    "cuad*",  # On-board serial callout (legacy)
    "ttyU*",  # USB-serial dial-in
    "ttyu*",  # On-board serial dial-in (modern)
)
_PATTERNS_OPENBSD: Final[tuple[str, ...]] = (
    "cuaU*",  # USB-serial
    "cua0*",  # On-board UART 0
    "cua1*",  # On-board UART 1
)
_PATTERNS_NETBSD: Final[tuple[str, ...]] = (
    "dtyU*",  # USB-serial callout
    "dty0*",  # On-board callout
    "ttyU*",  # USB-serial dial-in
)
_PATTERNS_DRAGONFLY: Final[tuple[str, ...]] = (
    "cuaU*",
    "cuau*",
)


def _patterns_for_platform(platform: str) -> tuple[str, ...]:
    """Select the right ``/dev`` glob patterns for ``platform``.

    Returns an empty tuple for platform strings that don't match any
    BSD variant we know about (callers short-circuit to ``[]`` without
    walking ``/dev``).
    """
    if platform.startswith("freebsd"):
        return _PATTERNS_FREEBSD
    if platform.startswith("openbsd"):
        return _PATTERNS_OPENBSD
    if platform.startswith("netbsd"):
        return _PATTERNS_NETBSD
    if platform.startswith("dragonfly"):
        return _PATTERNS_DRAGONFLY
    return ()


def enumerate_ports(
    *,
    dev_root: Path = _DEFAULT_DEV_ROOT,
    platform: str | None = None,
) -> list[PortInfo]:
    """Enumerate serial ports from ``/dev`` node names.

    Args:
        dev_root: Root of the device-node tree. Production uses
            ``/dev``; tests substitute a ``tmp_path`` tree so the walk
            runs deterministically on any host. The scanner never
            opens nodes — just builds path strings.
        platform: Override ``sys.platform`` for test dispatch. Omit in
            production; tests pass ``"freebsd14"`` / ``"openbsd7"`` /
            etc. to exercise variant-specific pattern selection.

    Returns:
        A list of :class:`PortInfo`, de-duplicated by device path and
        sorted for stable ordering. Empty when ``dev_root`` is missing
        (sandboxed CI runners) or the patterns match nothing (platform
        unknown, no serial adapters plugged in).
    """
    patterns = _patterns_for_platform(platform if platform is not None else sys.platform)
    if not patterns or not dev_root.is_dir():
        return []

    seen: set[str] = set()
    ports: list[PortInfo] = []
    for pattern in patterns:
        for node in dev_root.glob(pattern):
            device = str(node)
            if device in seen:
                continue
            seen.add(device)
            ports.append(
                PortInfo(
                    device=device,
                    name=node.name,
                    # Every other field is None by default — USB
                    # metadata enrichment is the pyserial extra's job
                    # on BSD until hardware-verified per §36.
                ),
            )
    ports.sort(key=lambda p: p.device)
    return ports


def resolve_port_info(
    path: str,
    *,
    dev_root: Path = _DEFAULT_DEV_ROOT,
    platform: str | None = None,
) -> PortInfo | None:
    """Resolve ``path`` to its :class:`PortInfo`, or ``None``.

    Single-entry variant of :func:`enumerate_ports` for the open-path
    typed-attribute hookup in :func:`anyserial.open_serial_port`.
    Returns ``None`` for paths that don't match any BSD naming pattern
    (pseudo terminals, unexpected paths), mirroring what Linux's
    sysfs-based :func:`resolve_port_info` returns for non-``/sys``-
    visible devices.
    """
    resolved = Path(path)
    patterns = _patterns_for_platform(platform if platform is not None else sys.platform)
    if not patterns:
        return None
    name = resolved.name
    for pattern in patterns:
        if resolved.match(str(dev_root / pattern)) or _fnmatch_basename(name, pattern):
            return PortInfo(device=str(resolved), name=name)
    return None


def _fnmatch_basename(name: str, pattern: str) -> bool:
    """Match ``name`` (a basename) against ``pattern`` (a glob).

    :meth:`Path.match` in Python 3.13 does not consistently treat a
    bare-basename pattern ("cuaU*") the way a caller might expect
    against a full path ("/dev/cu.usb"). This helper runs the glob
    against the basename directly so the naming-pattern logic in
    :func:`resolve_port_info` is unambiguous.
    """
    return fnmatchcase(name, pattern)


__all__ = [
    "enumerate_ports",
    "resolve_port_info",
]
