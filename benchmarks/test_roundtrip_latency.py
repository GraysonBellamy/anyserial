"""Receive-side single-byte round-trip latency.

Measures the path **kernel pty write → ``wait_readable`` wakeup →
``os.read`` → bytes returned to ``await port.receive(1)``** at 115200 baud
on a Linux pty. This is the canonical micro-benchmark for the readiness
loop in :mod:`anyserial.stream` — every µs here turns into responsiveness
in real applications doing request/response over a serial link.

Each iteration:

1. ``os.write(controller, b"x")`` on the pty controller side.
2. ``await port.receive(1)`` on the anyserial side; resolves once the
   readiness wakeup fires and the non-blocking ``os.read`` returns the byte.

The ``benchmark`` fixture aggregates across many iterations and reports
mean / min / max / stddev. Pass ``--benchmark-json=path/to/results.json``
to capture machine-readable results for the regression gate.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from anyserial import SerialConfig, SerialPort, open_serial_port

if TYPE_CHECKING:
    from anyio.from_thread import BlockingPortal
    from pytest_benchmark.fixture import BenchmarkFixture


_BAUD = 115_200
_CFG = SerialConfig(baudrate=_BAUD)


async def _one_byte(port: SerialPort, controller: int) -> None:
    """Inject one byte on the controller and consume it on the port side."""
    os.write(controller, b"x")
    received = await port.receive(1)
    assert received == b"x"


def test_receive_latency_1byte_115200_pty(
    benchmark: BenchmarkFixture,
    bench_portal: BlockingPortal,
    pty_pair: tuple[int, str],
) -> None:
    controller, path = pty_pair
    port = bench_portal.call(open_serial_port, path, _CFG)
    try:
        # Warm-up round so any FTDI-style buffering or one-shot setup is
        # paid before the timed iterations start.
        bench_portal.call(_one_byte, port, controller)

        def _iter() -> None:
            bench_portal.call(_one_byte, port, controller)

        benchmark.pedantic(_iter, rounds=200, iterations=5, warmup_rounds=2)
    finally:
        bench_portal.call(port.aclose)


async def _send_then_read(port: SerialPort, controller: int) -> None:
    """Send one byte from the port, drain it on the controller side."""
    await port.send(b"y")
    while True:
        try:
            data = os.read(controller, 1)
        except BlockingIOError:
            # The controller fd is non-blocking (see raw_pty_pair); spin
            # briefly until the kernel surfaces the byte. Sleeping back to
            # the loop here would inflate latency by a scheduler tick.
            continue
        if data:
            return


def test_send_latency_1byte_115200_pty(
    benchmark: BenchmarkFixture,
    bench_portal: BlockingPortal,
    pty_pair: tuple[int, str],
) -> None:
    controller, path = pty_pair
    port = bench_portal.call(open_serial_port, path, _CFG)
    try:
        bench_portal.call(_send_then_read, port, controller)

        def _iter() -> None:
            bench_portal.call(_send_then_read, port, controller)

        benchmark.pedantic(_iter, rounds=200, iterations=5, warmup_rounds=2)
    finally:
        bench_portal.call(port.aclose)
