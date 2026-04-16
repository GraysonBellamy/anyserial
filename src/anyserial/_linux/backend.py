"""Linux platform :class:`SyncSerialBackend`.

Subclasses :class:`PosixBackend` and layers Linux-specific behaviour on top:

- Custom baud rates go through ``TCGETS2`` / ``TCSETS2`` / ``BOTHER``, not
  the legacy ``Bxxxx`` bitflags. Standard rates still take the inherited
  ``tcsetattr`` path so they keep the battle-tested behaviour.
- ``low_latency=True`` enables ``ASYNC_LOW_LATENCY`` via ``TIOCSSERIAL``
  and, on FTDI ports, drops the sysfs ``latency_timer`` to 1 ms. Both
  originals are saved at open and restored at close so the next process
  to open the device inherits the kernel default rather than ours.
- The capability snapshot reports firm answers for the features Linux
  always exposes (custom baud, flow control, break, queue depth,
  low-latency, mark/space parity).

The hot path, lifecycle scaffolding, and most control-plane methods are
inherited unchanged from :class:`PosixBackend`; only :meth:`open` and
:meth:`close` need overrides to thread the low-latency save/restore
discipline around the parent's behaviour.
"""

from __future__ import annotations

import contextlib
import warnings
from typing import TYPE_CHECKING, override

from anyserial._linux.baudrate import (
    NCCS2,
    Termios2Attrs,
    mark_bother,
    read_termios2,
    write_termios2,
)
from anyserial._linux.capabilities import linux_capabilities
from anyserial._linux.low_latency import (
    FtdiLatencyTimer,
    enable_low_latency,
    restore_ftdi_latency_timer,
    restore_serial_flags,
    tune_ftdi_latency_timer,
)
from anyserial._linux.rs485 import (
    RS485State,
    read_rs485,
    write_rs485,
)
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
    from anyserial.config import RS485Config, SerialConfig


class LinuxBackend(PosixBackend):
    """Linux :class:`SyncSerialBackend`.

    Adds two Linux-only behaviours on top of the generic POSIX backend:
    custom baud via ``TCSETS2`` (so any rate the kernel and adapter
    accept works, not just the dozen-odd ``Bxxxx`` constants) and the
    low-latency knobs from DESIGN §18.1. Everything else — fd lifecycle,
    nonblocking read/write, modem-line ioctls, break — is inherited.
    """

    __slots__ = ("_ftdi_timer", "_saved_async_flags", "_saved_rs485")

    def __init__(self) -> None:
        super().__init__()
        # Originals captured at open time when ``low_latency=True``;
        # both stay ``None`` for every other open. close() reads them
        # to decide whether to restore.
        self._saved_async_flags: int | None = None
        self._ftdi_timer: FtdiLatencyTimer | None = None
        # Pre-touch ``struct serial_rs485`` snapshot captured on the
        # first successful apply (whether in ``open`` or ``configure``).
        # Stays ``None`` while RS-485 has never been written; close()
        # reads it to decide whether to restore. Once set it sticks
        # until close or an explicit ``rs485=None`` reconfigure.
        self._saved_rs485: RS485State | None = None

    @property
    @override
    def capabilities(self) -> SerialCapabilities:
        """Linux-specific capability snapshot (see :func:`linux_capabilities`)."""
        return linux_capabilities()

    @override
    def open(self, path: str, config: SerialConfig) -> None:
        """Open ``path``, apply termios, then layer low-latency + RS-485.

        Delegates to :meth:`PosixBackend.open` for the fd open + lock +
        termios apply, then runs the optional :data:`config.low_latency`
        and :data:`config.rs485` paths. Any failure during these post-open
        tuning steps tears the fd back down before re-raising so the
        orchestrator never sees a half-open backend.
        """
        super().open(path, config)
        try:
            if config.low_latency:
                self._enable_low_latency(config.unsupported_policy)
            if config.rs485 is not None:
                self._apply_rs485(config.rs485, config.unsupported_policy)
        except BaseException:
            # Roll the fd state back so a retry / fallback starts clean.
            # close() also restores any partial state we may have saved
            # before the failure (e.g. async flags set, FTDI raise,
            # RS-485 written then a follow-up step raised).
            with contextlib.suppress(Exception):
                self.close()
            raise

    @override
    def configure(self, config: SerialConfig) -> None:
        """Re-apply ``config`` — termios first, then RS-485.

        Runtime reconfiguration goes through :meth:`PosixBackend.configure`
        for the termios / termios2 update, then layers the RS-485 step on
        top if ``config.rs485`` is set. Dropping back to ``rs485=None``
        after RS-485 was previously enabled restores the pre-touch
        ``struct serial_rs485`` so the device returns to whatever state
        it was in before ``anyserial`` wrote to it.
        """
        super().configure(config)
        if config.rs485 is not None:
            self._apply_rs485(config.rs485, config.unsupported_policy)
        elif self._saved_rs485 is not None:
            # User explicitly dropped RS-485. Best-effort restore — the
            # fd is still open, but a driver that accepted TIOCSRS485
            # on open may still refuse a second write (rare); swallow
            # OSError so ``configure`` doesn't raise on rollback.
            with contextlib.suppress(OSError):
                write_rs485(self._fd, self._saved_rs485)
            self._saved_rs485 = None

    @override
    def close(self) -> None:
        """Restore low-latency and RS-485 originals before closing the fd."""
        fd = self._fd
        if fd >= 0:
            self._restore_low_latency(fd)
            self._restore_rs485(fd)
        super().close()

    @override
    def _apply_config_to_fd(self, fd: int, config: SerialConfig) -> None:
        """Apply ``config`` via termios; reroute non-standard baud to termios2.

        Standard rates stay on the inherited ``tcgetattr`` + builder
        pipeline — fewer moving parts, no kernel-ABI reliance beyond what
        stdlib ``termios`` already wraps. Non-standard rates read the
        current ``struct termios2``, thread its fields through the shared
        builders, set ``c_cflag``'s CBAUD slot to ``BOTHER``, and commit
        with a single ``TCSETS2`` ioctl.
        """
        if is_standard_baud(config.baudrate):
            super()._apply_config_to_fd(fd, config)
            return
        self._apply_custom_baud_config(fd, config)

    def _apply_custom_baud_config(self, fd: int, config: SerialConfig) -> None:
        """Commit ``config`` via ``TCGETS2`` + builders + ``TCSETS2``.

        The shared ``apply_*`` builders operate on :class:`TermiosAttrs`,
        which has the same ``iflag`` / ``oflag`` / ``cflag`` / ``lflag`` /
        ``cc`` fields as :class:`Termios2Attrs`. We project, transform, and
        project back — the ``line`` discipline byte and the ``ispeed`` /
        ``ospeed`` integers stay on the termios2 side of the fence.
        """
        current = read_termios2(fd)

        attrs = TermiosAttrs(
            iflag=current.iflag,
            oflag=current.oflag,
            cflag=current.cflag,
            lflag=current.lflag,
            # Speeds are placeholders; the termios2 path ignores them.
            ispeed=0,
            ospeed=0,
            cc=tuple(current.cc),
        )
        attrs = apply_raw_mode(attrs)
        attrs = apply_byte_size(attrs, config.byte_size)
        attrs = apply_parity(attrs, config.parity)
        attrs = apply_stop_bits(attrs, config.stop_bits)
        attrs = apply_flow_control(attrs, config.flow_control)
        attrs = apply_hangup(attrs, hangup_on_close=config.hangup_on_close)

        updated = Termios2Attrs(
            iflag=attrs.iflag,
            oflag=attrs.oflag,
            cflag=mark_bother(attrs.cflag),
            lflag=attrs.lflag,
            line=current.line,
            cc=bytes(_cc_bytes(attrs.cc)),
            ispeed=config.baudrate,
            ospeed=config.baudrate,
        )
        write_termios2(fd, updated)

    # ------------------------------------------------------------------
    # Low-latency helpers
    # ------------------------------------------------------------------

    def _enable_low_latency(self, policy: UnsupportedPolicy) -> None:
        """Enable ``ASYNC_LOW_LATENCY`` and drop the FTDI timer to 1 ms.

        Each step is independent: a driver may accept ``TIOCSSERIAL`` but
        not be an FTDI port (so the sysfs path is a no-op), or vice versa
        on a hypothetical future kernel. Failures are routed through
        ``policy`` per DESIGN §9.1 — ``RAISE`` (default) propagates as
        :class:`UnsupportedFeatureError`, ``WARN`` emits a runtime warning
        and continues, ``IGNORE`` is silent.
        """
        try:
            self._saved_async_flags = enable_low_latency(self._fd)
        except OSError as exc:
            self._handle_unsupported(exc, policy, "ASYNC_LOW_LATENCY")
            self._saved_async_flags = None

        try:
            self._ftdi_timer = tune_ftdi_latency_timer(self._path)
        except OSError as exc:
            self._handle_unsupported(exc, policy, "FTDI latency_timer")
            self._ftdi_timer = None

    def _restore_low_latency(self, fd: int) -> None:
        """Best-effort restore of any low-latency originals captured at open.

        Failures are swallowed: the fd is on its way out and there is no
        useful action the caller could take. ``close()`` cannot raise
        without leaking an fd, so we suppress and clear regardless.
        """
        if self._saved_async_flags is not None:
            saved = self._saved_async_flags
            self._saved_async_flags = None
            with contextlib.suppress(OSError):
                restore_serial_flags(fd, saved)
        if self._ftdi_timer is not None:
            saved_timer = self._ftdi_timer
            self._ftdi_timer = None
            with contextlib.suppress(OSError):
                restore_ftdi_latency_timer(saved_timer)

    # ------------------------------------------------------------------
    # RS-485 helpers
    # ------------------------------------------------------------------

    def _apply_rs485(self, config: RS485Config, policy: UnsupportedPolicy) -> None:
        """Apply ``config`` via ``TIOCSRS485``, saving originals on first use.

        The first successful apply in the lifetime of the fd snapshots
        the pre-touch ``struct serial_rs485`` into :attr:`_saved_rs485`
        so :meth:`close` (and ``configure(rs485=None)``) can restore it.
        Subsequent applies read the current state and re-merge the
        config-owned flag bits, preserving driver-reserved bits
        (``TERMINATE_BUS``, address mode) that the user cannot express
        through :class:`RS485Config`.

        Failures route through ``policy`` per DESIGN §9.1, mirroring
        :meth:`_enable_low_latency`. A ``WARN`` or ``IGNORE`` policy on
        a driver that rejects ``TIOCGRS485`` leaves :attr:`_saved_rs485`
        at ``None``, so close is a no-op.
        """
        try:
            current = read_rs485(self._fd)
        except OSError as exc:
            self._handle_unsupported(exc, policy, "TIOCSRS485")
            return

        if self._saved_rs485 is None:
            self._saved_rs485 = current

        merged = current.with_flags_from(config)
        try:
            write_rs485(self._fd, merged)
        except OSError as exc:
            self._handle_unsupported(exc, policy, "TIOCSRS485")

    def _restore_rs485(self, fd: int) -> None:
        """Best-effort restore of the pre-touch ``struct serial_rs485``.

        Same contract as :meth:`_restore_low_latency` — failures are
        swallowed because close() is the teardown path and there is
        nothing useful to do with an error at this point.
        """
        if self._saved_rs485 is None:
            return
        saved = self._saved_rs485
        self._saved_rs485 = None
        with contextlib.suppress(OSError):
            write_rs485(fd, saved)

    @staticmethod
    def _handle_unsupported(
        exc: OSError,
        policy: UnsupportedPolicy,
        feature: str,
    ) -> None:
        """Apply :class:`UnsupportedPolicy` to a low-latency apply failure."""
        match policy:
            case UnsupportedPolicy.RAISE:
                msg = f"{feature} is not supported on this device: {exc.strerror or exc}"
                raise UnsupportedFeatureError(exc.errno, msg, exc.filename) from exc
            case UnsupportedPolicy.WARN:
                warnings.warn(
                    f"{feature} request ignored: {exc.strerror or exc}",
                    RuntimeWarning,
                    stacklevel=4,
                )
            case UnsupportedPolicy.IGNORE:
                return


def _cc_bytes(cc: tuple[bytes | int, ...]) -> bytes:
    """Coerce a heterogeneous ``cc`` tuple to the 19-byte payload ``struct termios2`` expects.

    ``TermiosAttrs`` stores ``cc`` as ``tuple[bytes | int, ...]`` because
    :func:`termios.tcgetattr` hands back bytes for some indices on some
    platforms. The termios2 payload is a flat byte sequence; any stray
    length mismatch is caught here rather than at the kernel.
    """
    out = bytearray(NCCS2)
    for index, value in enumerate(cc[:NCCS2]):
        if isinstance(value, (bytes, bytearray, memoryview)):
            byte = bytes(value)
            out[index] = byte[0] if byte else 0
        else:
            out[index] = value & 0xFF
    return bytes(out)


__all__ = [
    "LinuxBackend",
]
