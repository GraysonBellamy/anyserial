"""Bulk-transfer throughput.

Sends a fixed-size payload from the anyserial port to the pty controller
and back, measuring wall-clock time per round-trip. The interesting metric
isn't bytes/sec at the kernel-pty layer (that's effectively unbounded —
no real link rate to throttle it) but rather the per-call overhead of the
write-then-drain loop in :class:`SerialPort` at non-trivial payload sizes.

A pty under the hood has a bounded internal buffer (~4 KiB on Linux), so
the bench drains the controller fd in a tight non-blocking loop alongside
each ``port.send`` to keep the buffer from blocking the writer. That mirrors
the realistic case where the peer end is also reading.

Pass ``--benchmark-json=path`` to capture results.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import anyio

from anyserial import SerialConfig, SerialPort, open_serial_port

if TYPE_CHECKING:
    from anyio.from_thread import BlockingPortal
    from pytest_benchmark.fixture import BenchmarkFixture

import anyio.lowlevel
import pytest

_BAUD = 4_000_000  # pty doesn't honour baud, but track it for the report
_CFG = SerialConfig(baudrate=_BAUD)
_PAYLOAD_SIZES = [256, 4096, 65536]


async def _send_and_drain(port: SerialPort, controller: int, payload: bytes) -> None:
    """Send ``payload`` and concurrently drain the pty controller side."""
    drained = bytearray()

    async def drain() -> None:
        while len(drained) < len(payload):
            try:
                chunk = os.read(controller, 16384)
            except BlockingIOError:
                # Yield back to the loop instead of busy-spinning; the
                # writer fills the kernel buffer in 4-KiB chunks and this
                # task wakes up via the next runloop tick.
                await anyio.lowlevel.checkpoint()
                continue
            if not chunk:
                continue
            drained.extend(chunk)

    async with anyio.create_task_group() as tg:
        tg.start_soon(drain)
        await port.send(payload)
    assert bytes(drained[: len(payload)]) == payload


@pytest.mark.parametrize("size", _PAYLOAD_SIZES, ids=lambda s: f"{s}B")
def test_send_throughput_pty(
    benchmark: BenchmarkFixture,
    bench_portal: BlockingPortal,
    pty_pair: tuple[int, str],
    size: int,
) -> None:
    controller, path = pty_pair
    payload = b"\xaa" * size
    port = bench_portal.call(open_serial_port, path, _CFG)
    try:
        # Warm-up.
        bench_portal.call(_send_and_drain, port, controller, payload)

        def _iter() -> None:
            bench_portal.call(_send_and_drain, port, controller, payload)

        # Larger payloads are slower; trade rounds for iterations so the
        # total wall-time stays bounded across the size matrix.
        rounds = 100 if size <= 4096 else 30
        benchmark.pedantic(_iter, rounds=rounds, iterations=1, warmup_rounds=1)
    finally:
        bench_portal.call(port.aclose)
