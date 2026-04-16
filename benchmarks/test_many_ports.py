"""Many-port scalability — fan-out cost of one round-trip per port.

Opens N pty pairs in parallel, runs one byte through each, and times the
total wall-clock cost. The interesting metric is **how badly** wall-time
grows with N: linear means the readiness loop scales fine, super-linear
means a per-port overhead is showing up that wasn't visible at N=1.

We run a small sweep (8, 32) by default. The 128-port case in
:doc:`DESIGN` §28.1 is gated behind ``ANYSERIAL_BENCH_HEAVY=1`` so
laptop-class CI doesn't burn minutes on file-descriptor exhaustion
checks every push.
"""

from __future__ import annotations

import contextlib
import os
from typing import TYPE_CHECKING

import anyio
import pytest

from anyserial import SerialConfig, SerialPort, open_serial_port

if TYPE_CHECKING:
    from anyio.from_thread import BlockingPortal
    from pytest_benchmark.fixture import BenchmarkFixture

from benchmarks.conftest import raw_pty_pair

_CFG = SerialConfig(baudrate=115_200)
_DEFAULT_FANOUT = [8, 32]
_HEAVY_FANOUT = [128]


def _select_fanout() -> list[int]:
    if os.environ.get("ANYSERIAL_BENCH_HEAVY") == "1":
        return _DEFAULT_FANOUT + _HEAVY_FANOUT
    return _DEFAULT_FANOUT


async def _fanout_round(ports: list[tuple[SerialPort, int]]) -> None:
    """One round-trip per (port, controller_fd) pair, all concurrent."""

    async def one(port: SerialPort, controller: int) -> None:
        os.write(controller, b"x")
        received = await port.receive(1)
        assert received == b"x"

    async with anyio.create_task_group() as tg:
        for port, controller in ports:
            tg.start_soon(one, port, controller)


@pytest.mark.parametrize("n_ports", _select_fanout(), ids=lambda n: f"{n}ports")
def test_fanout_roundtrip_pty(
    benchmark: BenchmarkFixture,
    bench_portal: BlockingPortal,
    n_ports: int,
) -> None:
    # Open n_ports pty pairs outside the timed body; teardown after.
    pty_stack = contextlib.ExitStack()
    try:
        pairs: list[tuple[int, str]] = [
            pty_stack.enter_context(raw_pty_pair()) for _ in range(n_ports)
        ]

        ports: list[tuple[SerialPort, int]] = []
        try:
            for controller, path in pairs:
                port = bench_portal.call(open_serial_port, path, _CFG)
                ports.append((port, controller))

            # Warm-up.
            bench_portal.call(_fanout_round, ports)

            def _iter() -> None:
                bench_portal.call(_fanout_round, ports)

            # Iteration cost scales with n_ports — keep total bench time bounded.
            rounds = max(20, 200 // max(1, n_ports // 8))
            benchmark.pedantic(_iter, rounds=rounds, iterations=1, warmup_rounds=1)
        finally:
            for port, _controller in ports:
                bench_portal.call(port.aclose)
    finally:
        pty_stack.close()
