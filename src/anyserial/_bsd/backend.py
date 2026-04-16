"""BSD platform :class:`SyncSerialBackend`.

One backend serves every BSD variant (FreeBSD, NetBSD, OpenBSD). The
shared termios surface is identical enough for the generic
:class:`PosixBackend` to carry the hot path and lifecycle; this
subclass layers the two BSD-specific behaviours on top:

- **Custom baud** goes through integer ``ispeed`` / ``ospeed``, not a
  ``Bxxxx`` bitflag encoding. The BSDs accept arbitrary rates directly
  via :func:`termios.tcsetattr`; see :mod:`anyserial._bsd.baudrate`.
- ``low_latency`` and ``rs485`` are :class:`UnsupportedPolicy`-routed
  rejections — no BSD equivalent of Linux's ``ASYNC_LOW_LATENCY`` knob,
  and kernel RS-485 is out of scope (DESIGN §18.2, §19.2).

Break signalling, modem lines, exclusive access, buffer flush, queue
depth, and the async hot path are inherited unchanged — the shared
:mod:`anyserial._posix.ioctl` module carries BSD-family numeric
fallbacks for ``TIOCSBRK`` / ``TIOCCBRK``.

Per DESIGN §36, BSD support is best-effort: variant-level
differences surface at hardware-test time. The policy-routing hook is
the deliberate choke point for telling users when the advertised
capability differs from what the running driver actually honours.
"""

from __future__ import annotations

import termios
import warnings
from typing import TYPE_CHECKING, override

from anyserial._bsd.baudrate import passthrough_rate
from anyserial._bsd.capabilities import bsd_capabilities
from anyserial._posix.backend import PosixBackend
from anyserial._posix.baudrate import is_standard_baud
from anyserial._posix.termios_apply import (
    TermiosAttrs,
    apply_byte_size,
    apply_flow_control,
    apply_hangup,
    apply_parity,
    apply_raw_mode,
    apply_stop_bits,
)
from anyserial._types import UnsupportedPolicy
from anyserial.exceptions import UnsupportedFeatureError

if TYPE_CHECKING:
    from anyserial.capabilities import SerialCapabilities
    from anyserial.config import SerialConfig


class BsdBackend(PosixBackend):
    """BSD :class:`SyncSerialBackend`.

    Subclasses :class:`PosixBackend`, overriding only the three hooks
    that differ on BSD: capability snapshot, custom-baud path, and the
    pre-open rejection of Linux-only features. Everything else — fd
    lifecycle, nonblocking read/write, modem-line ioctls, break — is
    inherited.
    """

    __slots__ = ()

    @property
    @override
    def capabilities(self) -> SerialCapabilities:
        """BSD capability snapshot (see :func:`bsd_capabilities`)."""
        return bsd_capabilities()

    @override
    def open(self, path: str, config: SerialConfig) -> None:
        """Reject BSD-unsupported features, then open via the base backend.

        Rejections run *before* ``os.open`` so a ``RAISE`` policy never
        leaves a transiently-open fd. ``WARN`` / ``IGNORE`` proceed with
        the offending fields effectively silenced — mirrors the Darwin
        backend's pattern exactly (DESIGN §18.2, §19.2).
        """
        self._reject_bsd_unsupported(config)
        super().open(path, config)

    @override
    def configure(self, config: SerialConfig) -> None:
        """Reject BSD-unsupported features, then reapply config."""
        self._reject_bsd_unsupported(config)
        super().configure(config)

    @override
    def _apply_config_to_fd(self, fd: int, config: SerialConfig) -> None:
        """Apply ``config`` via termios; custom baud goes straight into ispeed/ospeed.

        Standard rates keep the inherited ``tcgetattr`` + builder
        pipeline. Non-standard rates take the same builder pipeline but
        drop the caller's integer rate directly into ``c_ispeed`` /
        ``c_ospeed`` — on the BSDs those fields are plain integers, so
        ``tcsetattr`` accepts arbitrary values without a platform-specific
        ioctl (unlike Linux's ``TCSETS2`` or Darwin's ``IOSSIOSPEED``).
        """
        if is_standard_baud(config.baudrate):
            super()._apply_config_to_fd(fd, config)
            return
        self._apply_custom_baud_config(fd, config)

    def _apply_custom_baud_config(self, fd: int, config: SerialConfig) -> None:
        """Commit ``config`` via ``tcsetattr`` with a literal integer baud.

        The shared builders preserve every non-speed field from the
        current termios struct; we only override ``ispeed`` / ``ospeed``
        with the passthrough rate. If the kernel or driver rejects the
        rate (rare on USB-serial adapters, more common on on-board UARTs
        with clock-divider constraints), ``tcsetattr`` raises ``OSError``
        which the orchestrator maps to
        :class:`UnsupportedConfigurationError` via ``errno_to_exception``.
        """
        current = TermiosAttrs.from_list(termios.tcgetattr(fd))
        rate = passthrough_rate(config.baudrate)
        attrs = current.with_changes(ispeed=rate, ospeed=rate)
        attrs = apply_raw_mode(attrs)
        attrs = apply_byte_size(attrs, config.byte_size)
        attrs = apply_parity(attrs, config.parity)
        attrs = apply_stop_bits(attrs, config.stop_bits)
        attrs = apply_flow_control(attrs, config.flow_control)
        attrs = apply_hangup(attrs, hangup_on_close=config.hangup_on_close)
        termios.tcsetattr(fd, termios.TCSANOW, attrs.to_list())

    # ------------------------------------------------------------------
    # BSD-unsupported feature rejection
    # ------------------------------------------------------------------

    def _reject_bsd_unsupported(self, config: SerialConfig) -> None:
        """Apply :class:`UnsupportedPolicy` to BSD-only ``UNSUPPORTED`` features.

        Checked fields mirror :func:`bsd_capabilities`:

        - ``rs485`` → kernel RS-485 is out of scope on BSD.
        - ``low_latency`` → no BSD equivalent for ``ASYNC_LOW_LATENCY``.

        Under ``RAISE`` the first rejection wins; under ``WARN`` both
        fire. Shape matches :meth:`DarwinBackend._reject_darwin_unsupported`.
        """
        if config.rs485 is not None:
            self._reject_feature("RS-485", config.unsupported_policy)
        if config.low_latency:
            self._reject_feature("low_latency", config.unsupported_policy)

    @staticmethod
    def _reject_feature(feature: str, policy: UnsupportedPolicy) -> None:
        """Route a BSD-unsupported feature request through ``policy``.

        No underlying ``OSError`` is available because there is no ioctl
        to attempt in the first place — synthesise the user-facing
        message directly, matching :meth:`DarwinBackend._reject_feature`.
        """
        match policy:
            case UnsupportedPolicy.RAISE:
                msg = f"{feature} is not supported on BSD"
                raise UnsupportedFeatureError(msg)
            case UnsupportedPolicy.WARN:
                warnings.warn(
                    f"{feature} request ignored: not supported on BSD",
                    RuntimeWarning,
                    # warn → _reject_feature → open/configure → open_serial_port →
                    # user; stacklevel=4 lands on the user's call site.
                    stacklevel=4,
                )
            case UnsupportedPolicy.IGNORE:
                return


__all__ = [
    "BsdBackend",
]
