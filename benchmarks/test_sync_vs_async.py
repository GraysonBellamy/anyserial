"""Sync wrapper vs. raw ``portal.call(async)`` — overhead head-to-head.

The sync :class:`anyserial.sync.SerialPort` is a pure delegation layer
over the async port; every blocking call is implemented as one
``portal.call(coroutine, *args)``. This suite quantifies the extra
overhead introduced by the wrapper vs. calling through the portal
directly, against both a real pty and the in-kernel readiness path.

Measurements run against the default ``asyncio`` portal backend; the
cross-backend matrix is already covered by
:mod:`benchmarks.test_roundtrip_latency`. The wrapper adds at most one
Python frame per call — the target is ≤ ~15 % overhead vs. raw
``portal.call``.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

from anyserial import SerialConfig
from anyserial import open_serial_port as async_open_serial_port
from anyserial.sync import (
    SerialPort as SyncSerialPort,
)
from anyserial.sync import (
    _reset_portal_for_testing,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from anyio.from_thread import BlockingPortal
    from pytest_benchmark.fixture import BenchmarkFixture

    from anyserial import SerialPort as AsyncSerialPort


_BAUD = 115_200
_CFG = SerialConfig(baudrate=_BAUD)


@pytest.fixture(autouse=True)
def _reset_sync_portal() -> Iterator[None]:
    """Drop the sync wrapper's process-wide portal between benchmark cases.

    Each benchmark builds its own portal lifecycle; resetting the cached
    provider keeps configure_portal and portal identity deterministic.
    """
    yield
    _reset_portal_for_testing()


# ---------------------------------------------------------------------------
# Sync wrapper path
# ---------------------------------------------------------------------------


def test_sync_receive_latency_1byte_pty(
    benchmark: BenchmarkFixture,
    pty_pair: tuple[int, str],
) -> None:
    """Single-byte receive via ``anyserial.sync.SerialPort.receive``."""
    controller, path = pty_pair
    port = SyncSerialPort.open(path, _CFG)
    try:

        def _iter() -> None:
            os.write(controller, b"x")
            data = port.receive(1)
            assert data == b"x"

        # Warm-up so the portal thread, first async-port alloc, and
        # one-shot caches don't contaminate the timed rounds.
        _iter()
        benchmark.pedantic(_iter, rounds=200, iterations=5, warmup_rounds=2)
    finally:
        port.close()


def test_sync_send_latency_1byte_pty(
    benchmark: BenchmarkFixture,
    pty_pair: tuple[int, str],
) -> None:
    controller, path = pty_pair
    port = SyncSerialPort.open(path, _CFG)
    try:

        def _iter() -> None:
            port.send(b"y")
            while True:
                try:
                    data = os.read(controller, 1)
                except BlockingIOError:
                    continue
                if data:
                    return

        _iter()
        benchmark.pedantic(_iter, rounds=200, iterations=5, warmup_rounds=2)
    finally:
        port.close()


# ---------------------------------------------------------------------------
# Raw portal.call(async) path — baseline
# ---------------------------------------------------------------------------


async def _one_byte_async(port: AsyncSerialPort, controller: int) -> None:
    os.write(controller, b"x")
    received = await port.receive(1)
    assert received == b"x"


def test_async_receive_latency_1byte_pty(
    benchmark: BenchmarkFixture,
    bench_portal: BlockingPortal,
    pty_pair: tuple[int, str],
) -> None:
    """Reference: same scenario dispatched via raw ``portal.call``."""
    controller, path = pty_pair
    port = bench_portal.call(async_open_serial_port, path, _CFG)
    try:
        bench_portal.call(_one_byte_async, port, controller)

        def _iter() -> None:
            bench_portal.call(_one_byte_async, port, controller)

        benchmark.pedantic(_iter, rounds=200, iterations=5, warmup_rounds=2)
    finally:
        bench_portal.call(port.aclose)
