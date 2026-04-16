"""Timing benchmarks for :meth:`SerialPort.receive_available`.

Drains a controller-side write at 64 B / 1 KiB / 4 KiB queue depths. The
syscall-budget correctness assertion (the DESIGN §26.1 "1 syscall per
``receive_available()``" guarantee) lives in
``tests/integration/test_receive_syscall_budget.py`` so it runs as part
of every test suite, not just when ``--benchmark-only`` is active.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

from anyserial import SerialConfig, SerialPort, open_serial_port

if TYPE_CHECKING:
    from anyio.from_thread import BlockingPortal
    from pytest_benchmark.fixture import BenchmarkFixture

_CFG = SerialConfig(baudrate=115_200)
# Pty kernel buffer is 4 KiB on Linux; larger than that takes multiple
# injections on the controller side, which would muddle the timing.
_DEPTHS = [64, 1024, 4096]


async def _inject_then_drain(port: SerialPort, controller: int, payload: bytes) -> None:
    """Push ``payload`` onto the pty, drain it through receive_available."""
    os.write(controller, payload)
    drained = bytearray()
    while len(drained) < len(payload):
        chunk = await port.receive_available()
        drained.extend(chunk)
    assert bytes(drained[: len(payload)]) == payload


@pytest.mark.parametrize("depth", _DEPTHS, ids=lambda n: f"{n}B")
def test_receive_available_drain_pty(
    benchmark: BenchmarkFixture,
    bench_portal: BlockingPortal,
    pty_pair: tuple[int, str],
    depth: int,
) -> None:
    controller, path = pty_pair
    payload = b"\xcc" * depth
    port = bench_portal.call(open_serial_port, path, _CFG)
    try:
        # Warm-up.
        bench_portal.call(_inject_then_drain, port, controller, payload)

        def _iter() -> None:
            bench_portal.call(_inject_then_drain, port, controller, payload)

        # Larger payloads finish faster per byte; scale rounds down so the
        # total wall-time stays bounded across the matrix.
        rounds = 200 if depth <= 1024 else 100
        benchmark.pedantic(_iter, rounds=rounds, iterations=1, warmup_rounds=1)
    finally:
        bench_portal.call(port.aclose)
