"""Immutable, validated serial-port configuration.

All configuration is expressed as frozen dataclasses so it can be hashed,
compared, and shared safely across tasks. Validation runs in
``__post_init__`` — the only supported way to change a config at runtime is
:meth:`SerialConfig.with_changes`, which returns a new instance.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any, Self

from anyserial._types import (
    ByteSize,
    Parity,
    StopBits,
    UnsupportedPolicy,
)
from anyserial.exceptions import ConfigurationError

_MIN_READ_CHUNK_SIZE = 64
_MAX_READ_CHUNK_SIZE = 1 << 24  # 16 MiB — anything larger is almost certainly a bug


@dataclass(frozen=True, slots=True, kw_only=True)
class FlowControl:
    """Flow-control modes.

    Modeled as independent booleans rather than a single enum because
    ``xon_xoff`` (software) is orthogonal to the hardware lines ``rts_cts``
    and ``dtr_dsr`` — some configurations combine software with hardware
    flow control. Platforms may still reject specific combinations at apply
    time; that is reported as :class:`UnsupportedConfigurationError`.
    """

    xon_xoff: bool = False
    rts_cts: bool = False
    dtr_dsr: bool = False

    @classmethod
    def none(cls) -> Self:
        """Return a ``FlowControl`` with every mode disabled."""
        return cls()


@dataclass(frozen=True, slots=True, kw_only=True)
class RS485Config:
    """Kernel-level RS-485 configuration.

    Maps to Linux ``struct serial_rs485`` via :c:macro:`TIOCSRS485`. On
    platforms without kernel RS-485 support this config is rejected at
    apply time per :class:`UnsupportedPolicy`.

    Attributes:
        enabled: Whether RS-485 mode is active.
        rts_on_send: Logic level of RTS while transmitting.
        rts_after_send: Logic level of RTS after transmission completes.
        delay_before_send: Seconds to hold RTS before transmission starts.
        delay_after_send: Seconds to hold RTS after transmission ends.
        rx_during_tx: Whether receive is enabled while transmitting.
    """

    enabled: bool = True
    rts_on_send: bool = True
    rts_after_send: bool = False
    delay_before_send: float = 0.0
    delay_after_send: float = 0.0
    rx_during_tx: bool = False

    def __post_init__(self) -> None:
        """Validate delays are non-negative."""
        if self.delay_before_send < 0:
            raise ConfigurationError(
                f"delay_before_send must be >= 0 (got {self.delay_before_send!r})",
            )
        if self.delay_after_send < 0:
            raise ConfigurationError(
                f"delay_after_send must be >= 0 (got {self.delay_after_send!r})",
            )


@dataclass(frozen=True, slots=True, kw_only=True)
class SerialConfig:
    """Full serial-port configuration.

    Construct and validate up front; open the port with the result. All
    fields have sensible defaults. Use :meth:`with_changes` to derive a new
    config for runtime reconfiguration via :meth:`SerialPort.configure`.
    """

    baudrate: int = 115_200
    byte_size: ByteSize = ByteSize.EIGHT
    parity: Parity = Parity.NONE
    stop_bits: StopBits = StopBits.ONE
    flow_control: FlowControl = field(default_factory=FlowControl)
    exclusive: bool = False
    hangup_on_close: bool = True
    low_latency: bool = False
    read_chunk_size: int = 65_536
    rs485: RS485Config | None = None
    unsupported_policy: UnsupportedPolicy = UnsupportedPolicy.RAISE

    def __post_init__(self) -> None:
        """Validate the configuration body.

        The rules here are intentionally narrow — only things that are wrong
        independent of any platform or device. Driver- or device-level
        rejections surface as :class:`UnsupportedConfigurationError` when
        the config is applied.
        """
        if self.baudrate <= 0:
            raise ConfigurationError(
                f"baudrate must be positive (got {self.baudrate!r})",
            )
        if not (_MIN_READ_CHUNK_SIZE <= self.read_chunk_size <= _MAX_READ_CHUNK_SIZE):
            raise ConfigurationError(
                f"read_chunk_size must be between {_MIN_READ_CHUNK_SIZE} and "
                f"{_MAX_READ_CHUNK_SIZE} (got {self.read_chunk_size!r})",
            )
        # Mark/space parity requires 5-8 data bits (trivially satisfied here);
        # hardware flow control and XON/XOFF are independent booleans and we
        # don't forbid the combination at this layer — platforms that cannot
        # honour it raise UnsupportedConfigurationError at apply time.

    def with_changes(self, **changes: Any) -> Self:
        """Return a copy of this config with the given fields replaced.

        ``dataclasses.replace`` re-runs ``__post_init__``, so validation
        covers the new values.
        """
        return dataclasses.replace(self, **changes)


__all__ = [
    "FlowControl",
    "RS485Config",
    "SerialConfig",
]
