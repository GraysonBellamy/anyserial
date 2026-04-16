"""Darwin platform :class:`SyncSerialBackend`.

Subclasses :class:`PosixBackend` and layers Darwin-specific behaviour on top:

- Custom baud rates go through ``IOSSIOSPEED`` (see
  :mod:`anyserial._darwin.baudrate`), not ``TCSETS2`` (which is Linux-only).
  The standard ``termios.B*`` path still handles standard rates.
- ``low_latency`` and ``rs485`` are :class:`UnsupportedPolicy`-routed
  rejections — Darwin has no equivalent to Linux's ``ASYNC_LOW_LATENCY``
  or ``TIOCSRS485`` (DESIGN §18.2, §19.2). Rejections happen *before*
  opening the fd so a ``RAISE`` policy doesn't leave a device transiently
  open.
- The capability snapshot comes from :func:`darwin_capabilities`.

Break signalling, modem lines, exclusive access, buffer flush, queue
depth, and the async hot path are inherited unchanged from
:class:`PosixBackend` — the shared :mod:`anyserial._posix.ioctl` module
carries Darwin's ``TIOCSBRK`` / ``TIOCCBRK`` numeric fallbacks, so the
inherited :meth:`set_break` works on Darwin without any Darwin-specific
override.
"""

from __future__ import annotations

import termios
import warnings
from typing import TYPE_CHECKING, override

from anyserial._darwin.baudrate import set_iossiospeed
from anyserial._darwin.capabilities import darwin_capabilities
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


# Any standard Darwin baud works as a placeholder here; B9600 has been
# defined since V7 UNIX and is guaranteed present on every termios build.
# The actual rate is applied by the follow-up IOSSIOSPEED call.
_PLACEHOLDER_SPEED: int = termios.B9600


class DarwinBackend(PosixBackend):
    """Darwin :class:`SyncSerialBackend`.

    Adds one Darwin-only behaviour on top of the generic POSIX backend
    (custom baud via :data:`IOSSIOSPEED`) and reports honest
    ``UNSUPPORTED`` for the Linux-only features (``low_latency``,
    ``rs485``) with policy-driven rejection. Everything else — fd
    lifecycle, nonblocking read/write, modem-line ioctls, break — is
    inherited from :class:`PosixBackend`.
    """

    __slots__ = ()

    @property
    @override
    def capabilities(self) -> SerialCapabilities:
        """Darwin-specific capability snapshot (see :func:`darwin_capabilities`)."""
        return darwin_capabilities()

    @override
    def open(self, path: str, config: SerialConfig) -> None:
        """Reject Darwin-unsupported features, then open via the base backend.

        Rejections run *before* the ``os.open`` so a ``RAISE`` policy
        never leaves a transiently-open fd behind. Under ``WARN`` /
        ``IGNORE`` the warning is emitted and the open proceeds with the
        offending fields effectively ignored.
        """
        self._reject_darwin_unsupported(config)
        super().open(path, config)

    @override
    def configure(self, config: SerialConfig) -> None:
        """Reject Darwin-unsupported features, then reapply config.

        Same contract as :meth:`open`: rejections happen up front so the
        fd's current state isn't disturbed by a later step raising. Under
        ``WARN`` / ``IGNORE`` the reconfigure still runs and commits
        every field the platform *does* support.
        """
        self._reject_darwin_unsupported(config)
        super().configure(config)

    @override
    def _apply_config_to_fd(self, fd: int, config: SerialConfig) -> None:
        """Apply ``config`` via termios; reroute non-standard baud to ``IOSSIOSPEED``.

        Standard rates stay on the inherited ``tcgetattr`` + builder
        pipeline. Non-standard rates get the same builder pipeline with
        a placeholder standard baud in ``c_ispeed`` / ``c_ospeed`` so the
        ``tcsetattr`` call succeeds, followed by a single ``IOSSIOSPEED``
        ioctl that overrides the hardware line speed with the caller's
        actual rate.
        """
        if is_standard_baud(config.baudrate):
            super()._apply_config_to_fd(fd, config)
            return
        self._apply_custom_baud_config(fd, config)

    def _apply_custom_baud_config(self, fd: int, config: SerialConfig) -> None:
        """Commit ``config`` via ``tcsetattr`` + ``IOSSIOSPEED``.

        The two-step is mandatory: ``IOSSIOSPEED`` alone does not set
        byte size / parity / stop bits / flow control, and ``tcsetattr``
        alone cannot encode a non-standard rate on Darwin. The
        placeholder speed is never visible to the hardware — it sits in
        the termios struct only for the duration between the two calls.
        """
        current = TermiosAttrs.from_list(termios.tcgetattr(fd))
        attrs = current.with_changes(
            ispeed=_PLACEHOLDER_SPEED,
            ospeed=_PLACEHOLDER_SPEED,
        )
        attrs = apply_raw_mode(attrs)
        attrs = apply_byte_size(attrs, config.byte_size)
        attrs = apply_parity(attrs, config.parity)
        attrs = apply_stop_bits(attrs, config.stop_bits)
        attrs = apply_flow_control(attrs, config.flow_control)
        attrs = apply_hangup(attrs, hangup_on_close=config.hangup_on_close)
        termios.tcsetattr(fd, termios.TCSANOW, attrs.to_list())
        set_iossiospeed(fd, config.baudrate)

    # ------------------------------------------------------------------
    # Darwin-unsupported feature rejection
    # ------------------------------------------------------------------

    def _reject_darwin_unsupported(self, config: SerialConfig) -> None:
        """Apply :class:`UnsupportedPolicy` to Darwin-only ``UNSUPPORTED`` features.

        Checked fields mirror :func:`darwin_capabilities`:

        - ``rs485`` → Darwin has no ``TIOCSRS485`` equivalent.
        - ``low_latency`` → no ``ASYNC_LOW_LATENCY`` equivalent.

        Both are independent; if the user sets both and the policy is
        ``RAISE``, only the first rejection is reported (the second is
        dead code until the user fixes the first). Under ``WARN`` both
        warnings fire.
        """
        if config.rs485 is not None:
            self._reject_feature("RS-485", config.unsupported_policy)
        if config.low_latency:
            self._reject_feature("low_latency", config.unsupported_policy)

    @staticmethod
    def _reject_feature(feature: str, policy: UnsupportedPolicy) -> None:
        """Route a Darwin-unsupported feature request through ``policy``.

        No underlying ``OSError`` is available because there is no ioctl
        to attempt in the first place — Darwin simply has no mechanism
        for ``low_latency`` / ``rs485``. The Linux pattern
        (:meth:`LinuxBackend._handle_unsupported`) wraps a live
        ``OSError``; here we synthesize the user-facing message directly.
        """
        match policy:
            case UnsupportedPolicy.RAISE:
                msg = f"{feature} is not supported on Darwin"
                raise UnsupportedFeatureError(msg)
            case UnsupportedPolicy.WARN:
                warnings.warn(
                    f"{feature} request ignored: not supported on Darwin",
                    RuntimeWarning,
                    # warn → _reject_feature → open/configure → open_serial_port →
                    # user; stacklevel=4 points at the user's call site, matching
                    # LinuxBackend._handle_unsupported.
                    stacklevel=4,
                )
            case UnsupportedPolicy.IGNORE:
                return


__all__ = [
    "DarwinBackend",
]
