"""Optional pyserial-backed cross-platform discovery.

Wraps :func:`serial.tools.list_ports.comports` and rewrites each
``ListPortInfo`` into a :class:`PortInfo`. Useful as a source of USB
metadata on BSD (whose native enumerator returns device path only) and
as a cross-check for the native Linux / Darwin / Windows walkers during
migration from pySerial.

Requires the ``anyserial[discovery-pyserial]`` extra. ``pyserial`` is pure
Python and installs cleanly everywhere ``anyserial`` does.
"""

from __future__ import annotations

from typing import Any

from anyserial.discovery import PortInfo

_INSTALL_HINT = "pip install 'anyserial[discovery-pyserial]'"
# pyserial fills missing string fields with "n/a" rather than None.
# Normalize so callers can pattern-match `if port.hwid is None` without
# having to also remember the sentinel.
_PYSERIAL_PLACEHOLDER = "n/a"


def enumerate_ports() -> list[PortInfo]:
    """Return tty devices enumerated through ``pyserial.tools.list_ports``.

    Returns:
        Fresh list of :class:`PortInfo`. Order matches whatever
        ``comports()`` yields on the platform; callers that need stable
        ordering should sort on :attr:`PortInfo.device`.

    Raises:
        ImportError: The ``anyserial[discovery-pyserial]`` extra is not
            installed. Message includes the exact install command.
    """
    try:
        from serial.tools.list_ports import (  # type: ignore[import-untyped]  # noqa: PLC0415
            comports,
        )
    except ImportError as exc:
        msg = f"pyserial not installed; {_INSTALL_HINT}"
        raise ImportError(msg) from exc

    return [_to_port_info(p) for p in comports()]


def _to_port_info(p: Any) -> PortInfo:
    """Translate a ``pyserial`` ``ListPortInfo`` into a :class:`PortInfo`."""
    return PortInfo(
        device=p.device,
        name=_normalize(getattr(p, "name", None)),
        description=_normalize(getattr(p, "description", None)),
        hwid=_normalize(getattr(p, "hwid", None)),
        vid=getattr(p, "vid", None),
        pid=getattr(p, "pid", None),
        serial_number=_normalize(getattr(p, "serial_number", None)),
        manufacturer=_normalize(getattr(p, "manufacturer", None)),
        product=_normalize(getattr(p, "product", None)),
        location=_normalize(getattr(p, "location", None)),
        interface=_normalize(getattr(p, "interface", None)),
    )


def _normalize(value: str | None) -> str | None:
    """Map pyserial's ``"n/a"`` placeholder and empty strings to ``None``."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text == _PYSERIAL_PLACEHOLDER:
        return None
    return text


__all__ = ["enumerate_ports"]
