"""In-memory loopback backend for unit tests.

:class:`MockBackend` satisfies :class:`SyncSerialBackend` using a real
``socket.socketpair`` so :func:`anyio.wait_readable` actually fires on the
mock's file descriptor — the async read/write loops in :class:`SerialPort`
behave identically against a mock and against a real tty.

The mock is intentionally minimal:

- ``MockBackend.pair()`` returns two backends connected via a
  ``socketpair``; bytes written to one are available to read from the
  other.
- Every control-plane method (``configure``, ``modem_lines``,
  ``set_control_lines``, buffer resets, break, drain) succeeds without
  side effects and remembers the last config applied. Real platform
  backends are responsible for surfacing driver/device rejections.
- Fault injection is exposed via :class:`FaultPlan` — tests can set the
  knobs to drive every branch in the read / write loops without needing
  to recreate kernel-level error conditions.
"""

from __future__ import annotations

import errno
import os
import socket
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Self

from anyserial._types import Capability, ModemLines
from anyserial.capabilities import SerialCapabilities

if TYPE_CHECKING:
    from anyserial.config import SerialConfig


@dataclass(slots=True)
class FaultPlan:
    """Mutable knobs driving failure paths in :class:`MockBackend`.

    Every counter decrements on use; a value of zero means the fault is
    inactive. Boolean flags stay active until the test clears them.
    """

    eagain_reads: int = 0
    """Number of upcoming reads to fail with :class:`BlockingIOError`."""

    eagain_writes: int = 0
    """Number of upcoming writes to fail with :class:`BlockingIOError`."""

    eintr_reads: int = 0
    """Number of upcoming reads to fail with :class:`InterruptedError`."""

    eintr_writes: int = 0
    """Number of upcoming writes to fail with :class:`InterruptedError`."""

    short_write_max: int | None = None
    """Cap accepted bytes per write to at most this many, or ``None``."""

    disconnected: bool = False
    """When true, reads return 0 (EOF) and writes raise ``EPIPE``."""

    raise_eio_on_read: bool = False
    """When true, next read raises ``OSError(EIO)``; cleared after firing."""

    parity_errors: int = 0
    """Number of upcoming reads to fail with a simulated parity error.

    Raised as ``OSError(EIO, "mock: parity error")`` — real kernels surface
    parity violations via termios modes ``PARMRK`` / ``INPCK`` which the
    in-memory mock cannot reproduce exactly, so the mock stands in with an
    I/O error that tests the ``errno_to_exception`` mapping and the
    read-loop's error path.
    """


def _mock_capabilities() -> SerialCapabilities:
    """Return a :class:`SerialCapabilities` snapshot for the mock.

    Feature flags are ``SUPPORTED`` for the primitives the mock actually
    implements (input / output waiting backed by ``FIONREAD`` on the
    socket), ``UNSUPPORTED`` for features that have no meaning in memory
    (``low_latency``, ``rs485``, ``exclusive_access``), and ``UNKNOWN`` for
    the rest so tests can exercise the tri-state behaviour.
    """
    return SerialCapabilities(
        platform="mock",
        backend="mock",
        custom_baudrate=Capability.SUPPORTED,
        mark_space_parity=Capability.SUPPORTED,
        one_point_five_stop_bits=Capability.SUPPORTED,
        xon_xoff=Capability.UNKNOWN,
        rts_cts=Capability.UNKNOWN,
        dtr_dsr=Capability.UNKNOWN,
        modem_lines=Capability.SUPPORTED,
        break_signal=Capability.SUPPORTED,
        exclusive_access=Capability.UNSUPPORTED,
        low_latency=Capability.UNSUPPORTED,
        rs485=Capability.UNSUPPORTED,
        input_waiting=Capability.SUPPORTED,
        output_waiting=Capability.UNSUPPORTED,
        port_discovery=Capability.UNSUPPORTED,
    )


@dataclass(slots=True)
class _MockState:
    """Per-instance state split out so the public surface is cleaner."""

    sock: socket.socket
    path: str
    is_open: bool = False
    config: SerialConfig | None = None
    # Modem / control lines. ``peer`` is set after :meth:`pair` so writes to
    # ``rts`` on one end are observable as ``cts`` on the other.
    rts: bool = False
    dtr: bool = False
    peer: _MockState | None = None
    faults: FaultPlan = field(default_factory=FaultPlan)
    break_asserted: bool = False


class MockBackend:
    """``@runtime_checkable`` :class:`SyncSerialBackend` for tests.

    Construct backends only via :meth:`pair`; the direct constructor is
    private so tests don't accidentally make unlinked mocks.
    """

    __slots__ = ("_state",)

    def __init__(self, _state: _MockState) -> None:
        self._state = _state

    @classmethod
    def pair(
        cls,
        *,
        path_a: str = "/dev/mockA",
        path_b: str = "/dev/mockB",
    ) -> tuple[Self, Self]:
        """Create two linked :class:`MockBackend` instances.

        Both are already "opened" as far as the underlying socketpair is
        concerned, but :meth:`open` must still be called to transition each
        backend into the open state and apply a :class:`SerialConfig`.
        """
        # ``socket.socketpair()`` with no args picks the right family per
        # platform: AF_UNIX + SOCK_STREAM on POSIX, AF_INET emulated TCP
        # loopback on Windows (where AF_UNIX may not be available
        # depending on Python build / Windows SDK). Either way the pair
        # is a connected, full-duplex stream that integrates with
        # ``anyio.wait_readable`` via ``.fileno()``.
        sock_a, sock_b = socket.socketpair()
        sock_a.setblocking(False)
        sock_b.setblocking(False)
        state_a = _MockState(sock=sock_a, path=path_a)
        state_b = _MockState(sock=sock_b, path=path_b)
        state_a.peer = state_b
        state_b.peer = state_a
        return cls(state_a), cls(state_b)

    # ------------------------------------------------------------------
    # SyncSerialBackend properties
    # ------------------------------------------------------------------

    @property
    def path(self) -> str:
        return self._state.path

    @property
    def is_open(self) -> bool:
        return self._state.is_open

    @property
    def capabilities(self) -> SerialCapabilities:
        return _mock_capabilities()

    @property
    def faults(self) -> FaultPlan:
        """Mutable fault-injection plan. Test-only surface."""
        return self._state.faults

    @property
    def break_asserted(self) -> bool:
        """Whether :meth:`set_break` last asserted the break condition.

        Exposed for tests — real :class:`SyncSerialBackend` implementations
        do not need to expose this.
        """
        return self._state.break_asserted

    @property
    def last_config(self) -> SerialConfig | None:
        """Most recent config passed to :meth:`open` or :meth:`configure`."""
        return self._state.config

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def open(self, path: str, config: SerialConfig) -> None:
        """Mark the backend open and store the config for inspection."""
        if self._state.is_open:
            msg = f"MockBackend already open at {self._state.path!r}"
            raise RuntimeError(msg)
        # The socketpair is already connected; ``path`` is accepted for
        # API compatibility but the mock keeps the path it was constructed
        # with. Tests that want a specific path pass it via ``pair()``.
        self._state.path = path or self._state.path
        self._state.config = config
        self._state.is_open = True

    def close(self) -> None:
        """Close the underlying socket and mark the backend closed.

        Idempotent and always tears the socket down — the mock's pair
        owns its sockets from construction, not from :meth:`open`.
        """
        self._state.is_open = False
        if self._state.sock.fileno() != -1:
            self._state.sock.close()

    def fileno(self) -> int:
        return self._state.sock.fileno()

    # ------------------------------------------------------------------
    # Hot path
    # ------------------------------------------------------------------

    def read_nonblocking(self, buffer: bytearray | memoryview) -> int:
        """See :meth:`SyncSerialBackend.read_nonblocking`."""
        faults = self._state.faults
        if faults.raise_eio_on_read:
            faults.raise_eio_on_read = False
            raise OSError(errno.EIO, "mock: injected EIO on read")
        if faults.parity_errors > 0:
            faults.parity_errors -= 1
            raise OSError(errno.EIO, "mock: parity error")
        if faults.disconnected:
            return 0
        if faults.eintr_reads > 0:
            faults.eintr_reads -= 1
            raise InterruptedError(errno.EINTR, "mock: injected EINTR")
        if faults.eagain_reads > 0:
            faults.eagain_reads -= 1
            raise BlockingIOError(errno.EAGAIN, "mock: injected EAGAIN")
        return os.readv(self._state.sock.fileno(), [buffer])

    def write_nonblocking(self, data: memoryview) -> int:
        """See :meth:`SyncSerialBackend.write_nonblocking`."""
        faults = self._state.faults
        if faults.disconnected:
            raise BrokenPipeError(errno.EPIPE, "mock: disconnected")
        if faults.eintr_writes > 0:
            faults.eintr_writes -= 1
            raise InterruptedError(errno.EINTR, "mock: injected EINTR")
        if faults.eagain_writes > 0:
            faults.eagain_writes -= 1
            raise BlockingIOError(errno.EAGAIN, "mock: injected EAGAIN")
        view = data
        if faults.short_write_max is not None:
            view = data[: faults.short_write_max]
        if len(view) == 0:
            # Can't distinguish an empty send from EOF; refuse the call so
            # tests that ask for a zero-length write don't wedge.
            return 0
        return os.write(self._state.sock.fileno(), view)

    # ------------------------------------------------------------------
    # Control plane
    # ------------------------------------------------------------------

    def configure(self, config: SerialConfig) -> None:
        self._state.config = config

    def reset_input_buffer(self) -> None:
        """Drain the kernel input queue without propagating to the peer."""
        try:
            while True:
                if not self._state.sock.recv(4096):
                    break
        except BlockingIOError:
            pass

    def reset_output_buffer(self) -> None:
        # Socketpair writes go directly to the peer's receive queue; there
        # is no local output queue to flush. Mock is a no-op.
        return

    def set_break(self, on: bool) -> None:
        self._state.break_asserted = on

    def tcdrain_blocking(self) -> None:
        # Nothing to drain on a socketpair — writes are immediate.
        return

    def modem_lines(self) -> ModemLines:
        peer = self._state.peer
        if peer is None:
            return ModemLines(cts=False, dsr=False, ri=False, cd=False)
        # Local CTS is driven by the peer's RTS; local DSR by the peer's DTR.
        return ModemLines(cts=peer.rts, dsr=peer.dtr, ri=False, cd=peer.dtr)

    def set_control_lines(
        self,
        *,
        rts: bool | None = None,
        dtr: bool | None = None,
    ) -> None:
        if rts is not None:
            self._state.rts = rts
        if dtr is not None:
            self._state.dtr = dtr

    def input_waiting(self) -> int:
        """Bytes available to read without blocking.

        Implemented by peeking up to 64 KiB with ``MSG_PEEK``; the kernel
        does not expose ``FIONREAD`` on ``AF_UNIX`` socketpairs reliably
        enough to rely on.
        """
        try:
            return len(self._state.sock.recv(65_536, socket.MSG_PEEK))
        except BlockingIOError:
            return 0

    def output_waiting(self) -> int:
        # Socketpair writes are either immediate or raise EAGAIN; there is
        # no local output queue. Report zero.
        return 0


__all__ = [
    "FaultPlan",
    "MockBackend",
]
