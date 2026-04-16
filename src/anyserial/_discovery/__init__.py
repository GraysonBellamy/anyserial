"""Optional cross-platform discovery backends.

The native per-platform backends live under ``anyserial._linux.discovery``,
``anyserial._darwin.discovery``, ``anyserial._bsd.discovery``, and
``anyserial._windows.discovery``. The modules in this package are
*fallbacks* that opt-in callers can request via the ``backend=`` keyword
on :func:`anyserial.list_serial_ports`:

- :mod:`anyserial._discovery.pyudev` — Linux only, richer USB metadata via
  ``udev`` rules. Requires the ``anyserial[discovery-pyudev]`` extra.
- :mod:`anyserial._discovery.pyserial` — cross-platform via
  ``pyserial.tools.list_ports``. Requires the
  ``anyserial[discovery-pyserial]`` extra.

Each module exposes a single ``enumerate_ports() -> list[PortInfo]``
callable, matching the production-default contract from
:mod:`anyserial._linux.discovery`. Imports of the third-party packages
happen at call time so the cost of an unused fallback is exactly zero.
"""

from __future__ import annotations
