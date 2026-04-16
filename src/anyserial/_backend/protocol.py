"""Platform-boundary Protocols for serial backends.

Two Protocols model the two dispatch shapes described in :doc:`DESIGN` §25:

- :class:`SyncSerialBackend` — an OS-primitives object that owns an
  ``O_NONBLOCK`` file descriptor. POSIX backends (Linux, Darwin, BSD,
  generic POSIX) and the in-memory :class:`MockBackend` implement this
  Protocol. The async readiness loop lives in :class:`SerialPort`.
- :class:`AsyncSerialBackend` — a self-contained async backend that owns
  its own async primitives (Windows overlapped I/O, network-bridged
  serial, etc.). :class:`SerialPort` delegates directly to its async
  methods in this case.

Both Protocols are ``@runtime_checkable`` so :func:`SerialPort`'s dispatch
logic can pick the right path with an ``isinstance`` check. Keep these
Protocol definitions free of I/O-primitive imports — concrete backends
provide those themselves.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from anyserial._types import ModemLines
    from anyserial.capabilities import SerialCapabilities
    from anyserial.config import SerialConfig


@runtime_checkable
class SyncSerialBackend(Protocol):
    """OS-primitives backend driven by an external readiness loop.

    Implementations own an ``O_NONBLOCK`` file descriptor (or a loopback
    socket pair for tests). The hot-path methods ``read_nonblocking`` and
    ``write_nonblocking`` never block: they either return immediately with
    bytes transferred or raise :class:`BlockingIOError` on ``EAGAIN`` /
    :class:`InterruptedError` on ``EINTR``. :class:`SerialPort` is
    responsible for calling :func:`anyio.wait_readable` /
    :func:`anyio.wait_writable` around them.

    Lifecycle methods are synchronous because everything they do
    (``os.open``, ``termios.tcsetattr``, ``os.close``) completes in
    microseconds. The one genuinely blocking operation —
    :func:`tcdrain_blocking` — is documented as such and only called
    through :func:`anyio.to_thread.run_sync`.
    """

    @property
    def path(self) -> str:
        """Device path supplied to :meth:`open`."""
        ...

    @property
    def is_open(self) -> bool:
        """Whether the backend currently owns a valid fd."""
        ...

    @property
    def capabilities(self) -> SerialCapabilities:
        """Feature snapshot for this backend/device pair."""
        ...

    def open(self, path: str, config: SerialConfig) -> None:
        """Open the device and apply ``config``. Sync; must complete quickly."""
        ...

    def close(self) -> None:
        """Close the underlying fd and restore any modified kernel state."""
        ...

    def fileno(self) -> int:
        """Return the underlying integer fd for readiness waiting."""
        ...

    def read_nonblocking(self, buffer: bytearray | memoryview) -> int:
        """Single non-blocking read into ``buffer``.

        Returns:
            Number of bytes written into ``buffer``. Zero signals a clean
            EOF / disconnect — the caller turns this into
            :class:`SerialDisconnectedError`.

        Raises:
            BlockingIOError: No data available yet; caller re-parks on
                readiness.
            InterruptedError: ``EINTR``; caller retries.
            OSError: Translate via :func:`errno_to_exception` with
                ``context="io"``.
        """
        ...

    def write_nonblocking(self, data: memoryview) -> int:
        """Single non-blocking write from ``data``.

        Returns:
            Number of bytes accepted by the kernel (may be short).

        Raises:
            BlockingIOError: Kernel output queue full; caller re-parks.
            InterruptedError: ``EINTR``; caller retries.
            OSError: Translate via :func:`errno_to_exception` with
                ``context="io"``.
        """
        ...

    def configure(self, config: SerialConfig) -> None:
        """Apply ``config`` to the open device. Runs while I/O may be in flight."""
        ...

    def reset_input_buffer(self) -> None:
        """Discard unread input (``tcflush(TCIFLUSH)``)."""
        ...

    def reset_output_buffer(self) -> None:
        """Discard pending output (``tcflush(TCOFLUSH)``)."""
        ...

    def set_break(self, on: bool) -> None:
        """Start or stop a break condition (``TIOCSBRK`` / ``TIOCCBRK``).

        The coroutine-level sleep between start and stop is owned by
        :class:`SerialPort` so cancellation de-asserts the break via
        ``finally``.
        """
        ...

    def tcdrain_blocking(self) -> None:
        """Blocking ``tcdrain``. Only call via :func:`anyio.to_thread.run_sync`."""
        ...

    def modem_lines(self) -> ModemLines:
        """Snapshot input modem-status lines (``TIOCMGET``)."""
        ...

    def set_control_lines(
        self,
        *,
        rts: bool | None = None,
        dtr: bool | None = None,
    ) -> None:
        """Set output control lines; ``None`` means leave unchanged."""
        ...

    def input_waiting(self) -> int:
        """Bytes waiting in the kernel input queue (``FIONREAD`` / ``TIOCINQ``)."""
        ...

    def output_waiting(self) -> int:
        """Bytes waiting in the kernel output queue (``TIOCOUTQ``)."""
        ...


@runtime_checkable
class AsyncSerialBackend(Protocol):
    """Self-contained async backend for platforms without fd readiness.

    Used for Windows (overlapped I/O or worker-thread bridge; future),
    network-bridged serial, and any future platform where
    :func:`anyio.wait_readable` on a file descriptor is not meaningful. The
    contract is essentially :class:`anyio.abc.ByteStream`-shaped — ``send``
    and ``receive`` honour AnyIO cancellation, ``aclose`` is idempotent,
    and the blocking control-path calls are the backend's problem to
    dispatch (usually via :func:`anyio.to_thread.run_sync`).
    """

    @property
    def path(self) -> str: ...

    @property
    def is_open(self) -> bool: ...

    @property
    def capabilities(self) -> SerialCapabilities: ...

    async def open(self, path: str, config: SerialConfig) -> None: ...

    async def aclose(self) -> None: ...

    async def receive(self, max_bytes: int) -> bytes: ...

    async def receive_into(self, buffer: bytearray | memoryview) -> int: ...

    async def send(self, data: memoryview) -> None: ...

    async def configure(self, config: SerialConfig) -> None: ...

    async def reset_input_buffer(self) -> None: ...

    async def reset_output_buffer(self) -> None: ...

    async def drain(self) -> None: ...

    async def send_break(self, duration: float) -> None: ...

    async def modem_lines(self) -> ModemLines: ...

    async def set_control_lines(
        self,
        *,
        rts: bool | None = None,
        dtr: bool | None = None,
    ) -> None: ...

    def input_waiting(self) -> int: ...

    def output_waiting(self) -> int: ...


__all__ = [
    "AsyncSerialBackend",
    "SyncSerialBackend",
]
