"""Async :class:`SerialPort` and :func:`open_serial_port` entry point.

The read / write loops live here, not in the backends — the canonical
pattern separates OS primitives (non-blocking ``os.read`` / ``os.write``
owned by the :class:`SyncSerialBackend`) from async readiness waiting
(:func:`anyio.wait_readable` / :func:`anyio.wait_writable` owned by this
module). See :doc:`DESIGN` §12 and §25 for the rationale.

:class:`SerialPort` is the public symbol but is an abstract façade: its
``__new__`` dispatches to one of two concrete subclasses depending on the
Protocol the supplied backend satisfies.

- :class:`_PosixSerialPort` wraps a :class:`SyncSerialBackend` and owns the
  :func:`anyio.wait_readable` / :func:`anyio.wait_writable` readiness loop.
- :class:`_AsyncBackendSerialPort` wraps an :class:`AsyncSerialBackend` and
  delegates hot-path I/O directly. Used by the Windows backend.

Tests construct ``SerialPort(backend, config)`` exactly as before; the
dispatch is invisible to callers.
"""

from __future__ import annotations

import contextlib
import errno as _errno
import warnings
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self, override

import anyio
import anyio.abc
import anyio.to_thread
from anyio.streams.file import FileStreamAttribute

from anyserial._backend import (
    AsyncSerialBackend,
    SyncSerialBackend,
    select_backend,
)
from anyserial.capabilities import SerialStreamAttribute
from anyserial.config import SerialConfig
from anyserial.exceptions import (
    SerialClosedError,
    SerialDisconnectedError,
    errno_to_exception,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from anyserial._types import BytesLike, ModemLines
    from anyserial.capabilities import SerialCapabilities
    from anyserial.discovery import PortInfo

_DEFAULT_RECEIVE_LIMIT = 65_536
_MAX_DRAIN_INTERVAL = 0.050
_MIN_DRAIN_INTERVAL = 0.001


def _resolve_port_info_for_path(path: str) -> PortInfo | None:
    """Best-effort sync lookup of :class:`PortInfo` for ``path``.

    Used by :func:`open_serial_port` to populate the ``port_info`` typed
    attribute. Contract: *never raise*. Opening a serial port must not
    fail because metadata enrichment hiccupped on a USB unplug, a
    permission denial, or a missing platform backend — every failure
    mode maps to ``None`` so the typed attribute is simply omitted.
    """
    resolver = _platform_port_info_resolver()
    if resolver is None:
        return None
    try:
        return resolver(path)
    except OSError:
        return None


def _platform_port_info_resolver() -> Callable[[str], PortInfo | None] | None:
    """Return the platform-native :func:`resolve_port_info`, or ``None``.

    Lazy-imported per platform to keep module import cheap and to keep
    non-host backends out of the import graph. ``None`` means "this
    host has no native discovery backend", which :func:`_resolve_port_info_for_path`
    maps to "port_info typed attribute omitted".
    """
    import sys  # noqa: PLC0415 — local to keep module import cheap

    # Read once into a local so mypy / pyright don't narrow each branch
    # to the typechecker's host platform and flag the others unreachable.
    platform = sys.platform
    if platform.startswith("linux"):
        from anyserial._linux.discovery import (  # noqa: PLC0415 — lazy by platform
            resolve_port_info as linux_resolver,
        )

        return linux_resolver
    if platform == "darwin":
        from anyserial._darwin.discovery import (  # noqa: PLC0415 — lazy by platform
            resolve_port_info as darwin_resolver,
        )

        return darwin_resolver
    if "bsd" in platform or platform.startswith("dragonfly"):
        from anyserial._bsd.discovery import (  # noqa: PLC0415 — lazy by platform
            resolve_port_info as bsd_resolver,
        )

        return bsd_resolver
    return None


class SerialPort(anyio.abc.ByteStream):
    """Async serial-port stream.

    Implements :class:`anyio.abc.ByteStream` so it composes with every AnyIO
    stream helper. Construction does not touch the OS — use
    :func:`open_serial_port` (or :meth:`SerialPort.open`) to obtain an open
    port; direct instantiation is part of the test surface only.

    ``SerialPort`` itself is abstract: ``SerialPort(backend, config)``
    dispatches to :class:`_PosixSerialPort` (when ``backend`` satisfies
    :class:`SyncSerialBackend`) or :class:`_AsyncBackendSerialPort` (when it
    satisfies :class:`AsyncSerialBackend`). Both subclasses present the same
    public interface — callers never see the difference.

    Thread safety: a single :class:`SerialPort` is bound to one event loop.
    Concurrent ``receive`` calls raise :class:`anyio.BusyResourceError`; the
    same applies to ``send``. Full-duplex send+receive is always allowed.
    """

    __slots__ = (
        "_backend",
        "_close_lock",
        "_closed",
        "_config",
        "_configure_lock",
        "_port_info",
        "_receive_guard",
        "_send_guard",
    )

    _backend: SyncSerialBackend | AsyncSerialBackend

    def __new__(
        cls,
        backend: SyncSerialBackend | AsyncSerialBackend,
        config: SerialConfig,
        *,
        port_info: PortInfo | None = None,
    ) -> SerialPort:
        """Dispatch to the right subclass based on the backend's Protocol.

        Subclasses bypass dispatch (``cls is SerialPort`` is False) so that
        :func:`open_serial_port` can construct the variant directly without
        a redundant ``isinstance`` round trip. The ``isinstance`` /
        ``TypeError`` fallback below covers callers that bypass static
        typing and pass an arbitrary object — both type-checkers see the
        union as exhaustive and flag the fallback, which we silence
        because the runtime guard is intentional.
        """
        if cls is SerialPort:
            if isinstance(backend, SyncSerialBackend):
                return object.__new__(_PosixSerialPort)
            if isinstance(backend, AsyncSerialBackend):  # pyright: ignore[reportUnnecessaryIsInstance]
                return object.__new__(_AsyncBackendSerialPort)
            msg = (  # type: ignore[unreachable]
                "SerialPort requires a backend satisfying SyncSerialBackend "
                f"or AsyncSerialBackend; got {type(backend).__name__!r}"
            )
            raise TypeError(msg)
        return object.__new__(cls)

    def __init__(
        self,
        backend: SyncSerialBackend | AsyncSerialBackend,
        config: SerialConfig,
        *,
        port_info: PortInfo | None = None,
    ) -> None:
        """Wrap an already-opened backend.

        Prefer :func:`open_serial_port`. This constructor exists for test
        harnesses and any code path that needs to hand in a custom backend
        (for example, :class:`MockBackend.pair()`).

        Args:
            backend: The opened backend to drive.
            config: Active configuration; mirrored on the
                :attr:`SerialStreamAttribute.config` typed attribute.
            port_info: Optional discovery metadata for the open device.
                When ``None`` the :attr:`SerialStreamAttribute.port_info`
                typed attribute is omitted, so lookups raise
                :class:`anyio.TypedAttributeLookupError` — matching the
                AnyIO convention for unavailable attributes.
        """
        self._backend = backend
        self._config: SerialConfig = config
        self._port_info: PortInfo | None = port_info
        self._receive_guard = anyio.ResourceGuard("reading from")
        self._send_guard = anyio.ResourceGuard("writing to")
        self._configure_lock = anyio.Lock()
        self._close_lock = anyio.Lock()
        self._closed = False

    # ------------------------------------------------------------------
    # Convenience constructor
    # ------------------------------------------------------------------

    @classmethod
    async def open(
        cls,
        path: str,
        /,
        **config_fields: object,
    ) -> SerialPort:
        """Shortcut for ``await open_serial_port(path, SerialConfig(**fields))``."""
        config = SerialConfig(**config_fields)  # type: ignore[arg-type]
        return await open_serial_port(path, config)

    # ------------------------------------------------------------------
    # Public properties (shared)
    # ------------------------------------------------------------------

    @property
    def path(self) -> str:
        """Device path the backend was opened on."""
        return self._backend.path

    @property
    def is_open(self) -> bool:
        """Whether the port is usable for I/O."""
        return not self._closed and self._backend.is_open

    @property
    def config(self) -> SerialConfig:
        """Most recent :class:`SerialConfig` applied to the backend."""
        return self._config

    @property
    def capabilities(self) -> SerialCapabilities:
        """Feature-support snapshot reported by the backend."""
        return self._backend.capabilities

    @property
    def port_info(self) -> PortInfo | None:
        """Discovery metadata for the open device, or ``None`` if unresolved.

        ``None`` for pseudo terminals, devices the platform's native
        discovery backend doesn't recognise, and platforms whose backend
        hasn't landed yet. Mirrors the
        :attr:`SerialStreamAttribute.port_info` typed attribute, which is
        omitted (rather than ``None``) when this is ``None``.
        """
        return self._port_info

    # ------------------------------------------------------------------
    # AnyIO typed attributes (base = universal entries; subclasses extend)
    # ------------------------------------------------------------------

    @property
    @override
    def extra_attributes(self) -> Mapping[Any, Callable[[], Any]]:
        """Typed attributes exposed via ``port.extra(...)``.

        Subclasses extend this with backend-specific entries (e.g.,
        :attr:`FileStreamAttribute.fileno` only on POSIX).
        """
        attrs: dict[Any, Callable[[], Any]] = {
            FileStreamAttribute.path: lambda: Path(self._backend.path),
            SerialStreamAttribute.capabilities: lambda: self._backend.capabilities,
            SerialStreamAttribute.config: lambda: self._config,
        }
        port_info = self._port_info
        if port_info is not None:
            # Capture the value so the lambda doesn't reach back through
            # self after callers have moved on.
            attrs[SerialStreamAttribute.port_info] = lambda: port_info
        return attrs

    # ------------------------------------------------------------------
    # ByteStream contract — send side is shared (delegates to _send_view)
    # ------------------------------------------------------------------

    @override
    async def send(self, item: bytes) -> None:
        """Write every byte of ``item``, handling partial writes internally."""
        await self._send_view(memoryview(item))

    async def send_buffer(self, data: BytesLike) -> None:
        """Write every byte from any :pep:`688` buffer-protocol object.

        Accepts ``bytes``, ``bytearray``, ``memoryview``, ``array.array``,
        NumPy arrays, etc. Non-byte element types are cast to bytes via a
        zero-copy ``memoryview.cast('B')``.
        """
        view = memoryview(data)
        if view.itemsize != 1:
            # cast() requires a contiguous source; a memoryview of bytes is
            # always contiguous, so fall back to bytes() if the source isn't.
            view = view.cast("B") if view.contiguous else memoryview(bytes(view))
        await self._send_view(view)

    @override
    async def send_eof(self) -> None:
        """Drain pending output. Idempotent; does not close the port.

        Serial has no true half-close; :meth:`send_eof` exists for
        :class:`anyio.abc.ByteStream` compliance and simply flushes the
        kernel output queue so "I'm done sending for now" has sensible
        observable behaviour for generic AnyIO code.
        """
        if self._closed:
            return
        await self.drain()

    # ------------------------------------------------------------------
    # Async context manager + finalisation (shared)
    # ------------------------------------------------------------------

    @override
    async def __aenter__(self) -> Self:
        """Return ``self`` so ``async with`` expressions can bind the port."""
        return self

    @override
    async def __aexit__(self, *exc_info: object) -> None:
        """Close the port on exit from the ``async with`` block."""
        await self.aclose()

    def __del__(self) -> None:
        """Emit a :class:`ResourceWarning` if the port was leaked open.

        Per :doc:`DESIGN` §15: explicit :meth:`aclose` or an ``async with``
        block is required. Subclasses may override to add a best-effort
        sync close on top of this warning when their backend supports it
        (POSIX does; the async backend Protocol does not).
        """
        closed = getattr(self, "_closed", True)
        if closed:
            return
        backend = getattr(self, "_backend", None)
        if backend is None:
            return
        warnings.warn(
            f"unclosed serial port {backend.path!r}; "
            "use `async with` or call `await port.aclose()`",
            ResourceWarning,
            stacklevel=2,
        )

    # ------------------------------------------------------------------
    # Internal helpers (shared)
    # ------------------------------------------------------------------

    def _raise_if_closed(self) -> None:
        if self._closed:
            raise SerialClosedError(
                _errno.EBADF,
                "serial port is closed",
                self._backend.path,
            )

    # ------------------------------------------------------------------
    # Hot-path / control surface — overridden by each variant.
    #
    # These are not :func:`abc.abstractmethod` because :meth:`__new__`
    # already guarantees that direct instantiation always returns a
    # concrete subclass; declaring abstractness would force every test
    # site that constructs ``SerialPort(backend, cfg)`` to satisfy mypy
    # for an instantiation path that the runtime never actually takes.
    # ------------------------------------------------------------------

    @override
    async def receive(self, max_bytes: int = _DEFAULT_RECEIVE_LIMIT) -> bytes:
        """Read up to ``max_bytes`` bytes; subclasses provide the I/O path."""
        raise NotImplementedError

    async def _send_view(self, view: memoryview) -> None:
        """Write ``view`` in full; subclasses provide the I/O path."""
        raise NotImplementedError

    @override
    async def aclose(self) -> None:
        """Close the port; idempotent. Subclasses own the teardown sequence."""
        raise NotImplementedError

    async def receive_into(self, buffer: bytearray | memoryview) -> int:
        """Zero-allocation read into caller-owned ``buffer``.

        Prefer this over :meth:`receive` in tight loops that want to
        reuse a scratch buffer. Subclasses provide the I/O path.
        """
        raise NotImplementedError

    async def receive_available(self, *, limit: int | None = None) -> bytes:
        """Drain the kernel input queue in one wakeup. See subclasses."""
        raise NotImplementedError

    async def configure(self, config: SerialConfig) -> None:
        """Re-apply ``config`` to the open port. Subclass-implemented."""
        raise NotImplementedError

    async def reset_input_buffer(self) -> None:
        """Discard unread input. Subclass-implemented."""
        raise NotImplementedError

    async def reset_output_buffer(self) -> None:
        """Discard pending output. Subclass-implemented."""
        raise NotImplementedError

    async def drain(self) -> None:
        """Wait for the kernel output queue to empty. Subclass-implemented."""
        raise NotImplementedError

    async def drain_exact(self) -> None:
        """Drain including UART FIFO. Subclass-implemented."""
        raise NotImplementedError

    async def send_break(self, duration: float = 0.25) -> None:
        """Assert BREAK for ``duration`` seconds. Subclass-implemented."""
        raise NotImplementedError

    async def modem_lines(self) -> ModemLines:
        """Snapshot CTS/DSR/RI/CD. Subclass-implemented."""
        raise NotImplementedError

    async def set_control_lines(
        self,
        *,
        rts: bool | None = None,
        dtr: bool | None = None,
    ) -> None:
        """Drive RTS / DTR. Subclass-implemented."""
        raise NotImplementedError

    def input_waiting(self) -> int:
        """Bytes in the kernel input queue. Subclass-implemented."""
        raise NotImplementedError

    def output_waiting(self) -> int:
        """Bytes in the kernel output queue. Subclass-implemented."""
        raise NotImplementedError


class _PosixSerialPort(SerialPort):
    """:class:`SerialPort` variant driving a :class:`SyncSerialBackend`.

    Owns the :func:`anyio.wait_readable` / :func:`anyio.wait_writable`
    readiness loop on top of the backend's non-blocking ``os.read`` /
    ``os.write`` primitives. Used by every POSIX backend (Linux, Darwin,
    BSD, generic POSIX) and by :class:`MockBackend` in tests.
    """

    __slots__ = ()

    # Narrowed from the base annotation; the runtime invariant is enforced
    # by the dispatch in :meth:`SerialPort.__new__`. Pyright treats this
    # as an unsound override of a mutable attribute, but ``__new__`` makes
    # the narrowing safe.
    _backend: SyncSerialBackend  # pyright: ignore[reportIncompatibleVariableOverride]

    # ------------------------------------------------------------------
    # Typed attributes — adds fileno on top of the base set
    # ------------------------------------------------------------------

    @property
    @override
    def extra_attributes(self) -> Mapping[Any, Callable[[], Any]]:
        attrs = dict(super().extra_attributes)
        attrs[FileStreamAttribute.fileno] = self._backend.fileno
        return attrs

    # ------------------------------------------------------------------
    # ByteStream contract
    # ------------------------------------------------------------------

    @override
    async def receive(self, max_bytes: int = _DEFAULT_RECEIVE_LIMIT) -> bytes:
        """Read up to ``max_bytes`` bytes; returns as soon as any are available.

        Never returns ``b""`` — a clean EOF from the peer raises
        :class:`SerialDisconnectedError` (which is itself an
        :class:`anyio.BrokenResourceError`).
        """
        if max_bytes <= 0:
            msg = f"max_bytes must be positive (got {max_bytes!r})"
            raise ValueError(msg)
        with self._receive_guard:
            self._raise_if_closed()
            # Allocate exactly what the caller asked for. Callers that
            # want to avoid the per-call allocation should use
            # ``receive_into`` with a pre-allocated buffer instead.
            buffer = bytearray(max_bytes)
            fd = self._backend.fileno()
            while True:
                await anyio.wait_readable(fd)
                self._raise_if_closed()
                try:
                    count = self._backend.read_nonblocking(buffer)
                except (BlockingIOError, InterruptedError):
                    continue
                except OSError as exc:
                    raise errno_to_exception(exc, context="io", path=self._backend.path) from exc
                if count == 0:
                    raise SerialDisconnectedError(
                        _errno.EIO,
                        "device returned EOF after readiness",
                        self._backend.path,
                    )
                return bytes(buffer[:count])

    @override
    async def _send_view(self, view: memoryview) -> None:
        """Write ``view`` in full, respecting the send guard and partial writes."""
        total = len(view)
        if total == 0:
            # An empty write is a no-op but must still check closed state.
            self._raise_if_closed()
            return
        with self._send_guard:
            self._raise_if_closed()
            fd = self._backend.fileno()
            offset = 0
            while offset < total:
                await anyio.wait_writable(fd)
                self._raise_if_closed()
                try:
                    written = self._backend.write_nonblocking(view[offset:])
                except (BlockingIOError, InterruptedError):
                    continue
                except OSError as exc:
                    raise errno_to_exception(exc, context="io", path=self._backend.path) from exc
                if written <= 0:
                    continue
                offset += written

    @override
    async def aclose(self) -> None:
        """Close the port. Idempotent.

        Wakes any pending :func:`anyio.wait_readable` /
        :func:`anyio.wait_writable` via :func:`anyio.notify_closing` before
        the fd is closed, and runs the teardown inside a shielded
        :class:`anyio.CancelScope` so cancellation cannot leak an open fd.
        """
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            # DESIGN §12.3 / §15: the teardown below is shielded so a
            # cancellation of the caller can never leak an open fd. The
            # body is currently synchronous, so ruff flags the scope as
            # redundant — keep it anyway for forward-compatibility and
            # to make the invariant explicit at the call site.
            with anyio.CancelScope(shield=True):  # noqa: ASYNC100
                fd = self._backend.fileno()
                with contextlib.suppress(OSError, ValueError):
                    # notify_closing accepts any int; guard only against
                    # the already-closed race.
                    anyio.notify_closing(fd)
                self._backend.close()

    # ------------------------------------------------------------------
    # Serial-specific extensions
    # ------------------------------------------------------------------

    @override
    async def receive_into(self, buffer: bytearray | memoryview) -> int:
        """Read into caller-owned ``buffer`` without an intermediate copy.

        The zero-allocation hot path. :meth:`receive` allocates a fresh
        ``bytearray(max_bytes)`` on every call to satisfy the
        ``ByteStream`` contract's ``bytes`` return type; tight loops
        that want to reuse a scratch buffer should call this method
        instead and manage the buffer themselves.

        Returns the number of bytes written into ``buffer``. Raises
        :class:`SerialDisconnectedError` on EOF, preserving the same "never
        return zero in normal operation" invariant as :meth:`receive`.
        """
        length = len(buffer)
        if length == 0:
            msg = "buffer must be non-empty"
            raise ValueError(msg)
        with self._receive_guard:
            self._raise_if_closed()
            fd = self._backend.fileno()
            while True:
                await anyio.wait_readable(fd)
                self._raise_if_closed()
                try:
                    count = self._backend.read_nonblocking(buffer)
                except (BlockingIOError, InterruptedError):
                    continue
                except OSError as exc:
                    raise errno_to_exception(exc, context="io", path=self._backend.path) from exc
                if count == 0:
                    raise SerialDisconnectedError(
                        _errno.EIO,
                        "device returned EOF after readiness",
                        self._backend.path,
                    )
                return count

    @override
    async def receive_available(self, *, limit: int | None = None) -> bytes:
        """Wait for readiness, then return every byte the kernel has queued.

        Useful for request/response protocols where batching a single
        readiness wakeup into one syscall reduces round-trip latency.
        """
        with self._receive_guard:
            self._raise_if_closed()
            fd = self._backend.fileno()
            await anyio.wait_readable(fd)
            self._raise_if_closed()
            waiting = self._backend.input_waiting()
            size = waiting if waiting > 0 else self._config.read_chunk_size
            if limit is not None:
                size = min(size, limit)
            if size <= 0:
                return b""
            buffer = bytearray(size)
            while True:
                try:
                    count = self._backend.read_nonblocking(buffer)
                except (BlockingIOError, InterruptedError):
                    # Rare: TIOCINQ said there were bytes but the follow-up
                    # read races with another consumer. Re-park.
                    await anyio.wait_readable(fd)
                    self._raise_if_closed()
                    continue
                except OSError as exc:
                    raise errno_to_exception(exc, context="io", path=self._backend.path) from exc
                if count == 0:
                    raise SerialDisconnectedError(
                        _errno.EIO,
                        "device returned EOF after readiness",
                        self._backend.path,
                    )
                return bytes(buffer[:count])

    @override
    async def configure(self, config: SerialConfig) -> None:
        """Re-apply a new :class:`SerialConfig` to the open port."""
        async with self._configure_lock:
            self._raise_if_closed()
            try:
                self._backend.configure(config)
            except OSError as exc:
                raise errno_to_exception(exc, context="ioctl", path=self._backend.path) from exc
            self._config = config

    @override
    async def reset_input_buffer(self) -> None:
        """Discard unread bytes from the kernel input queue."""
        self._raise_if_closed()
        try:
            self._backend.reset_input_buffer()
        except OSError as exc:
            raise errno_to_exception(exc, context="ioctl", path=self._backend.path) from exc

    @override
    async def reset_output_buffer(self) -> None:
        """Discard pending bytes in the kernel output queue."""
        self._raise_if_closed()
        try:
            self._backend.reset_output_buffer()
        except OSError as exc:
            raise errno_to_exception(exc, context="ioctl", path=self._backend.path) from exc

    @override
    async def drain(self) -> None:
        """Wait for the kernel output queue to empty.

        Async reformulation of ``tcdrain``: polls ``TIOCOUTQ`` and sleeps
        for a bounded interval between probes. Does not wait for the UART
        hardware FIFO; use :meth:`drain_exact` when that matters.
        """
        with self._send_guard:
            self._raise_if_closed()
            bps = max(self._config.baudrate // 10, 1)
            while True:
                self._raise_if_closed()
                try:
                    pending = self._backend.output_waiting()
                except OSError as exc:
                    raise errno_to_exception(exc, context="ioctl", path=self._backend.path) from exc
                if pending <= 0:
                    return
                wait_s = max(pending / bps, _MIN_DRAIN_INTERVAL)
                await anyio.sleep(min(wait_s, _MAX_DRAIN_INTERVAL))

    @override
    async def drain_exact(self) -> None:
        """True ``tcdrain`` semantics — waits for the UART FIFO too.

        Blocks in a worker thread via :func:`anyio.to_thread.run_sync`. Use
        for user-space RS-485 direction switching or other cases where
        FIFO-drain timing matters.
        """
        with self._send_guard:
            self._raise_if_closed()
            await anyio.to_thread.run_sync(self._backend.tcdrain_blocking)

    @override
    async def send_break(self, duration: float = 0.25) -> None:
        """Assert a serial BREAK condition for ``duration`` seconds.

        Implemented as ``TIOCSBRK`` + :func:`anyio.sleep` + ``TIOCCBRK``
        (see :doc:`DESIGN` §12.4.1). Cancellable mid-sleep; the BREAK is
        guaranteed to be de-asserted via ``finally``.
        """
        if duration < 0:
            msg = f"duration must be non-negative (got {duration!r})"
            raise ValueError(msg)
        with self._send_guard:
            self._raise_if_closed()
            try:
                self._backend.set_break(on=True)
            except OSError as exc:
                raise errno_to_exception(exc, context="ioctl", path=self._backend.path) from exc
            try:
                await anyio.sleep(duration)
            finally:
                # Best effort: de-assert even if the port was closed
                # mid-sleep. Swallow OSError so the caller sees any
                # original cancellation or exception.
                with contextlib.suppress(OSError):
                    self._backend.set_break(on=False)

    @override
    async def modem_lines(self) -> ModemLines:
        """Snapshot the input modem-status lines (CTS/DSR/RI/CD)."""
        self._raise_if_closed()
        try:
            return self._backend.modem_lines()
        except OSError as exc:
            raise errno_to_exception(exc, context="ioctl", path=self._backend.path) from exc

    @override
    async def set_control_lines(
        self,
        *,
        rts: bool | None = None,
        dtr: bool | None = None,
    ) -> None:
        """Drive the output control lines; ``None`` leaves a line unchanged."""
        self._raise_if_closed()
        try:
            self._backend.set_control_lines(rts=rts, dtr=dtr)
        except OSError as exc:
            raise errno_to_exception(exc, context="ioctl", path=self._backend.path) from exc

    @override
    def input_waiting(self) -> int:
        """Bytes waiting in the kernel input queue right now (non-awaiting)."""
        self._raise_if_closed()
        return self._backend.input_waiting()

    @override
    def output_waiting(self) -> int:
        """Bytes waiting in the kernel output queue right now (non-awaiting)."""
        self._raise_if_closed()
        return self._backend.output_waiting()

    # ------------------------------------------------------------------
    # Finalisation — extends base warning with sync close best-effort
    # ------------------------------------------------------------------

    @override
    def __del__(self) -> None:
        super().__del__()
        backend = getattr(self, "_backend", None)
        if backend is None or getattr(self, "_closed", True):
            return
        with contextlib.suppress(Exception):
            backend.close()


class _AsyncBackendSerialPort(SerialPort):
    """:class:`SerialPort` variant driving an :class:`AsyncSerialBackend`.

    Used when the backend owns its own async I/O primitives (Windows
    overlapped I/O, network-bridged serial, etc.). The fd-readiness loop
    is bypassed entirely: every hot-path method delegates to the backend
    under the same resource guards / close-lock / configure-lock that the
    POSIX variant uses.

    This variant deliberately omits :attr:`FileStreamAttribute.fileno`
    from its typed attributes — async backends have no integer fd to
    wait on (DESIGN §24.5).
    """

    __slots__ = ()

    # Narrowed from the base; see :class:`_PosixSerialPort` for the reason
    # this is sound despite the pyright complaint.
    _backend: AsyncSerialBackend  # pyright: ignore[reportIncompatibleVariableOverride]

    # ------------------------------------------------------------------
    # ByteStream contract
    # ------------------------------------------------------------------

    @override
    async def receive(self, max_bytes: int = _DEFAULT_RECEIVE_LIMIT) -> bytes:
        if max_bytes <= 0:
            msg = f"max_bytes must be positive (got {max_bytes!r})"
            raise ValueError(msg)
        with self._receive_guard:
            self._raise_if_closed()
            data = await self._backend.receive(max_bytes)
            if not data:
                raise SerialDisconnectedError(
                    _errno.EIO,
                    "device returned EOF after readiness",
                    self._backend.path,
                )
            return data

    @override
    async def _send_view(self, view: memoryview) -> None:
        if len(view) == 0:
            self._raise_if_closed()
            return
        with self._send_guard:
            self._raise_if_closed()
            await self._backend.send(view)

    @override
    async def aclose(self) -> None:
        """Close the port. Idempotent.

        Runs the backend teardown inside a shielded :class:`anyio.CancelScope`
        so cancellation cannot leak an open handle. Backends own their own
        wakeup story for parked I/O — the AnyIO contract for
        ``ByteStream.aclose`` is that pending receives raise
        :class:`anyio.ClosedResourceError`.
        """
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            with anyio.CancelScope(shield=True):
                await self._backend.aclose()

    # ------------------------------------------------------------------
    # Serial-specific extensions
    # ------------------------------------------------------------------

    @override
    async def receive_into(self, buffer: bytearray | memoryview) -> int:
        if len(buffer) == 0:
            msg = "buffer must be non-empty"
            raise ValueError(msg)
        with self._receive_guard:
            self._raise_if_closed()
            count = await self._backend.receive_into(buffer)
            if count == 0:
                raise SerialDisconnectedError(
                    _errno.EIO,
                    "device returned EOF after readiness",
                    self._backend.path,
                )
            return count

    @override
    async def receive_available(self, *, limit: int | None = None) -> bytes:
        """Return whatever bytes the backend has queued in one round trip.

        On async backends ``input_waiting()`` is a snapshot; we use it as
        a sizing hint for the ``receive`` call so the backend can short-
        circuit if a quick read drains the kernel queue.
        """
        with self._receive_guard:
            self._raise_if_closed()
            waiting = self._backend.input_waiting()
            size = waiting if waiting > 0 else self._config.read_chunk_size
            if limit is not None:
                size = min(size, limit)
            if size <= 0:
                return b""
            data = await self._backend.receive(size)
            if not data:
                raise SerialDisconnectedError(
                    _errno.EIO,
                    "device returned EOF after readiness",
                    self._backend.path,
                )
            return data

    @override
    async def configure(self, config: SerialConfig) -> None:
        async with self._configure_lock:
            self._raise_if_closed()
            await self._backend.configure(config)
            self._config = config

    @override
    async def reset_input_buffer(self) -> None:
        self._raise_if_closed()
        await self._backend.reset_input_buffer()

    @override
    async def reset_output_buffer(self) -> None:
        self._raise_if_closed()
        await self._backend.reset_output_buffer()

    @override
    async def drain(self) -> None:
        with self._send_guard:
            self._raise_if_closed()
            await self._backend.drain()

    @override
    async def drain_exact(self) -> None:
        """Map to ``backend.drain()`` — async backends own FIFO semantics.

        On Windows this resolves to ``FlushFileBuffers`` (see
        ``docs/design-windows-backend.md`` §7); other async backends decide
        what FIFO semantics they expose under the single ``drain`` method.
        """
        with self._send_guard:
            self._raise_if_closed()
            await self._backend.drain()

    @override
    async def send_break(self, duration: float = 0.25) -> None:
        if duration < 0:
            msg = f"duration must be non-negative (got {duration!r})"
            raise ValueError(msg)
        with self._send_guard:
            self._raise_if_closed()
            # The async backend owns the timing sleep internally — see
            # AsyncSerialBackend.send_break in protocol.py and
            # design-windows-backend.md §6.1 (SetCommBreak / ClearCommBreak).
            await self._backend.send_break(duration)

    @override
    async def modem_lines(self) -> ModemLines:
        self._raise_if_closed()
        return await self._backend.modem_lines()

    @override
    async def set_control_lines(
        self,
        *,
        rts: bool | None = None,
        dtr: bool | None = None,
    ) -> None:
        self._raise_if_closed()
        await self._backend.set_control_lines(rts=rts, dtr=dtr)

    @override
    def input_waiting(self) -> int:
        self._raise_if_closed()
        return self._backend.input_waiting()

    @override
    def output_waiting(self) -> int:
        self._raise_if_closed()
        return self._backend.output_waiting()


async def open_serial_port(
    path: str,
    config: SerialConfig | None = None,
) -> SerialPort:
    """Open a serial port and return an initialised :class:`SerialPort`.

    The backend is chosen via :func:`select_backend` based on the current
    platform. Sync backends (POSIX) are opened synchronously and wrapped in
    :class:`_PosixSerialPort`; async backends (Windows) have their async
    ``open`` awaited and are wrapped in :class:`_AsyncBackendSerialPort`.
    """
    cfg = config if config is not None else SerialConfig()
    backend = select_backend(path, cfg)
    if isinstance(backend, SyncSerialBackend):
        try:
            backend.open(path, cfg)
        except OSError as exc:
            raise errno_to_exception(exc, context="open", path=path) from exc
        # Best-effort metadata enrichment; never raises.
        port_info = _resolve_port_info_for_path(path)
        return _PosixSerialPort(backend, cfg, port_info=port_info)
    # AsyncSerialBackend branch — Protocol exhaustively covers the
    # ``select_backend`` return type, so no trailing fallback is needed.
    # OSError translation is the backend's responsibility (Windows raises
    # WinError, which doesn't map cleanly through ``errno_to_exception``).
    await backend.open(path, cfg)
    port_info = _resolve_port_info_for_path(path)
    return _AsyncBackendSerialPort(backend, cfg, port_info=port_info)


class SerialConnectable(anyio.abc.ByteStreamConnectable):
    """Deferred-open factory for a serial port.

    Implements :class:`anyio.abc.ByteStreamConnectable`, so any AnyIO code
    that accepts a generic connectable can accept a ``SerialConnectable``.
    Construction does no I/O; :meth:`connect` opens the port.
    """

    __slots__ = ("config", "path")

    def __init__(self, *, path: str, config: SerialConfig | None = None) -> None:
        self.path: str = path
        self.config: SerialConfig = config if config is not None else SerialConfig()

    @override
    async def connect(self) -> SerialPort:
        """Open the port and return a fresh :class:`SerialPort`."""
        return await open_serial_port(self.path, self.config)


__all__ = [
    "SerialConnectable",
    "SerialPort",
    "open_serial_port",
]
