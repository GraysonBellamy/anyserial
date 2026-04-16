"""Generic POSIX implementation of :class:`SyncSerialBackend`.

This is the foundation every POSIX platform backend builds on. Linux,
Darwin, and BSD inherit the hot-path and lifecycle logic here and override
only the platform-specific bits (custom baud, low-latency, RS-485,
capabilities snapshot). On unknown POSIX variants the class is usable as-is
for the subset of features standard termios can express.

The backend owns exactly one piece of state: the integer fd (``-1`` when
closed). Every method is synchronous by DESIGN §25.1 contract; the async
readiness loop lives in :class:`SerialPort`. Hot-path methods
(:meth:`read_nonblocking`, :meth:`write_nonblocking`) never block because
the fd is opened ``O_NONBLOCK`` — they return immediately with a count or
raise :class:`BlockingIOError` on ``EAGAIN`` for the caller to re-park.
"""

from __future__ import annotations

import fcntl
import os
import sys
import termios
from typing import TYPE_CHECKING, Final

from anyserial._posix import ioctl as _ioctl
from anyserial._posix.baudrate import baudrate_to_speed
from anyserial._posix.termios_apply import (
    TermiosAttrs,
    apply_byte_size,
    apply_flow_control,
    apply_hangup,
    apply_parity,
    apply_raw_mode,
    apply_stop_bits,
)
from anyserial._types import Capability
from anyserial.capabilities import SerialCapabilities

if TYPE_CHECKING:
    from anyserial._types import ModemLines
    from anyserial.config import SerialConfig


_OPEN_FLAGS: Final[int] = os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
"""Flags every POSIX serial open uses.

``O_RDWR`` — serial ports are bidirectional; ``O_NOCTTY`` — don't let the tty
become our controlling terminal; ``O_NONBLOCK`` — the hot path relies on
non-blocking reads/writes; ``O_CLOEXEC`` — avoid leaking the fd across
``exec`` (absent on very old POSIX; falls back to ``0``).
"""


class PosixBackend:
    """Generic POSIX :class:`SyncSerialBackend`.

    Subclasses override the capability probe, custom-baud handling, and
    low-latency / RS-485 ioctls; the hot path and lifecycle stay common.
    Construction does no I/O — :meth:`open` is the single entry point that
    touches the kernel, matching the DESIGN §7.1 "constructor never touches
    the OS" rule so future per-platform factories can share this class.
    """

    __slots__ = ("_config", "_fd", "_path")

    def __init__(self) -> None:
        self._fd: int = -1
        self._path: str = ""
        self._config: SerialConfig | None = None

    # ------------------------------------------------------------------
    # SyncSerialBackend properties
    # ------------------------------------------------------------------

    @property
    def path(self) -> str:
        """Device path the backend was last opened on."""
        return self._path

    @property
    def is_open(self) -> bool:
        """Whether the backend currently owns a live fd."""
        return self._fd >= 0

    @property
    def capabilities(self) -> SerialCapabilities:
        """Capability snapshot for the generic POSIX backend.

        Linux/Darwin/BSD subclasses override this with platform-specific
        answers. At this layer most optional features are ``UNKNOWN`` —
        standard termios can express them but whether the driver accepts
        the request is only knowable at operation time.
        """
        return _posix_capabilities()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def open(self, path: str, config: SerialConfig) -> None:
        """Open ``path`` with the standard POSIX flags and apply ``config``.

        Raises:
            RuntimeError: The backend is already open.
            OSError: Any syscall failure — ``open``, ``flock``, ``tcsetattr``,
                or the config-apply ioctls. The orchestrator
                (:func:`open_serial_port`) catches ``OSError`` and routes it
                through :func:`errno_to_exception` with ``context="open"``.
        """
        if self._fd >= 0:
            msg = f"{type(self).__name__} is already open at {self._path!r}"
            raise RuntimeError(msg)

        fd = os.open(path, _OPEN_FLAGS)
        try:
            if config.exclusive:
                self._acquire_exclusive_lock(fd, path)
            self._apply_config_to_fd(fd, config)
        except BaseException:
            # Any failure after ``os.open`` must clean up the fd so the
            # orchestrator's exception mapping sees a clean state.
            os.close(fd)
            raise

        self._fd = fd
        self._path = path
        self._config = config

    def close(self) -> None:
        """Close the fd. Idempotent.

        :meth:`anyio.notify_closing` has already been called by
        :meth:`SerialPort.aclose` by the time we get here (DESIGN §12.3),
        so any pending readers/writers have been woken with
        :class:`anyio.ClosedResourceError`.
        """
        fd = self._fd
        if fd < 0:
            return
        self._fd = -1
        # The shielded scope in SerialPort.aclose guarantees we reach this
        # call even under cancellation — no need to defend against it here.
        os.close(fd)

    def fileno(self) -> int:
        """Return the underlying integer fd for readiness waiting."""
        return self._fd

    # ------------------------------------------------------------------
    # Hot path
    # ------------------------------------------------------------------

    def read_nonblocking(self, buffer: bytearray | memoryview) -> int:
        """Single non-blocking read into ``buffer``. See :class:`SyncSerialBackend`."""
        # ``os.readv`` fills the supplied buffer in place, avoiding the
        # allocation an ``os.read``+slice would incur. That's the whole
        # point of the receive-into path — no intermediate bytes object.
        return os.readv(self._fd, [buffer])

    def write_nonblocking(self, data: memoryview) -> int:
        """Single non-blocking write from ``data``. See :class:`SyncSerialBackend`."""
        return os.write(self._fd, data)

    # ------------------------------------------------------------------
    # Control plane
    # ------------------------------------------------------------------

    def configure(self, config: SerialConfig) -> None:
        """Apply ``config`` to the already-open fd.

        Called by :meth:`SerialPort.configure`; serialized by the port's
        ``_configure_lock`` so it does not race in-flight I/O.
        """
        self._apply_config_to_fd(self._fd, config)
        self._config = config

    def reset_input_buffer(self) -> None:
        """Discard unread input via ``tcflush(TCIFLUSH)``."""
        _ioctl.reset_input_buffer(self._fd)

    def reset_output_buffer(self) -> None:
        """Discard pending output via ``tcflush(TCOFLUSH)``."""
        _ioctl.reset_output_buffer(self._fd)

    def set_break(self, on: bool) -> None:
        """Assert (``on=True``) or de-assert the break condition."""
        _ioctl.set_break(self._fd, on=on)

    def tcdrain_blocking(self) -> None:
        """Blocking ``tcdrain``. Only call via :func:`anyio.to_thread.run_sync`."""
        termios.tcdrain(self._fd)

    def modem_lines(self) -> ModemLines:
        """Read CTS/DSR/RI/CD via ``TIOCMGET``."""
        return _ioctl.get_modem_lines(self._fd)

    def set_control_lines(
        self,
        *,
        rts: bool | None = None,
        dtr: bool | None = None,
    ) -> None:
        """Set RTS/DTR via ``TIOCMBIS``/``TIOCMBIC``. ``None`` leaves unchanged."""
        _ioctl.set_control_lines(self._fd, rts=rts, dtr=dtr)

    def input_waiting(self) -> int:
        """Bytes waiting in the kernel input queue."""
        return _ioctl.input_waiting(self._fd)

    def output_waiting(self) -> int:
        """Bytes pending in the kernel output queue."""
        return _ioctl.output_waiting(self._fd)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _acquire_exclusive_lock(fd: int, path: str) -> None:
        """Acquire an advisory exclusive lock; raise ``OSError(EBUSY)`` if taken.

        ``fcntl.flock`` raises :class:`BlockingIOError` (``EAGAIN`` or
        ``EWOULDBLOCK``) when another process already holds the lock. The
        orchestrator's :func:`errno_to_exception` maps ``EBUSY`` / ``EACCES``
        to :class:`PortBusyError`, so we translate the EAGAIN flavour here.
        """
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            msg = f"port {path!r} is exclusively locked by another process"
            raise OSError(_errno_ebusy(), msg, path) from exc

    def _apply_config_to_fd(self, fd: int, config: SerialConfig) -> None:
        """Translate ``config`` into termios flags and commit via ``tcsetattr``.

        Subclasses may override to layer platform-specific bits (custom
        baud, low-latency, RS-485) on top of the shared termios work.
        Keeping the shared path in its own method lets them call back into
        it via ``super()._apply_config_to_fd(fd, config)`` without
        duplicating the builder pipeline.
        """
        current = TermiosAttrs.from_list(termios.tcgetattr(fd))
        speed = baudrate_to_speed(config.baudrate)
        attrs = current.with_changes(ispeed=speed, ospeed=speed)
        attrs = apply_raw_mode(attrs)
        attrs = apply_byte_size(attrs, config.byte_size)
        attrs = apply_parity(attrs, config.parity)
        attrs = apply_stop_bits(attrs, config.stop_bits)
        attrs = apply_flow_control(attrs, config.flow_control)
        attrs = apply_hangup(attrs, hangup_on_close=config.hangup_on_close)
        termios.tcsetattr(fd, termios.TCSANOW, attrs.to_list())


def _errno_ebusy() -> int:
    """Return :data:`errno.EBUSY`.

    Isolated in a helper so the ``errno`` import stays local to this one
    code path — no runtime cost, and the module keeps a single clear
    reason to import ``errno``.
    """
    import errno  # noqa: PLC0415  — local by design

    return errno.EBUSY


def _posix_capabilities() -> SerialCapabilities:
    """Capability snapshot for the generic POSIX backend.

    Defensive defaults: ``UNKNOWN`` for optional termios features that a
    driver may or may not honour, ``UNSUPPORTED`` for Linux-specific
    mechanisms (custom baud, low-latency, RS-485) this layer cannot
    provide, and ``SUPPORTED`` for primitives the kernel always honours on
    any POSIX (input waiting, buffer flush, exclusive flock).
    """
    rts_cts = Capability.UNKNOWN if _has_rts_cts() else Capability.UNSUPPORTED
    break_cap = Capability.UNKNOWN if _has_break_support() else Capability.UNSUPPORTED
    mark_space = Capability.UNKNOWN if hasattr(termios, "CMSPAR") else Capability.UNSUPPORTED

    return SerialCapabilities(
        platform=sys.platform,
        backend="posix",
        custom_baudrate=Capability.UNSUPPORTED,
        mark_space_parity=mark_space,
        one_point_five_stop_bits=Capability.UNSUPPORTED,
        xon_xoff=Capability.UNKNOWN,
        rts_cts=rts_cts,
        dtr_dsr=Capability.UNSUPPORTED,
        modem_lines=Capability.UNKNOWN,
        break_signal=break_cap,
        exclusive_access=Capability.SUPPORTED,
        low_latency=Capability.UNSUPPORTED,
        rs485=Capability.UNSUPPORTED,
        input_waiting=Capability.SUPPORTED,
        output_waiting=Capability.UNKNOWN,
        port_discovery=Capability.UNSUPPORTED,
    )


def _has_rts_cts() -> bool:
    """Return ``True`` if termios exposes either hardware-handshake spelling."""
    if hasattr(termios, "CRTSCTS"):
        return True
    return hasattr(termios, "CCTS_OFLOW") and hasattr(termios, "CRTS_IFLOW")


def _has_break_support() -> bool:
    """Return ``True`` if break ioctls are reachable (stdlib or platform fallback).

    Python's ``termios`` module omits ``TIOCSBRK`` / ``TIOCCBRK`` on every
    POSIX we target. :mod:`anyserial._posix.ioctl` carries hardcoded
    kernel-ABI numeric fallbacks for Linux (``<asm/ioctls.h>``) and the
    BSD family — Darwin, FreeBSD, NetBSD, OpenBSD, DragonFly — which all
    share ``<sys/ttycom.h>``.
    """
    if hasattr(termios, "TIOCSBRK") and hasattr(termios, "TIOCCBRK"):
        return True
    platform = sys.platform
    return platform.startswith("linux") or platform == "darwin" or "bsd" in platform


__all__ = [
    "PosixBackend",
]
