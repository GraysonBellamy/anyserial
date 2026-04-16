"""Allocation profile of the receive hot path.

Not timing benchmarks — regression guards. ``tracemalloc`` snapshots
before/after a tight receive loop should report close to zero net
allocations on the payload path. Three variants, each with its own
ceiling reflecting what the call actually does:

- ``receive(1)`` — allocates a fresh ``bytes`` object per call
  (user-visible slice). Budget is loose (256 KiB / 200 calls) because
  small ``bytes`` objects interact with CPython's internal small-int /
  interned-buffer caches, which report as allocations on tracemalloc
  snapshots without actually growing the heap.
- ``receive_into(buf)`` — caller-owned buffer, zero payload allocation.
  This is the **DESIGN §26.1 "zero payload allocation" target**. Budget
  is tight (16 KiB / 200 calls) so any accidental copy — e.g. a stray
  ``bytes(view)`` — surfaces immediately.
- ``receive_available()`` — one ``bytes`` allocation per call (return
  value). Budget sits between the other two.

The assertion is what guards regressions; the timing bench-pedantic
wrapper exists so the numbers land in the JSON report alongside the
other bench outputs.
"""

from __future__ import annotations

import os
import tracemalloc
from typing import TYPE_CHECKING

import pytest

from anyserial import SerialConfig, SerialPort, open_serial_port

if TYPE_CHECKING:
    from anyio.from_thread import BlockingPortal
    from pytest_benchmark.fixture import BenchmarkFixture

_CFG = SerialConfig(baudrate=115_200)
_ITERATIONS = 200


async def _drain_via_receive(port: SerialPort, controller: int, n: int) -> None:
    for _ in range(n):
        os.write(controller, b"z")
        await port.receive(1)


async def _drain_via_receive_into(port: SerialPort, controller: int, n: int) -> None:
    # One bytearray, reused for every call. The hot-path contract is
    # that os.readv fills this buffer in place — zero payload allocation.
    buf = bytearray(1)
    for _ in range(n):
        os.write(controller, b"z")
        await port.receive_into(buf)


async def _drain_via_receive_available(port: SerialPort, controller: int, n: int) -> None:
    for _ in range(n):
        os.write(controller, b"z")
        await port.receive_available()


def _measure_allocs(
    benchmark: BenchmarkFixture,
    bench_portal: BlockingPortal,
    port: SerialPort,
    controller: int,
    drain_fn: object,
    warmup_n: int,
) -> int:
    """Return ``net_bytes`` allocated across ``benchmark.pedantic`` iterations.

    Shared skeleton: prime the readiness path, start tracemalloc, run the
    drain in a pedantic loop, diff the snapshots. Caller asserts on the
    returned byte count so the ceiling lives next to the variant-specific
    docstring where the budget can be justified.
    """
    bench_portal.call(drain_fn, port, controller, warmup_n)

    tracemalloc.start()
    snap_before = tracemalloc.take_snapshot()

    def _iter() -> None:
        bench_portal.call(drain_fn, port, controller, _ITERATIONS)

    benchmark.pedantic(_iter, rounds=5, iterations=1, warmup_rounds=0)

    snap_after = tracemalloc.take_snapshot()
    tracemalloc.stop()
    diff = snap_after.compare_to(snap_before, "filename")
    return sum(stat.size_diff for stat in diff)


def _fail_with_top_allocators(
    bench_portal: BlockingPortal,
    port: SerialPort,
    controller: int,
    drain_fn: object,
    net_bytes: int,
    ceiling: int,
    name: str,
) -> None:
    """Re-run the drain with tracemalloc on to attribute the regression."""
    tracemalloc.start()
    snap_before = tracemalloc.take_snapshot()
    bench_portal.call(drain_fn, port, controller, _ITERATIONS)
    snap_after = tracemalloc.take_snapshot()
    tracemalloc.stop()
    diff = snap_after.compare_to(snap_before, "filename")
    top = "\n".join(str(stat) for stat in diff[:10])
    pytest.fail(
        f"{name}: net allocation {net_bytes} bytes exceeds ceiling {ceiling}\n"
        f"Top allocators:\n{top}"
    )


# Generous: each receive(1) creates a fresh bytes object. 256 KiB / 200
# calls ≈ 1.3 KiB per call worth of noise tolerance.
_CEILING_RECEIVE = 256 * 1024


def test_receive_loop_allocation_ceiling(
    benchmark: BenchmarkFixture,
    bench_portal: BlockingPortal,
    pty_pair: tuple[int, str],
) -> None:
    controller, path = pty_pair
    port = bench_portal.call(open_serial_port, path, _CFG)
    try:
        net_bytes = _measure_allocs(
            benchmark, bench_portal, port, controller, _drain_via_receive, warmup_n=4
        )
        if net_bytes > _CEILING_RECEIVE:
            _fail_with_top_allocators(
                bench_portal,
                port,
                controller,
                _drain_via_receive,
                net_bytes,
                _CEILING_RECEIVE,
                "receive(1)",
            )
    finally:
        bench_portal.call(port.aclose)


# §26.1 target: zero payload allocation. A reused bytearray means the
# only growth should be readiness-loop bookkeeping (AnyIO internals,
# Python frame churn). Empirically sits under 4 KiB on Python 3.13; we
# set the gate at 16 KiB / 200 calls = 80 bytes per call so a stray
# ``bytes(...)`` copy of the 1-byte payload surfaces immediately.
_CEILING_RECEIVE_INTO = 16 * 1024


def test_receive_into_zero_payload_allocation(
    benchmark: BenchmarkFixture,
    bench_portal: BlockingPortal,
    pty_pair: tuple[int, str],
) -> None:
    controller, path = pty_pair
    port = bench_portal.call(open_serial_port, path, _CFG)
    try:
        net_bytes = _measure_allocs(
            benchmark,
            bench_portal,
            port,
            controller,
            _drain_via_receive_into,
            warmup_n=4,
        )
        if net_bytes > _CEILING_RECEIVE_INTO:
            _fail_with_top_allocators(
                bench_portal,
                port,
                controller,
                _drain_via_receive_into,
                net_bytes,
                _CEILING_RECEIVE_INTO,
                "receive_into",
            )
    finally:
        bench_portal.call(port.aclose)


# Between the other two: one bytes allocation per call (return value),
# but a single readiness wait drains whatever TIOCINQ reports, so the
# per-byte overhead is lower than receive(1). 64 KiB is plenty of
# headroom without letting a regression hide.
_CEILING_RECEIVE_AVAILABLE = 64 * 1024


def test_receive_available_loop_allocation_ceiling(
    benchmark: BenchmarkFixture,
    bench_portal: BlockingPortal,
    pty_pair: tuple[int, str],
) -> None:
    controller, path = pty_pair
    port = bench_portal.call(open_serial_port, path, _CFG)
    try:
        net_bytes = _measure_allocs(
            benchmark,
            bench_portal,
            port,
            controller,
            _drain_via_receive_available,
            warmup_n=4,
        )
        if net_bytes > _CEILING_RECEIVE_AVAILABLE:
            _fail_with_top_allocators(
                bench_portal,
                port,
                controller,
                _drain_via_receive_available,
                net_bytes,
                _CEILING_RECEIVE_AVAILABLE,
                "receive_available",
            )
    finally:
        bench_portal.call(port.aclose)
