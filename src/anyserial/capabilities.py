"""Capability reporting and typed-attribute keys for ``SerialPort``.

:class:`SerialCapabilities` collapses platform-, driver-, and device-level
feature availability into a single reportable snapshot, using the tri-state
:class:`~anyserial._types.Capability` so callers can distinguish "this
platform advertises the feature but the driver may still reject it" from
"this platform has no mechanism for the feature at all".

:class:`SerialStreamAttribute` is an AnyIO typed-attribute set that exposes
those snapshots (plus the active :class:`SerialConfig` and discovery
:class:`PortInfo`) through the stream's ``extra()`` / ``extra_attributes``
interface — the canonical AnyIO pattern for backend-specific metadata.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from anyio import TypedAttributeSet, typed_attribute

if TYPE_CHECKING:
    from anyserial._types import Capability
    from anyserial.config import SerialConfig
    from anyserial.discovery import PortInfo


@dataclass(frozen=True, slots=True, kw_only=True)
class SerialCapabilities:
    """Feature-support snapshot for a backend (and, after open, a device).

    The ``platform`` and ``backend`` strings identify where the snapshot came
    from; the remaining fields are tri-state :class:`Capability` values.
    ``UNKNOWN`` means "the platform exposes a mechanism, but whether the
    driver or device will accept a specific request is only knowable at
    operation time" — callers should be prepared for
    :class:`UnsupportedConfigurationError` even when a capability reads
    ``SUPPORTED`` or ``UNKNOWN``.
    """

    platform: str
    backend: str
    custom_baudrate: Capability
    mark_space_parity: Capability
    one_point_five_stop_bits: Capability
    xon_xoff: Capability
    rts_cts: Capability
    dtr_dsr: Capability
    modem_lines: Capability
    break_signal: Capability
    exclusive_access: Capability
    low_latency: Capability
    rs485: Capability
    input_waiting: Capability
    output_waiting: Capability
    port_discovery: Capability


class SerialStreamAttribute(TypedAttributeSet):
    """Typed attributes exposed via ``port.extra(...)``.

    These compose with AnyIO's own typed-attribute sets (notably
    ``anyio.streams.file.FileStreamAttribute`` for ``fileno`` on POSIX). Code
    that knows nothing about ``SerialPort`` can still request attributes it
    understands, and Windows-style backends can omit POSIX-only attributes
    (like ``fileno``) without a type-level compromise.
    """

    capabilities: SerialCapabilities = typed_attribute()
    config: SerialConfig = typed_attribute()
    # Populated when discovery can resolve the device path (USB-attached
    # adapters, on-board UARTs visible under /sys/class/tty). Pseudo
    # terminals and platforms without a native discovery backend yield no
    # entry, so a typed-attribute lookup raises TypedAttributeLookupError.
    port_info: PortInfo = typed_attribute()


__all__ = [
    "SerialCapabilities",
    "SerialStreamAttribute",
]
