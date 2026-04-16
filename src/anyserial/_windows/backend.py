"""Windows :class:`AsyncSerialBackend` — data path + modem events.

Implements ``open / aclose / receive / receive_into / send / configure /
reset_*/ drain / send_break / modem_lines / set_control_lines /
input_waiting / output_waiting / wait_modem_event`` against the
runtime-native overlapped-I/O helpers in :mod:`anyserial._windows._trio_io`
and :mod:`anyserial._windows._asyncio_io`. Runtime detection is one-shot in
:meth:`open`; the hot path is a constant-time dispatch on a stored
flavour string.

Modem-event surface: :meth:`wait_modem_event` (``WaitCommEvent``-based
modem-line change notification, §6.4) and ``SetCommMask`` during
:meth:`open` to enable ``EV_CTS | EV_DSR | EV_RING | EV_RLSD | EV_ERR |
EV_BREAK`` events.

Cancellation contract: see ``docs/design-windows-backend.md`` §5. We never
call ``CancelIoEx`` ourselves — both runtimes do it, and double-cancellation
races are the one footgun this design exists to avoid.
"""

from __future__ import annotations

import contextlib
from ctypes import byref, c_uint32, sizeof
from typing import TYPE_CHECKING

import anyio
import anyio.to_thread

from anyserial._types import CommEvent, ModemLines
from anyserial._windows import _asyncio_io, _trio_io
from anyserial._windows import _win32 as w
from anyserial._windows._asyncio_io import HandleWrapper
from anyserial._windows._errors import winerror_to_exception
from anyserial._windows._runtime import RuntimeFlavour, detect_runtime
from anyserial._windows.capabilities import windows_capabilities
from anyserial._windows.dcb import apply_config, build_read_any_timeouts
from anyserial.exceptions import SerialClosedError, SerialDisconnectedError

if TYPE_CHECKING:
    from anyserial.capabilities import SerialCapabilities
    from anyserial.config import SerialConfig


class WindowsBackend:
    """Async serial backend for Windows.

    Holds the open ``HANDLE`` plus the detected runtime flavour. Hot-path
    methods dispatch on the flavour to either :mod:`._trio_io` (Trio's
    ``readinto_overlapped`` / ``write_overlapped``) or
    :mod:`._asyncio_io` (CPython ``_overlapped`` + ProactorEventLoop).

    Lifecycle is single-open per instance: a closed backend cannot be
    re-opened. :meth:`aclose` is idempotent and shielded against
    cancellation by the wrapping :class:`SerialPort`.
    """

    __slots__ = (
        "_capabilities",
        "_config",
        "_handle",
        "_handle_wrapper",
        "_open",
        "_path",
        "_runtime",
    )

    def __init__(self) -> None:
        self._path: str = ""
        self._handle: int = 0
        self._handle_wrapper: HandleWrapper | None = None
        self._open: bool = False
        self._runtime: RuntimeFlavour | None = None
        self._capabilities: SerialCapabilities = windows_capabilities()
        self._config: SerialConfig | None = None

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def path(self) -> str:
        return self._path

    @property
    def is_open(self) -> bool:
        return self._open

    @property
    def capabilities(self) -> SerialCapabilities:
        return self._capabilities

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def open(self, path: str, config: SerialConfig) -> None:
        """Open the COM port and apply ``config``.

        Sequence (design-windows-backend.md §3 / §6):

        1. Detect the async runtime (raises if neither asyncio Proactor
           nor Trio is active).
        2. ``CreateFileW`` with ``FILE_FLAG_OVERLAPPED`` and
           ``dwShareMode=0`` (always exclusive on Windows).
        3. ``SetupComm`` to size the kernel queues.
        4. ``GetCommState`` → overlay config → ``SetCommState`` (§6.2.1).
        5. ``SetCommTimeouts`` with "wait-for-any" policy (§6.3).
        6. Register the handle with the runtime's IOCP.
        """
        if self._open:
            msg = f"WindowsBackend already open on {self._path!r}"
            raise RuntimeError(msg)
        self._path = path
        self._runtime = detect_runtime()

        kernel32 = w.load_kernel32()
        win_path = w.normalise_com_path(path)
        try:
            handle = kernel32.CreateFileW(
                win_path,
                w.GENERIC_READ | w.GENERIC_WRITE,
                0,  # dwShareMode = 0 → exclusive
                None,
                w.OPEN_EXISTING,
                w.FILE_FLAG_OVERLAPPED,
                None,
            )
        except OSError as exc:
            raise winerror_to_exception(exc, context="open", path=path) from exc

        self._handle = handle
        try:
            kernel32.SetupComm(handle, w.DEFAULT_INPUT_QUEUE, w.DEFAULT_OUTPUT_QUEUE)
            self._apply_dcb(config)
            kernel32.SetCommTimeouts(handle, byref(build_read_any_timeouts()))
        except OSError as exc:
            with contextlib.suppress(OSError):
                kernel32.CloseHandle(handle)
            self._handle = 0
            self._open = False
            raise winerror_to_exception(exc, context="open", path=path) from exc

        # Register with the runtime's IOCP. Trio: register_with_iocp
        # accepts a raw int. asyncio: proactor._register_with_iocp
        # requires an object with .fileno() (design §3).
        if self._runtime == "trio":
            await _trio_io.register(handle)
        else:
            self._handle_wrapper = HandleWrapper(handle)
            await _asyncio_io.register(self._handle_wrapper)

        # Enable modem-line and error event notifications (§6.4).
        # EV_RXCHAR is deliberately excluded — we do not use comm events
        # for data-path readiness. SetCommMask(handle, 0) in aclose()
        # wakes any pending WaitCommEvent cleanly.
        kernel32.SetCommMask(handle, w.EV_ALL_MODEM)

        self._config = config
        self._open = True

    async def aclose(self) -> None:
        """Close the port. Idempotent.

        Sequence (§5):

        1. ``SetCommMask(handle, 0)`` — wake any pending ``WaitCommEvent``
           cleanly.
        2. ``PurgeComm(PURGE_RX|TX|ABORT)`` — cancel in-flight I/O.
        3. ``CloseHandle`` — final teardown.

        Each step is best-effort: we suppress ``OSError`` so a half-broken
        device cannot leak the handle. Trio's ``register_with_iocp`` has
        no deregister call; the handle is dissociated when closed.
        """
        if not self._open:
            return
        self._open = False
        handle = self._handle
        self._handle = 0
        self._handle_wrapper = None
        kernel32 = w.load_kernel32()
        with contextlib.suppress(OSError):
            kernel32.SetCommMask(handle, 0)
        with contextlib.suppress(OSError):
            kernel32.PurgeComm(
                handle,
                w.PURGE_RXABORT | w.PURGE_TXABORT | w.PURGE_RXCLEAR | w.PURGE_TXCLEAR,
            )
        with contextlib.suppress(OSError):
            kernel32.CloseHandle(handle)

    # ------------------------------------------------------------------
    # Hot path
    # ------------------------------------------------------------------

    async def receive(self, max_bytes: int) -> bytes:
        """Read up to ``max_bytes`` bytes via overlapped I/O.

        Loops internally on zero-byte completions per
        design-windows-backend.md §6.3: with the ``MAXDWORD / MAXDWORD / 1``
        "wait-for-any" ``COMMTIMEOUTS`` triple, the kernel completes the
        overlapped read after ~1 ms with zero bytes when no data has
        arrived. Those empty completions are an artifact of the timeout
        policy, not an EOF signal — serial HANDLEs surface real
        disconnects as ``ERROR_DEVICE_REMOVED`` / ``ERROR_GEN_FAILURE``
        via ``OSError`` instead. We reissue the read so the caller never
        sees an empty completion; cancellation is honoured because both
        dispatch paths checkpoint on every iteration.
        """
        self._raise_if_closed()
        buffer = bytearray(max_bytes)
        count = await self._readinto_until_data(buffer)
        return bytes(buffer[:count])

    async def receive_into(self, buffer: bytearray | memoryview) -> int:
        """Zero-copy read into ``buffer``.

        ``ReadFileInto`` (asyncio) and ``readinto_overlapped`` (Trio) both
        write through the buffer protocol directly into the caller's
        memory — no intermediate copy. The zero-byte retry contract matches
        :meth:`receive`; see that method's docstring for rationale.
        """
        self._raise_if_closed()
        return await self._readinto_until_data(buffer)

    async def _readinto_until_data(self, buffer: bytearray | memoryview) -> int:
        """Dispatch the overlapped read, retrying on empty completions.

        Returns once the kernel delivers at least one byte. Errors from
        either runtime surface as :class:`OSError` subclasses and are
        translated to the domain exception hierarchy via
        :func:`winerror_to_exception`.
        """
        while True:
            try:
                count = await self._dispatch_readinto(buffer)
            except OSError as exc:
                raise winerror_to_exception(exc, context="io", path=self._path) from exc
            if count > 0:
                return count
            # count == 0: 1 ms wait-for-any timeout expired with no data
            # on the wire. Reissue. Both dispatch paths above already
            # checkpoint; this keeps an idle port quietly polling at
            # ~1 kHz without fighting AnyIO cancellation.

    async def send(self, data: memoryview) -> None:
        """Write all of ``data``, looping over short writes."""
        self._raise_if_closed()
        offset = 0
        total = len(data)
        while offset < total:
            try:
                written = await self._dispatch_write(data[offset:])
            except OSError as exc:
                raise winerror_to_exception(exc, context="io", path=self._path) from exc
            if written <= 0:
                # Shouldn't happen on a healthy port; treat as disconnect
                # so the caller doesn't spin forever.
                raise SerialDisconnectedError(0, "WriteFile returned 0 bytes", self._path)
            offset += written

    async def _dispatch_readinto(self, buffer: bytearray | memoryview) -> int:
        if self._runtime == "trio":
            return await _trio_io.readinto(self._handle, buffer)
        assert self._handle_wrapper is not None  # set during open() for asyncio
        return await _asyncio_io.readinto(self._handle, self._handle_wrapper, buffer)

    async def _dispatch_write(self, data: bytes | memoryview) -> int:
        if self._runtime == "trio":
            return await _trio_io.write(self._handle, data)
        assert self._handle_wrapper is not None  # set during open() for asyncio
        return await _asyncio_io.write(self._handle, self._handle_wrapper, data)

    # ------------------------------------------------------------------
    # Control plane
    # ------------------------------------------------------------------

    async def configure(self, config: SerialConfig) -> None:
        """Re-apply ``config`` to the open port via ``GetCommState`` → ``SetCommState``."""
        self._raise_if_closed()
        try:
            self._apply_dcb(config)
        except OSError as exc:
            raise winerror_to_exception(exc, context="ioctl", path=self._path) from exc
        self._config = config

    def _apply_dcb(self, config: SerialConfig) -> None:
        """Read current DCB, overlay config, write back (§6.2.1).

        ``GetCommState`` preserves driver-specific state in reserved/padding
        bytes; :func:`apply_config` deterministically sets every documented
        field.
        """
        kernel32 = w.load_kernel32()
        dcb = w.DCB()
        dcb.DCBlength = sizeof(w.DCB)
        kernel32.GetCommState(self._handle, byref(dcb))
        apply_config(dcb, config)
        kernel32.SetCommState(self._handle, byref(dcb))

    async def reset_input_buffer(self) -> None:
        """Discard unread bytes via ``PurgeComm(PURGE_RXCLEAR | PURGE_RXABORT)``."""
        self._raise_if_closed()
        kernel32 = w.load_kernel32()
        try:
            kernel32.PurgeComm(self._handle, w.PURGE_RXCLEAR | w.PURGE_RXABORT)
        except OSError as exc:
            raise winerror_to_exception(exc, context="ioctl", path=self._path) from exc

    async def reset_output_buffer(self) -> None:
        """Discard pending output via ``PurgeComm(PURGE_TXCLEAR | PURGE_TXABORT)``."""
        self._raise_if_closed()
        kernel32 = w.load_kernel32()
        try:
            kernel32.PurgeComm(self._handle, w.PURGE_TXCLEAR | w.PURGE_TXABORT)
        except OSError as exc:
            raise winerror_to_exception(exc, context="ioctl", path=self._path) from exc

    async def drain(self) -> None:
        """Block until the kernel output queue is empty (``FlushFileBuffers``).

        ``FlushFileBuffers`` is synchronous and may block briefly; we run
        it in a worker thread so it cannot stall the event loop.
        """
        self._raise_if_closed()
        kernel32 = w.load_kernel32()

        def _flush() -> None:
            try:
                kernel32.FlushFileBuffers(self._handle)
            except OSError as exc:
                raise winerror_to_exception(exc, context="io", path=self._path) from exc

        await anyio.to_thread.run_sync(_flush)

    async def send_break(self, duration: float) -> None:
        """Assert BREAK for ``duration`` seconds.

        ``SetCommBreak`` + :func:`anyio.sleep` + ``ClearCommBreak``. The
        sleep is cancellable; the BREAK is de-asserted via ``finally`` so
        cancellation cannot leave the line stuck.
        """
        self._raise_if_closed()
        kernel32 = w.load_kernel32()
        try:
            kernel32.SetCommBreak(self._handle)
        except OSError as exc:
            raise winerror_to_exception(exc, context="ioctl", path=self._path) from exc
        try:
            await anyio.sleep(duration)
        finally:
            with contextlib.suppress(OSError):
                kernel32.ClearCommBreak(self._handle)

    async def modem_lines(self) -> ModemLines:
        """Snapshot CTS/DSR/RI/CD via ``GetCommModemStatus``."""
        self._raise_if_closed()
        kernel32 = w.load_kernel32()
        status = c_uint32(0)
        try:
            kernel32.GetCommModemStatus(self._handle, byref(status))
        except OSError as exc:
            raise winerror_to_exception(exc, context="ioctl", path=self._path) from exc
        return ModemLines(
            cts=bool(status.value & w.MS_CTS_ON),
            dsr=bool(status.value & w.MS_DSR_ON),
            ri=bool(status.value & w.MS_RING_ON),
            cd=bool(status.value & w.MS_RLSD_ON),
        )

    async def set_control_lines(
        self,
        *,
        rts: bool | None = None,
        dtr: bool | None = None,
    ) -> None:
        """Drive RTS / DTR via ``EscapeCommFunction``."""
        self._raise_if_closed()
        kernel32 = w.load_kernel32()
        try:
            if rts is not None:
                kernel32.EscapeCommFunction(self._handle, w.SETRTS if rts else w.CLRRTS)
            if dtr is not None:
                kernel32.EscapeCommFunction(self._handle, w.SETDTR if dtr else w.CLRDTR)
        except OSError as exc:
            raise winerror_to_exception(exc, context="ioctl", path=self._path) from exc

    async def wait_modem_event(self) -> CommEvent:
        """Block until a modem-line change or error event fires.

        Issues ``WaitCommEvent`` via the runtime's overlapped-I/O machinery
        and returns a :class:`CommEvent` describing which lines changed.
        Cancellation is automatic — both runtimes call ``CancelIoEx`` and
        await real completion before raising.

        Shutdown: :meth:`aclose` calls ``SetCommMask(handle, 0)`` which
        wakes any pending ``WaitCommEvent`` with an empty mask (or
        ``ERROR_OPERATION_ABORTED``); the caller sees a zero-event
        :class:`CommEvent` or the cancellation exception, both of which
        are clean shutdown signals.
        """
        self._raise_if_closed()
        try:
            mask = await self._dispatch_wait_comm_event()
        except OSError as exc:
            raise winerror_to_exception(exc, context="ioctl", path=self._path) from exc
        return CommEvent(
            cts_changed=bool(mask & w.EV_CTS),
            dsr_changed=bool(mask & w.EV_DSR),
            rlsd_changed=bool(mask & w.EV_RLSD),
            ring=bool(mask & w.EV_RING),
            error=bool(mask & w.EV_ERR),
            break_received=bool(mask & w.EV_BREAK),
        )

    async def _dispatch_wait_comm_event(self) -> int:
        if self._runtime == "trio":
            return await _trio_io.wait_comm_event(self._handle)
        return await _asyncio_io.wait_comm_event(self._handle)

    def input_waiting(self) -> int:
        """Bytes in the kernel input queue (``ClearCommError`` → ``COMSTAT.cbInQue``)."""
        return int(self._comstat().cbInQue)

    def output_waiting(self) -> int:
        """Bytes in the kernel output queue (``COMSTAT.cbOutQue``)."""
        return int(self._comstat().cbOutQue)

    def _comstat(self) -> w.COMSTAT:
        self._raise_if_closed()
        kernel32 = w.load_kernel32()
        errors = c_uint32(0)
        stat = w.COMSTAT()
        try:
            kernel32.ClearCommError(self._handle, byref(errors), byref(stat))
        except OSError as exc:
            raise winerror_to_exception(exc, context="ioctl", path=self._path) from exc
        return stat

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _raise_if_closed(self) -> None:
        if not self._open:
            raise SerialClosedError(0, "WindowsBackend is closed", self._path)


__all__ = ["WindowsBackend"]
