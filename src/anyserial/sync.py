r"""Blocking :class:`SerialPort` wrapper for scripts and test benches.

Async-first is the primary promise of :mod:`anyserial`; this module is a
pure delegation layer over the async :class:`anyserial.SerialPort` for
callers that do not want to run an event loop themselves. See
:doc:`DESIGN` §7.5 for the design and rationale.

Every sync port in the process shares a single
:class:`anyio.from_thread.BlockingPortalProvider`, which owns one
background thread running an AnyIO event loop. The provider is refcounted
internally: the event-loop thread spins up on the first open and shuts
down when the last sync port is closed. Opening multiple sync ports does
**not** spawn multiple event-loop threads.

Example:
    >>> from anyserial.sync import SerialPort
    >>> with SerialPort.open("/dev/ttyUSB0", baudrate=115200) as port:
    ...     port.send(b"ping\n")
    ...     reply = port.receive(1024, timeout=1.0)

Timeouts are optional per-call arguments on every blocking method,
implemented via :func:`anyio.fail_after` inside the portal call; the
stdlib :class:`TimeoutError` surfaces unchanged when they fire.
"""

from __future__ import annotations

import contextlib
import threading
import warnings
from typing import TYPE_CHECKING, Any, Self, overload

import anyio
import anyio.from_thread

from anyserial.config import SerialConfig
from anyserial.stream import SerialPort as _AsyncSerialPort
from anyserial.stream import open_serial_port as _async_open_serial_port

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

    from anyserial._types import BytesLike, ModemLines
    from anyserial.capabilities import SerialCapabilities
    from anyserial.discovery import PortInfo

_DEFAULT_RECEIVE_LIMIT = 65_536


# ---------------------------------------------------------------------------
# Process-wide portal configuration
# ---------------------------------------------------------------------------

_CONFIG_LOCK = threading.Lock()
_provider: anyio.from_thread.BlockingPortalProvider | None = None
_provider_backend: str = "asyncio"
_provider_options: dict[str, Any] = {}

# Sentinel for ``SerialPort.extra(..., default=...)`` — distinct from any
# user-supplied value so ``None`` remains a valid default.
_MISSING: Any = object()


def configure_portal(
    *,
    backend: str = "asyncio",
    backend_options: Mapping[str, Any] | None = None,
) -> None:
    """Override the AnyIO backend used by the process-wide sync portal.

    Must be called before the first sync :class:`SerialPort` is opened;
    otherwise a :class:`RuntimeError` is raised. Defaults are
    ``backend="asyncio"`` with no options.

    Args:
        backend: AnyIO backend name — ``"asyncio"`` or ``"trio"``.
        backend_options: Backend-specific options forwarded to
            :func:`anyio.run`. For uvloop pass
            ``{"use_uvloop": True}``.

    Raises:
        RuntimeError: If a portal has already been spawned this process.
    """
    global _provider_backend, _provider_options  # noqa: PLW0603 — module-level config singleton
    with _CONFIG_LOCK:
        if _provider is not None:
            msg = (
                "configure_portal() must be called before the first sync "
                "SerialPort is opened; portal already started"
            )
            raise RuntimeError(msg)
        _provider_backend = backend
        _provider_options = dict(backend_options) if backend_options else {}


def _get_provider() -> anyio.from_thread.BlockingPortalProvider:
    """Return the process-wide provider, constructing it on first use."""
    global _provider  # noqa: PLW0603 — lazy module-level singleton
    with _CONFIG_LOCK:
        if _provider is None:
            _provider = anyio.from_thread.BlockingPortalProvider(
                backend=_provider_backend,
                backend_options=_provider_options or None,
            )
        return _provider


def _reset_portal_for_testing() -> None:  # pyright: ignore[reportUnusedFunction]
    """Drop the cached provider so the next open rebuilds one.

    Tests only. Callers must ensure no sync ports are currently open;
    otherwise provider refcounts will diverge and the portal thread will
    leak.
    """
    global _provider  # noqa: PLW0603 — test-only reset of the singleton
    with _CONFIG_LOCK:
        _provider = None


# ---------------------------------------------------------------------------
# Sync SerialPort
# ---------------------------------------------------------------------------


class SerialPort:
    """Blocking serial-port wrapper over an async :class:`anyserial.SerialPort`.

    Use :meth:`SerialPort.open` (or the module-level
    :func:`open_serial_port`) to obtain an open port. Direct construction
    from an already-opened async port exists for tests only.

    Every blocking method accepts an optional ``timeout`` keyword; when
    provided the call is wrapped in :func:`anyio.fail_after` on the
    portal thread, raising :class:`TimeoutError` on expiry.

    Thread safety: concurrent method calls from multiple OS threads are
    serialized by the portal queue. Snapshot-style properties and
    methods (``input_waiting``, ``output_waiting``) do not dispatch to
    the portal and return an immediate value.
    """

    __slots__ = (
        "_async_port",
        "_closed",
        "_portal",
        "_portal_entered",
        "_provider",
    )

    def __init__(
        self,
        async_port: _AsyncSerialPort,
        *,
        portal: anyio.from_thread.BlockingPortal,
        provider: anyio.from_thread.BlockingPortalProvider,
    ) -> None:
        """Wrap an already-opened async port. Prefer :meth:`SerialPort.open`.

        The ``provider`` context is assumed already entered once on behalf
        of this port; :meth:`close` releases that reference.
        """
        self._async_port: _AsyncSerialPort = async_port
        self._portal: anyio.from_thread.BlockingPortal = portal
        self._provider: anyio.from_thread.BlockingPortalProvider = provider
        self._portal_entered: bool = True
        self._closed: bool = False

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def open(
        cls,
        path: str,
        config: SerialConfig | None = None,
        /,
        *,
        timeout: float | None = None,
        **config_fields: object,
    ) -> Self:
        """Open ``path`` and return a blocking :class:`SerialPort`.

        Args:
            path: Device path (for example ``"/dev/ttyUSB0"``).
            config: Fully-specified :class:`SerialConfig`. Mutually
                exclusive with ``**config_fields``.
            timeout: Maximum seconds to wait for the open to complete.
            **config_fields: Shortcut — forwarded to
                :class:`SerialConfig` to construct a fresh config.

        Raises:
            ValueError: Both ``config`` and ``**config_fields`` supplied.
            TimeoutError: ``timeout`` elapsed before open completed.
        """
        if config is not None and config_fields:
            msg = "pass either `config=...` or keyword fields, not both"
            raise ValueError(msg)
        cfg = config if config is not None else SerialConfig(**config_fields)  # type: ignore[arg-type]
        provider = _get_provider()
        portal = provider.__enter__()
        try:
            async_port = _call_with_timeout(portal, _async_open_serial_port, timeout, path, cfg)
        except BaseException:
            provider.__exit__(None, None, None)
            raise
        return cls(async_port, portal=portal, provider=provider)

    # ------------------------------------------------------------------
    # Properties — direct delegation, no portal
    # ------------------------------------------------------------------

    @property
    def path(self) -> str:
        """Device path the port was opened on."""
        return self._async_port.path

    @property
    def is_open(self) -> bool:
        """Whether the port is usable for I/O."""
        return not self._closed and self._async_port.is_open

    @property
    def config(self) -> SerialConfig:
        """Most recent :class:`SerialConfig` applied to the port."""
        return self._async_port.config

    @property
    def capabilities(self) -> SerialCapabilities:
        """Feature-support snapshot reported by the backend."""
        return self._async_port.capabilities

    @property
    def port_info(self) -> PortInfo | None:
        """Discovery metadata for the open device, or ``None``."""
        return self._async_port.port_info

    @property
    def extra_attributes(self) -> Mapping[Any, Callable[[], Any]]:
        """Typed attributes exposed via :meth:`extra`."""
        return self._async_port.extra_attributes

    @overload
    def extra[T_Attr](self, attribute: T_Attr) -> T_Attr: ...

    @overload
    def extra[T_Attr, T_Default](
        self, attribute: T_Attr, default: T_Default
    ) -> T_Attr | T_Default: ...

    def extra(self, attribute: Any, default: Any = _MISSING) -> Any:
        """Look up a typed attribute.

        Mirrors :meth:`anyio.TypedAttributeProvider.extra`. When
        ``default`` is supplied it is returned instead of raising
        :class:`anyio.TypedAttributeLookupError`.
        """
        if default is _MISSING:
            return self._async_port.extra(attribute)  # noqa: S610 — AnyIO typed-attribute lookup
        return self._async_port.extra(attribute, default)  # noqa: S610

    # ------------------------------------------------------------------
    # Snapshots — non-awaiting, no portal
    # ------------------------------------------------------------------

    def input_waiting(self) -> int:
        """Bytes waiting in the kernel input queue right now."""
        return self._async_port.input_waiting()

    def output_waiting(self) -> int:
        """Bytes waiting in the kernel output queue right now."""
        return self._async_port.output_waiting()

    # ------------------------------------------------------------------
    # Blocking I/O — dispatched via portal
    # ------------------------------------------------------------------

    def receive(
        self,
        max_bytes: int = _DEFAULT_RECEIVE_LIMIT,
        *,
        timeout: float | None = None,
    ) -> bytes:
        """Read up to ``max_bytes``; returns as soon as any are available."""
        return self._run(self._async_port.receive, max_bytes, timeout=timeout)

    def receive_into(
        self,
        buffer: bytearray | memoryview,
        *,
        timeout: float | None = None,
    ) -> int:
        """Read into ``buffer`` in place; return number of bytes written."""
        return self._run(self._async_port.receive_into, buffer, timeout=timeout)

    def receive_available(
        self,
        *,
        limit: int | None = None,
        timeout: float | None = None,
    ) -> bytes:
        """Wait for readiness, then return every queued byte."""

        async def _call() -> bytes:
            return await self._async_port.receive_available(limit=limit)

        return self._run(_call, timeout=timeout)

    def send(self, item: bytes, *, timeout: float | None = None) -> None:
        """Write every byte of ``item``, handling partial writes."""
        self._run(self._async_port.send, item, timeout=timeout)

    def send_buffer(self, data: BytesLike, *, timeout: float | None = None) -> None:
        """Write every byte from a :pep:`688` buffer-protocol object."""
        self._run(self._async_port.send_buffer, data, timeout=timeout)

    def send_eof(self, *, timeout: float | None = None) -> None:
        """Drain pending output. Idempotent; does not close the port."""
        self._run(self._async_port.send_eof, timeout=timeout)

    def configure(self, config: SerialConfig, *, timeout: float | None = None) -> None:
        """Re-apply a new :class:`SerialConfig` to the open port."""
        self._run(self._async_port.configure, config, timeout=timeout)

    def reset_input_buffer(self, *, timeout: float | None = None) -> None:
        """Discard unread bytes from the kernel input queue."""
        self._run(self._async_port.reset_input_buffer, timeout=timeout)

    def reset_output_buffer(self, *, timeout: float | None = None) -> None:
        """Discard pending bytes in the kernel output queue."""
        self._run(self._async_port.reset_output_buffer, timeout=timeout)

    def drain(self, *, timeout: float | None = None) -> None:
        """Wait for the kernel output queue to empty."""
        self._run(self._async_port.drain, timeout=timeout)

    def drain_exact(self, *, timeout: float | None = None) -> None:
        """True ``tcdrain`` semantics — waits for the UART FIFO too."""
        self._run(self._async_port.drain_exact, timeout=timeout)

    def send_break(self, duration: float = 0.25, *, timeout: float | None = None) -> None:
        """Assert a serial BREAK condition for ``duration`` seconds."""
        self._run(self._async_port.send_break, duration, timeout=timeout)

    def modem_lines(self, *, timeout: float | None = None) -> ModemLines:
        """Snapshot the input modem-status lines (CTS/DSR/RI/CD)."""
        return self._run(self._async_port.modem_lines, timeout=timeout)

    def set_control_lines(
        self,
        *,
        rts: bool | None = None,
        dtr: bool | None = None,
        timeout: float | None = None,
    ) -> None:
        """Drive the output control lines; ``None`` leaves a line unchanged."""

        async def _call() -> None:
            await self._async_port.set_control_lines(rts=rts, dtr=dtr)

        self._run(_call, timeout=timeout)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self, *, timeout: float | None = None) -> None:
        """Close the port. Idempotent.

        Releases this port's reference on the shared portal; the event-loop
        thread shuts down when the last sync port is closed.
        """
        if self._closed:
            return
        self._closed = True
        try:
            self._run(self._async_port.aclose, timeout=timeout)
        finally:
            self._release_portal()

    def _release_portal(self) -> None:
        if not self._portal_entered:
            return
        self._portal_entered = False
        with contextlib.suppress(Exception):
            self._provider.__exit__(None, None, None)

    def __enter__(self) -> Self:
        """Return ``self`` so ``with`` expressions can bind the port."""
        return self

    def __exit__(self, *exc_info: object) -> None:
        """Close the port on exit from the ``with`` block."""
        self.close()

    def __del__(self) -> None:
        """Emit :class:`ResourceWarning` if the port was leaked open."""
        if getattr(self, "_closed", True):
            return
        async_port = getattr(self, "_async_port", None)
        path = getattr(async_port, "path", "<unknown>") if async_port is not None else "<unknown>"
        warnings.warn(
            f"unclosed sync serial port {path!r}; use `with` or call `port.close()`",
            ResourceWarning,
            stacklevel=2,
        )
        # Best-effort: close on the portal if we still hold it.
        with contextlib.suppress(Exception):
            self.close()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _run[T](
        self,
        afn: Callable[..., Awaitable[T]],
        *args: Any,
        timeout: float | None = None,
    ) -> T:
        """Dispatch ``afn`` to the portal with optional ``fail_after``."""
        return _call_with_timeout(self._portal, afn, timeout, *args)


def _call_with_timeout[T](
    portal: anyio.from_thread.BlockingPortal,
    afn: Callable[..., Awaitable[T]],
    timeout: float | None,
    *args: Any,
) -> T:
    """Run ``afn(*args)`` on ``portal``, bounded by ``timeout`` if not None."""
    if timeout is None:
        return portal.call(afn, *args)

    async def _bounded() -> T:
        with anyio.fail_after(timeout):
            return await afn(*args)

    return portal.call(_bounded)


# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------


def open_serial_port(
    path: str,
    config: SerialConfig | None = None,
    *,
    timeout: float | None = None,
) -> SerialPort:
    """Open a serial port and return a blocking :class:`SerialPort`.

    Equivalent to :meth:`SerialPort.open`. The backend is chosen by the
    async :func:`anyserial.open_serial_port` based on the current
    platform.
    """
    return SerialPort.open(path, config, timeout=timeout)


class SerialConnectable:
    """Deferred-open recipe for a blocking :class:`SerialPort`.

    Mirrors :class:`anyserial.SerialConnectable` as a sync-friendly data
    object. Does **not** implement :class:`anyio.abc.ByteStreamConnectable`
    (that Protocol requires an async ``connect``); sync callers that need
    AnyIO connectable polymorphism should use the async variant.
    """

    __slots__ = ("config", "path")

    def __init__(self, *, path: str, config: SerialConfig | None = None) -> None:
        self.path: str = path
        self.config: SerialConfig = config if config is not None else SerialConfig()

    def connect(self, *, timeout: float | None = None) -> SerialPort:
        """Open the port and return a fresh :class:`SerialPort`."""
        return SerialPort.open(self.path, self.config, timeout=timeout)


__all__ = [
    "SerialConnectable",
    "SerialPort",
    "configure_portal",
    "open_serial_port",
]
