"""Hardware benchmark against a live Alicat MFC.

Three scenarios:

1. **Poll round-trip** — send ``A\\r``, read until the reply CR arrives.
   The real-world latency that matters for closed-loop hardware control;
   dominated by USB-serial IRP turnaround and device processing.
2. **Cancellation / timeout latency** — schedule a cancel with a known
   deadline against a ``receive()`` that will never complete, measure
   overshoot past the deadline. For pyserial (sync) there's no real
   cancellation path; its row reports timeout-overshoot instead.
3. **Streaming frame interval** — inter-frame time at the application
   layer while the device streams at its default 50 ms cadence.

Runs across the anyserial backend matrix (asyncio / asyncio+uvloop / trio
/ sync wrapper) and, where applicable, head-to-head against ``pyserial``
(sync) and ``pyserial-asyncio`` on the same hardware.

Leaves the device in streaming mode (how we found it).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import pty
import statistics
import sys
import termios
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import anyio
import anyio.from_thread
import serial  # type: ignore[import-untyped]
import serial_asyncio  # type: ignore[import-untyped]
from anyio.streams.buffered import BufferedByteReceiveStream

from anyserial import SerialConfig, open_serial_port
from anyserial.sync import SerialPort as SyncSerialPort
from anyserial.sync import (
    _reset_portal_for_testing,  # pyright: ignore[reportPrivateUsage]
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from anyserial.stream import SerialPort as AsyncSerialPort

PORT = "/dev/ttyUSB0"
BAUD = 115_200
UNIT = "A"
POLL_CMD = b"A\r"

ROUND_TRIP_ITERS = 500
WARMUP_ITERS = 20
STREAM_FRAMES = 200
CANCEL_ITERS = 200
CANCEL_DEADLINE_S = 0.010  # 10 ms — well above floor, well below stream cadence


# ---------- anyserial async helpers -------------------------------------


async def _poll_roundtrip(port: AsyncSerialPort) -> int:
    await port.send(POLL_CMD)
    n = 0
    while True:
        chunk = await port.receive(128)
        n += len(chunk)
        if b"\r" in chunk:
            return n


async def _drain(port: AsyncSerialPort, seconds: float) -> None:
    with anyio.move_on_after(seconds):
        while True:
            await port.receive(1024)


async def _open_and_prep(port_path: str) -> AsyncSerialPort:
    cfg = SerialConfig(baudrate=BAUD)
    port = await open_serial_port(port_path, cfg)
    await _drain(port, 0.2)
    await port.send(f"@@ {UNIT}\r".encode("ascii"))
    await anyio.sleep(0.3)
    await _drain(port, 0.3)
    return port


async def _read_until_cr(port: AsyncSerialPort) -> None:
    while True:
        chunk = await port.receive(128)
        if b"\r" in chunk:
            return


# ---------- stats -------------------------------------------------------


@dataclass
class Stats:
    label: str
    n: int
    min_us: float
    p50_us: float
    p90_us: float
    p99_us: float
    max_us: float
    mean_us: float
    stdev_us: float

    @classmethod
    def from_samples(cls, label: str, samples_ns: list[int]) -> Stats:
        us = [s / 1000 for s in samples_ns]
        us_sorted = sorted(us)
        n = len(us_sorted)

        def pct(p: float) -> float:
            if not us_sorted:
                return 0.0
            k = max(0, min(n - 1, round(p / 100.0 * (n - 1))))
            return us_sorted[k]

        return cls(
            label=label,
            n=n,
            min_us=us_sorted[0],
            p50_us=pct(50),
            p90_us=pct(90),
            p99_us=pct(99),
            max_us=us_sorted[-1],
            mean_us=statistics.fmean(us),
            stdev_us=statistics.stdev(us) if n > 1 else 0.0,
        )


def _print_table(rows: list[Stats], title: str) -> None:
    print(f"\n=== {title} ===")
    hdr = (
        f"{'backend':<22} {'n':>5}  "
        f"{'min':>9} {'p50':>9} {'p90':>9} {'p99':>9} {'max':>10} "
        f"{'mean':>9} {'stdev':>9}"
    )
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        print(
            f"{r.label:<22} {r.n:>5}  "
            f"{r.min_us:>8.1f}µs {r.p50_us:>8.1f}µs {r.p90_us:>8.1f}µs "
            f"{r.p99_us:>8.1f}µs {r.max_us:>9.1f}µs "
            f"{r.mean_us:>8.1f}µs {r.stdev_us:>8.1f}µs"
        )


# ---------- portal helper -----------------------------------------------


@contextlib.contextmanager
def _portal(
    backend: str,
    options: dict[str, object],
) -> Iterator[anyio.from_thread.BlockingPortal]:
    with anyio.from_thread.start_blocking_portal(
        backend,
        backend_options=options,
    ) as portal:
        yield portal


# ---------- round-trip: anyserial async ---------------------------------


def _bench_roundtrip_any_async(
    label: str,
    backend: str,
    options: dict[str, object],
) -> Stats:
    with _portal(backend, options) as portal:
        port = portal.call(_open_and_prep, PORT)
        try:
            for _ in range(WARMUP_ITERS):
                portal.call(_poll_roundtrip, port)
            samples: list[int] = []
            for _ in range(ROUND_TRIP_ITERS):
                t0 = time.perf_counter_ns()
                portal.call(_poll_roundtrip, port)
                samples.append(time.perf_counter_ns() - t0)
        finally:
            portal.call(port.aclose)
    return Stats.from_samples(label, samples)


# ---------- round-trip: anyserial async, *no* portal --------------------
#
# The portal-based rows above pay one thread hop per iteration. These rows
# run the whole benchmark inside a single coroutine via ``anyio.run``, so
# the only Python-level overhead on the timed path is the library itself.


async def _no_portal_roundtrip_coro(iters: int) -> list[int]:
    port = await _open_and_prep(PORT)
    try:
        for _ in range(WARMUP_ITERS):
            await _poll_roundtrip(port)
        samples: list[int] = []
        for _ in range(iters):
            t0 = time.perf_counter_ns()
            await _poll_roundtrip(port)
            samples.append(time.perf_counter_ns() - t0)
    finally:
        await port.aclose()
    return samples


def _bench_roundtrip_no_portal(
    label: str,
    backend: str,
    options: dict[str, object],
) -> Stats:
    samples = anyio.run(
        _no_portal_roundtrip_coro,
        ROUND_TRIP_ITERS,
        backend=backend,
        backend_options=options,
    )
    return Stats.from_samples(label, samples)


# ---------- round-trip: anyserial async + BufferedByteReceiveStream ------
#
# `BufferedByteReceiveStream.receive_until(b"\r", N)` reads into an internal
# buffer once per delimiter hit, so a line-framed protocol pays one await
# per frame even when the kernel chunks the USB data.


async def _poll_roundtrip_buffered(
    port: AsyncSerialPort,
    buffered: BufferedByteReceiveStream,
) -> None:
    await port.send(POLL_CMD)
    await buffered.receive_until(b"\r", 256)


async def _buffered_roundtrip_coro(iters: int) -> list[int]:
    port = await _open_and_prep(PORT)
    buffered = BufferedByteReceiveStream(port)
    try:
        for _ in range(WARMUP_ITERS):
            await _poll_roundtrip_buffered(port, buffered)
        samples: list[int] = []
        for _ in range(iters):
            t0 = time.perf_counter_ns()
            await _poll_roundtrip_buffered(port, buffered)
            samples.append(time.perf_counter_ns() - t0)
    finally:
        await port.aclose()
    return samples


def _bench_roundtrip_buffered(
    label: str,
    backend: str,
    options: dict[str, object],
) -> Stats:
    samples = anyio.run(
        _buffered_roundtrip_coro,
        ROUND_TRIP_ITERS,
        backend=backend,
        backend_options=options,
    )
    return Stats.from_samples(label, samples)


# ---------- round-trip: anyserial sync ----------------------------------


@contextlib.contextmanager
def _quiet_sync(port: SyncSerialPort, seconds: float) -> Iterator[None]:
    deadline = time.monotonic() + seconds
    try:
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                port.receive(1024, timeout=remaining)
            except TimeoutError:
                break
    finally:
        yield


def _bench_roundtrip_any_sync() -> Stats:
    cfg = SerialConfig(baudrate=BAUD)
    port = SyncSerialPort.open(PORT, cfg)
    try:
        with _quiet_sync(port, 0.2):
            pass
        port.send(f"@@ {UNIT}\r".encode("ascii"))
        time.sleep(0.3)
        with _quiet_sync(port, 0.3):
            pass

        def _one() -> None:
            port.send(POLL_CMD)
            while True:
                chunk = port.receive(128, timeout=1.0)
                if b"\r" in chunk:
                    return

        for _ in range(WARMUP_ITERS):
            _one()
        samples: list[int] = []
        for _ in range(ROUND_TRIP_ITERS):
            t0 = time.perf_counter_ns()
            _one()
            samples.append(time.perf_counter_ns() - t0)
    finally:
        port.close()
        _reset_portal_for_testing()
    return Stats.from_samples("anyserial sync", samples)


# ---------- round-trip: pyserial (sync) ---------------------------------


def _pyserial_drain(s: serial.Serial, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    s.timeout = 0.05
    while time.monotonic() < deadline:
        if not s.read(4096):
            break


def _bench_roundtrip_pyserial() -> Stats:
    s = serial.Serial(PORT, BAUD, timeout=1.0)
    try:
        _pyserial_drain(s, 0.2)
        s.write(f"@@ {UNIT}\r".encode("ascii"))
        time.sleep(0.3)
        _pyserial_drain(s, 0.3)
        # Restore the longer read timeout for the measured loop.
        s.timeout = 1.0

        def _one() -> None:
            s.write(POLL_CMD)
            # read_until reads byte-by-byte internally up to terminator or
            # timeout; fine for a ~50-byte frame.
            reply = s.read_until(b"\r")
            if not reply.endswith(b"\r"):
                raise RuntimeError(f"pyserial read_until truncated: {reply!r}")

        for _ in range(WARMUP_ITERS):
            _one()
        samples: list[int] = []
        for _ in range(ROUND_TRIP_ITERS):
            t0 = time.perf_counter_ns()
            _one()
            samples.append(time.perf_counter_ns() - t0)
    finally:
        s.close()
    return Stats.from_samples("pyserial (sync)", samples)


# ---------- round-trip: pyserial-asyncio --------------------------------


async def _ps_async_open() -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    # pyserial-asyncio ships partial type info on open_serial_connection.
    reader, writer = await serial_asyncio.open_serial_connection(  # pyright: ignore[reportUnknownMemberType]
        url=PORT,
        baudrate=BAUD,
    )
    return reader, writer


async def _ps_async_drain(reader: asyncio.StreamReader, seconds: float) -> None:
    try:
        await asyncio.wait_for(reader.read(4096), timeout=seconds)
    except TimeoutError:
        return


async def _ps_async_prep(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    await _ps_async_drain(reader, 0.2)
    writer.write(f"@@ {UNIT}\r".encode("ascii"))
    await writer.drain()
    await asyncio.sleep(0.3)
    await _ps_async_drain(reader, 0.3)


async def _ps_async_roundtrip(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    writer.write(POLL_CMD)
    await writer.drain()
    await reader.readuntil(b"\r")


def _bench_roundtrip_pyserial_asyncio() -> Stats:
    # pyserial-asyncio is asyncio-only, so a plain portal on asyncio.
    with _portal("asyncio", {"use_uvloop": False}) as portal:
        reader, writer = portal.call(_ps_async_open)
        try:
            portal.call(_ps_async_prep, reader, writer)
            for _ in range(WARMUP_ITERS):
                portal.call(_ps_async_roundtrip, reader, writer)
            samples: list[int] = []
            for _ in range(ROUND_TRIP_ITERS):
                t0 = time.perf_counter_ns()
                portal.call(_ps_async_roundtrip, reader, writer)
                samples.append(time.perf_counter_ns() - t0)
        finally:

            async def _close() -> None:
                writer.close()
                # pyserial-asyncio's close path can race on some versions;
                # swallow and move on.
                with contextlib.suppress(Exception):
                    await writer.wait_closed()

            portal.call(_close)
    return Stats.from_samples("pyserial-asyncio", samples)


# ---------- cancellation / timeout latency ------------------------------


async def _cancel_once_any(port: AsyncSerialPort, deadline_s: float) -> int:
    """Issue receive() that won't complete, cancel via move_on_after."""
    t0 = time.perf_counter_ns()
    with anyio.move_on_after(deadline_s):
        await port.receive(1024)
    return time.perf_counter_ns() - t0 - int(deadline_s * 1e9)


def _bench_cancel_any_async(
    label: str,
    backend: str,
    options: dict[str, object],
) -> Stats:
    with _portal(backend, options) as portal:
        port = portal.call(_open_and_prep, PORT)
        try:
            # Make sure nothing is pending.
            portal.call(_drain, port, 0.2)
            # Warm-up.
            for _ in range(5):
                portal.call(_cancel_once_any, port, CANCEL_DEADLINE_S)
            samples: list[int] = [
                portal.call(_cancel_once_any, port, CANCEL_DEADLINE_S) for _ in range(CANCEL_ITERS)
            ]
        finally:
            portal.call(port.aclose)
    return Stats.from_samples(label, samples)


def _bench_cancel_any_sync() -> Stats:
    """`anyserial.sync.SerialPort.receive(timeout=...)` — not true cancel,
    but the user-visible analogue: deadline on an otherwise-blocking call.
    """
    cfg = SerialConfig(baudrate=BAUD)
    port = SyncSerialPort.open(PORT, cfg)
    try:
        with _quiet_sync(port, 0.2):
            pass
        port.send(f"@@ {UNIT}\r".encode("ascii"))
        time.sleep(0.3)
        with _quiet_sync(port, 0.3):
            pass

        def _one() -> int:
            t0 = time.perf_counter_ns()
            with contextlib.suppress(TimeoutError):
                port.receive(1024, timeout=CANCEL_DEADLINE_S)
            return time.perf_counter_ns() - t0 - int(CANCEL_DEADLINE_S * 1e9)

        for _ in range(5):
            _one()
        samples = [_one() for _ in range(CANCEL_ITERS)]
    finally:
        port.close()
        _reset_portal_for_testing()
    return Stats.from_samples("anyserial sync", samples)


async def _ps_async_cancel_once(reader: asyncio.StreamReader) -> int:
    t0 = time.perf_counter_ns()
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(reader.read(1024), timeout=CANCEL_DEADLINE_S)
    return time.perf_counter_ns() - t0 - int(CANCEL_DEADLINE_S * 1e9)


def _bench_cancel_pyserial_asyncio() -> Stats:
    with _portal("asyncio", {"use_uvloop": False}) as portal:
        reader, writer = portal.call(_ps_async_open)
        try:
            portal.call(_ps_async_prep, reader, writer)
            for _ in range(5):
                portal.call(_ps_async_cancel_once, reader)
            samples: list[int] = [
                portal.call(_ps_async_cancel_once, reader) for _ in range(CANCEL_ITERS)
            ]
        finally:

            async def _close() -> None:
                writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()

            portal.call(_close)
    return Stats.from_samples("pyserial-asyncio", samples)


def _bench_cancel_pyserial() -> Stats:
    """pyserial's `Serial(timeout=X).read()` returns early on timeout.

    It's a blocking read with a deadline, not cancellation — included to
    show the mechanism/overshoot behaviour of the same user pattern.
    """
    s = serial.Serial(PORT, BAUD, timeout=CANCEL_DEADLINE_S)
    try:
        _pyserial_drain(s, 0.2)
        s.write(f"@@ {UNIT}\r".encode("ascii"))
        time.sleep(0.3)
        _pyserial_drain(s, 0.3)
        s.timeout = CANCEL_DEADLINE_S

        def _one() -> int:
            t0 = time.perf_counter_ns()
            s.read(1024)  # returns empty after timeout
            return time.perf_counter_ns() - t0 - int(CANCEL_DEADLINE_S * 1e9)

        for _ in range(5):
            _one()
        samples = [_one() for _ in range(CANCEL_ITERS)]
    finally:
        s.close()
    return Stats.from_samples("pyserial (sync)", samples)


# ---------- streaming scenario ------------------------------------------


async def _stream_intervals(port: AsyncSerialPort, frames: int) -> list[int]:
    await port.send(f"{UNIT}@ @\r".encode("ascii"))
    await anyio.sleep(0.2)
    await _read_until_cr(port)

    intervals: list[int] = []
    prev: int | None = None
    for _ in range(frames):
        await _read_until_cr(port)
        now = time.perf_counter_ns()
        if prev is not None:
            intervals.append(now - prev)
        prev = now

    await _drain(port, 0.1)
    await port.send(f"@@ {UNIT}\r".encode("ascii"))
    await anyio.sleep(0.3)
    await _drain(port, 0.3)
    return intervals


def _bench_stream(
    label: str,
    backend: str,
    options: dict[str, object],
) -> Stats:
    with _portal(backend, options) as portal:
        port = portal.call(_open_and_prep, PORT)
        try:
            intervals = portal.call(_stream_intervals, port, STREAM_FRAMES)
        finally:
            portal.call(port.aclose)
    return Stats.from_samples(label, intervals)


# ---------- scenario: fan-out scaling -----------------------------------
#
# Where anyserial earns its weight: one event loop, N concurrent ports,
# no thread-per-port. With a single physical Alicat we can't actually
# saturate N real links, so we fake the peers with N pty pairs, each
# with a blocking echo-bot thread that echoes a fixed data frame on
# every poll. The *measurement* is about how the two libraries scale
# with concurrent ports, not absolute latency (pty has no baud-rate
# simulation — reads/writes are immediate). The scaling law still
# shows the real architectural difference.

FAN_OUT_POLLS = 50
FAN_OUT_SIZES = [1, 4, 16]
FAKE_FRAME = b" +014.63 +023.47 +000.00 +000.00 +000.00     N2\r"


def _apply_raw_pty(fd: int) -> None:
    attrs = termios.tcgetattr(fd)
    attrs[0] &= ~(
        termios.IGNBRK
        | termios.BRKINT
        | termios.PARMRK
        | termios.ISTRIP
        | termios.INLCR
        | termios.IGNCR
        | termios.ICRNL
        | termios.IXON
    )
    attrs[1] &= ~termios.OPOST
    attrs[3] &= ~(termios.ECHO | termios.ECHONL | termios.ICANON | termios.ISIG | termios.IEXTEN)
    attrs[2] = (attrs[2] & ~(termios.CSIZE | termios.PARENB)) | termios.CS8
    termios.tcsetattr(fd, termios.TCSANOW, attrs)


@contextlib.contextmanager
def _pty_fleet(n: int) -> Iterator[list[str]]:
    """Spin up ``n`` pty pairs with echo-bot threads.

    Yields the follower paths. The echo-bot blocks on ``os.read`` on the
    controller side and writes ``FAKE_FRAME`` whenever it sees a CR —
    same wire protocol as the real Alicat for poll → data frame.
    """
    # (controller_fd, follower_fd, stop_event, bot_thread). The follower
    # fd is held open for the fleet's lifetime — closing it makes the
    # master get EIO on reads until the next slave reopen, which kills
    # the echo-bot thread before anyserial has opened the port.
    created: list[tuple[int, int, threading.Event, threading.Thread]] = []
    paths: list[str] = []

    def _bot(fd: int, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                data = os.read(fd, 64)
            except OSError:
                return
            if not data:
                return
            if b"\r" in data:
                try:
                    os.write(fd, FAKE_FRAME)
                except OSError:
                    return

    try:
        for _ in range(n):
            controller, follower = pty.openpty()
            path = os.ttyname(follower)
            _apply_raw_pty(follower)
            # Intentionally NOT closing the follower here — see the note
            # on the `created` list above. anyserial will open its own
            # second fd on this same slave via `path`.
            stop = threading.Event()
            t = threading.Thread(target=_bot, args=(controller, stop), daemon=True)
            t.start()
            created.append((controller, follower, stop, t))
            paths.append(path)
        yield paths
    finally:
        for ctrl, foll, stop, t in created:
            stop.set()
            # Close wakes the bot's blocking read with OSError.
            with contextlib.suppress(OSError):
                os.close(ctrl)
            with contextlib.suppress(OSError):
                os.close(foll)
            t.join(timeout=1.0)


async def _fanout_one_anyserial(path: str, polls: int) -> None:
    cfg = SerialConfig(baudrate=BAUD)
    async with await open_serial_port(path, cfg) as port:
        for _ in range(polls):
            await port.send(POLL_CMD)
            # fail_after keeps a wedged bot from hanging the whole run.
            with anyio.fail_after(1.0):
                while True:
                    chunk = await port.receive(128)
                    if b"\r" in chunk:
                        break


async def _fanout_anyserial_coro(paths: list[str], polls: int) -> None:
    async with anyio.create_task_group() as tg:
        for p in paths:
            tg.start_soon(_fanout_one_anyserial, p, polls)


def _bench_fanout_anyserial(n: int) -> float:
    with _pty_fleet(n) as paths:
        t0 = time.perf_counter()
        anyio.run(_fanout_anyserial_coro, paths, FAN_OUT_POLLS)
        return time.perf_counter() - t0


def _fanout_one_pyserial(path: str, polls: int) -> None:
    s = serial.Serial(path, BAUD, timeout=1.0)
    try:
        for i in range(polls):
            s.write(POLL_CMD)
            reply = s.read_until(b"\r")
            if not reply.endswith(b"\r"):
                raise RuntimeError(f"pyserial fanout {path} poll #{i} truncated/timeout: {reply!r}")
    finally:
        s.close()


def _bench_fanout_pyserial(n: int) -> float:
    with _pty_fleet(n) as paths:
        t0 = time.perf_counter()
        threads = [
            threading.Thread(
                target=_fanout_one_pyserial,
                args=(p, FAN_OUT_POLLS),
                daemon=True,
            )
            for p in paths
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return time.perf_counter() - t0


# ---------- orchestration -----------------------------------------------


def main() -> None:  # noqa: PLR0915 — benchmark orchestrator, linear by design
    # Scenario selection: pass one or more of {roundtrip, cancel, stream,
    # fanout} on the command line to run only those. No args = run all.
    valid = {"roundtrip", "cancel", "stream", "fanout"}
    requested = set(sys.argv[1:]) if len(sys.argv) > 1 else valid
    unknown = requested - valid
    if unknown:
        print(f"unknown scenario(s): {sorted(unknown)}; valid: {sorted(valid)}")
        sys.exit(2)

    any_backends: list[tuple[str, str, dict[str, object]]] = [
        ("anyserial asyncio", "asyncio", {"use_uvloop": False}),
        ("anyserial asyncio+uv", "asyncio", {"use_uvloop": True}),
        ("anyserial trio", "trio", {}),
    ]

    print(
        f"device: {PORT} @ {BAUD} 8N1\n"
        f"round-trip: {WARMUP_ITERS} warmup + {ROUND_TRIP_ITERS} timed poll→frame\n"
        f"cancel: {CANCEL_ITERS} x move_on_after({CANCEL_DEADLINE_S * 1000:.0f} ms)\n"
        f"streaming: {STREAM_FRAMES} frames / backend (default 50 ms rate)"
    )

    if "roundtrip" in requested:
        rt_rows: list[Stats] = []
        for label, backend, opts in any_backends:
            print(f"\n  [round-trip] {label} ...", flush=True)
            rt_rows.append(_bench_roundtrip_any_async(label, backend, opts))

        # No-portal variants: same library, no per-iter thread hop.
        for suffix, backend, opts in any_backends:
            label = suffix + " (no portal)"
            print(f"\n  [round-trip] {label} ...", flush=True)
            rt_rows.append(_bench_roundtrip_no_portal(label, backend, opts))

        # Idiomatic line-framed read: BufferedByteReceiveStream.receive_until.
        print("\n  [round-trip] anyserial asyncio + buffered (no portal) ...", flush=True)
        rt_rows.append(
            _bench_roundtrip_buffered(
                "anyserial + buffered",
                "asyncio",
                {"use_uvloop": False},
            )
        )
        print(
            "\n  [round-trip] anyserial asyncio+uv + buffered (no portal) ...",
            flush=True,
        )
        rt_rows.append(
            _bench_roundtrip_buffered(
                "anyserial + buffered+uv",
                "asyncio",
                {"use_uvloop": True},
            )
        )

        print("\n  [round-trip] anyserial sync ...", flush=True)
        rt_rows.append(_bench_roundtrip_any_sync())

        print("\n  [round-trip] pyserial-asyncio ...", flush=True)
        rt_rows.append(_bench_roundtrip_pyserial_asyncio())

        print("\n  [round-trip] pyserial (sync) ...", flush=True)
        rt_rows.append(_bench_roundtrip_pyserial())

        _print_table(rt_rows, "Poll round-trip (A\\r → data frame)")

    if "cancel" in requested:
        cancel_rows: list[Stats] = []
        for label, backend, opts in any_backends:
            print(f"\n  [cancel] {label} ...", flush=True)
            cancel_rows.append(_bench_cancel_any_async(label, backend, opts))

        print("\n  [cancel] anyserial sync ...", flush=True)
        cancel_rows.append(_bench_cancel_any_sync())

        print("\n  [cancel] pyserial-asyncio ...", flush=True)
        cancel_rows.append(_bench_cancel_pyserial_asyncio())

        print("\n  [cancel] pyserial (sync) ...", flush=True)
        cancel_rows.append(_bench_cancel_pyserial())

        _print_table(
            cancel_rows,
            f"Overshoot past {CANCEL_DEADLINE_S * 1000:.0f} ms deadline "
            f"(cancel for anyserial async; timeout for sync libs)",
        )

    if "stream" in requested:
        stream_rows: list[Stats] = []
        for label, backend, opts in any_backends:
            print(f"\n  [streaming] {label} ...", flush=True)
            stream_rows.append(_bench_stream(label, backend, opts))

        _print_table(stream_rows, "Streaming inter-frame interval (target 50 ms)")

    if "fanout" in requested:
        # pty-backed — timing is about scaling law, not absolute latency.
        print(f"\n=== Fan-out scaling — {FAN_OUT_POLLS} polls x N pty-echo devices ===")
        print(
            f"{'N':>4}  "
            f"{'anyserial async (s)':>22}  "
            f"{'pyserial threaded (s)':>22}  "
            f"{'ratio (py/any)':>16}"
        )
        print("-" * 74)
        for n in FAN_OUT_SIZES:
            print(f"\n  [fan-out N={n}] anyserial ...", flush=True)
            any_t = _bench_fanout_anyserial(n)
            print(f"  [fan-out N={n}] pyserial ...", flush=True)
            py_t = _bench_fanout_pyserial(n)
            print(f"{n:>4}  {any_t:>22.3f}  {py_t:>22.3f}  {py_t / any_t:>15.2f}x")

    # Leave the device streaming — only touch it if a scenario did.
    if requested & {"roundtrip", "cancel", "stream"}:
        print("\n[restore: streaming mode]")
        with _portal("asyncio", {"use_uvloop": False}) as portal:
            cfg = SerialConfig(baudrate=BAUD)

            async def _leave_streaming() -> None:
                port = await open_serial_port(PORT, cfg)
                try:
                    await port.send(f"{UNIT}@ @\r".encode("ascii"))
                    await anyio.sleep(0.2)
                finally:
                    await port.aclose()

            portal.call(_leave_streaming)


if __name__ == "__main__":
    main()
