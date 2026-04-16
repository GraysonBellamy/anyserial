"""Windows backend benchmarks over a com0com virtual COM-port pair.

design-windows-backend.md SS11 defines four benchmark scenarios with explicit
targets:

1. **Single-port round-trip, 1 B request/reply** -- p99 <= 3x Linux p99 on
   same hardware. Measures IOCP dispatch + overlapped I/O overhead.

2. **Throughput at 921600 baud, 4 KiB chunks** -- >= 90% of pyserial-asyncio
   POSIX equivalent. com0com doesn't enforce baud-rate throttling, so this
   really measures the per-call overhead of the overlapped write path at a
   non-trivial payload size.

3. **32 concurrent ports (com0com pairs)** -- no thread growth; CPU scales
   linearly. Validates that IOCP completion dispatch doesn't spawn worker
   threads per port.

4. **Open / close cycle** -- < 50 ms per cycle. Catches handle leaks,
   driver-state regressions, and DCB round-trip overhead.

Fixtures (``bench_portal``, ``com_pair``) are defined in this module rather
than ``conftest.py`` because the project's ``benchmarks/conftest.py`` is
Linux-only (it imports ``pty`` / ``termios`` / ``fcntl``). Wrapping that in a
platform switch would make it harder to read; defining the Windows fixtures
locally is cleaner and the pattern only needs to live in one place.

The module-level platform guard skips everything here on non-Windows hosts,
so the Linux conftest's skip never fights with this file.
"""

from __future__ import annotations

import contextlib
import os
import sys
import threading
from typing import TYPE_CHECKING

import anyio
import anyio.from_thread
import pytest

from anyserial import SerialConfig, SerialPort, open_serial_port

if TYPE_CHECKING:
    from collections.abc import Iterator

    from anyio.from_thread import BlockingPortal
    from pytest_benchmark.fixture import BenchmarkFixture

# Read into a local so mypy doesn't narrow each branch to the type-checker's
# host platform and flag the rest of the file as unreachable. Mirrors the
# pattern used in tests/integration/test_windows_backend.py.
_PLATFORM = sys.platform
if _PLATFORM != "win32":
    pytest.skip(
        "Windows benchmarks require a Windows host with com0com",
        allow_module_level=True,
    )


# ---------------------------------------------------------------------------
# Fixtures (Windows-only — see module docstring for why these don't live in
# benchmarks/conftest.py)
# ---------------------------------------------------------------------------


def _pair_from_env() -> tuple[str, str] | None:
    """Parse ``ANYSERIAL_WINDOWS_PAIR=COMA,COMB`` into a 2-tuple, or None."""
    raw = os.environ.get("ANYSERIAL_WINDOWS_PAIR")
    if not raw:
        return None
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if len(parts) != 2:
        msg = f"ANYSERIAL_WINDOWS_PAIR must be 'COMA,COMB'; got {raw!r}"
        raise RuntimeError(msg)
    return parts[0], parts[1]


_BACKEND_PARAMS = [
    pytest.param(("asyncio", {"use_uvloop": False}), id="asyncio"),
    pytest.param(("trio", {}), id="trio"),
]


@pytest.fixture(params=_BACKEND_PARAMS)
def bench_portal(
    request: pytest.FixtureRequest,
) -> Iterator[BlockingPortal]:
    """Yield a :class:`BlockingPortal` for the parametrized AnyIO backend.

    Windows backend matrix is asyncio (ProactorEventLoop) and trio only —
    uvloop does not build on Windows.
    """
    backend, options = request.param
    with anyio.from_thread.start_blocking_portal(backend, backend_options=options) as portal:
        yield portal


@pytest.fixture
def com_pair() -> tuple[str, str]:
    """Yield a ``(port_a, port_b)`` com0com virtual pair.

    Skips the test if ``ANYSERIAL_WINDOWS_PAIR`` is not set.
    """
    pair = _pair_from_env()
    if pair is None:
        pytest.skip("ANYSERIAL_WINDOWS_PAIR not set; skipping com0com benchmarks")
    return pair


# ---------------------------------------------------------------------------
# Scenario 1: Single-port round-trip, 1 B request/reply
# ---------------------------------------------------------------------------

_ROUNDTRIP_CFG = SerialConfig(baudrate=115_200)


async def _one_byte_roundtrip(a: SerialPort, b: SerialPort) -> None:
    """Send 1 byte A->B, then 1 byte B->A."""
    await a.send(b"Q")
    data = await b.receive(1)
    assert data == b"Q"
    await b.send(b"R")
    back = await a.receive(1)
    assert back == b"R"


def test_roundtrip_1byte_115200_com0com(
    benchmark: BenchmarkFixture,
    bench_portal: BlockingPortal,
    com_pair: tuple[str, str],
) -> None:
    """SS11 scenario 1: single-byte round-trip latency.

    Target: p99 <= 3x Linux p99 on equivalent hardware.
    """
    a_path, b_path = com_pair
    a = bench_portal.call(open_serial_port, a_path, _ROUNDTRIP_CFG)
    b = bench_portal.call(open_serial_port, b_path, _ROUNDTRIP_CFG)
    try:
        # Warm-up: prime the overlapped I/O registration on both ports.
        bench_portal.call(_one_byte_roundtrip, a, b)

        def _iter() -> None:
            bench_portal.call(_one_byte_roundtrip, a, b)

        benchmark.pedantic(_iter, rounds=200, iterations=5, warmup_rounds=2)
    finally:
        bench_portal.call(a.aclose)
        bench_portal.call(b.aclose)


# ---------------------------------------------------------------------------
# Scenario 2: Throughput at 921600 baud, 4 KiB chunks
# ---------------------------------------------------------------------------

_THROUGHPUT_BAUD = 921_600
_THROUGHPUT_CFG = SerialConfig(baudrate=_THROUGHPUT_BAUD)
_CHUNK_SIZE = 4096


async def _send_and_drain(
    sender: SerialPort,
    receiver: SerialPort,
    payload: bytes,
) -> None:
    """Send ``payload`` on *sender* and drain to completion on *receiver*."""
    drained = bytearray()

    async def drain() -> None:
        while len(drained) < len(payload):
            chunk = await receiver.receive(len(payload) - len(drained))
            drained.extend(chunk)

    async with anyio.create_task_group() as tg:
        tg.start_soon(drain)
        await sender.send(payload)

    assert len(drained) == len(payload)


def test_throughput_4kib_921600_com0com(
    benchmark: BenchmarkFixture,
    bench_portal: BlockingPortal,
    com_pair: tuple[str, str],
) -> None:
    """SS11 scenario 2: throughput at 921600 baud, 4 KiB chunks.

    Target: >= 90% of pyserial-asyncio POSIX equivalent.
    com0com doesn't enforce baud-rate throttling, so this measures per-call
    overhead of the overlapped write + read paths at a realistic payload size.
    """
    a_path, b_path = com_pair
    payload = b"\xaa" * _CHUNK_SIZE
    a = bench_portal.call(open_serial_port, a_path, _THROUGHPUT_CFG)
    b = bench_portal.call(open_serial_port, b_path, _THROUGHPUT_CFG)
    try:
        # Warm-up.
        bench_portal.call(_send_and_drain, a, b, payload)

        def _iter() -> None:
            bench_portal.call(_send_and_drain, a, b, payload)

        benchmark.pedantic(_iter, rounds=100, iterations=1, warmup_rounds=1)
    finally:
        bench_portal.call(a.aclose)
        bench_portal.call(b.aclose)


# ---------------------------------------------------------------------------
# Scenario 3: 32 concurrent ports — IOCP scalability
# ---------------------------------------------------------------------------

_FANOUT_CFG = SerialConfig(baudrate=115_200)
_FANOUT_DEFAULT = [8, 32]


def _discover_all_pairs() -> list[tuple[str, str]]:
    """Return all com0com pairs from ``ANYSERIAL_WINDOWS_PAIRS`` (plural).

    Format: ``"COMA1,COMB1;COMA2,COMB2;..."``.  Falls back to the single
    pair in ``ANYSERIAL_WINDOWS_PAIR`` (singular) if the plural form is unset.
    """
    raw = os.environ.get("ANYSERIAL_WINDOWS_PAIRS", "")
    if raw:
        pairs: list[tuple[str, str]] = []
        for entry in raw.split(";"):
            parts = [p.strip() for p in entry.split(",") if p.strip()]
            if len(parts) == 2:
                pairs.append((parts[0], parts[1]))
        if pairs:
            return pairs
    # Fall back to singular env var.
    single = os.environ.get("ANYSERIAL_WINDOWS_PAIR", "")
    if single:
        parts = [p.strip() for p in single.split(",") if p.strip()]
        if len(parts) == 2:
            return [(parts[0], parts[1])]
    return []


def _select_fanout() -> list[int]:
    if os.environ.get("ANYSERIAL_BENCH_HEAVY") == "1":
        return [*_FANOUT_DEFAULT, 128]
    return _FANOUT_DEFAULT


async def _fanout_round(pairs: list[tuple[SerialPort, SerialPort]]) -> None:
    """One byte round-trip per (sender, receiver) pair, all concurrent."""

    async def one(sender: SerialPort, receiver: SerialPort) -> None:
        await sender.send(b"x")
        data = await receiver.receive(1)
        assert data == b"x"

    async with anyio.create_task_group() as tg:
        for sender, receiver in pairs:
            tg.start_soon(one, sender, receiver)


@pytest.mark.parametrize("n_ports", _select_fanout(), ids=lambda n: f"{n}ports")
def test_fanout_roundtrip_com0com(
    benchmark: BenchmarkFixture,
    bench_portal: BlockingPortal,
    n_ports: int,
) -> None:
    """SS11 scenario 3: N concurrent ports, no thread growth.

    Target: no thread growth; CPU scales linearly with port count.

    Windows COM ports are opened with ``dwShareMode=0`` (exclusive access),
    so each port in the fanout needs its own com0com pair. Provision
    multiple pairs via ``ANYSERIAL_WINDOWS_PAIRS`` (semicolon-separated):

        set ANYSERIAL_WINDOWS_PAIRS=COM50,COM51;COM52,COM53;...

    If fewer pairs are available than ``n_ports``, the test skips.
    """
    all_pairs = _discover_all_pairs()
    if len(all_pairs) < n_ports:
        pytest.skip(
            f"Need {n_ports} com0com pairs but only {len(all_pairs)} available. "
            f"Set ANYSERIAL_WINDOWS_PAIRS='COMA1,COMB1;COMA2,COMB2;...' "
            f"with at least {n_ports} pairs."
        )

    ports: list[tuple[SerialPort, SerialPort]] = []
    try:
        for a_path, b_path in all_pairs[:n_ports]:
            a = bench_portal.call(open_serial_port, a_path, _FANOUT_CFG)
            b = bench_portal.call(open_serial_port, b_path, _FANOUT_CFG)
            ports.append((a, b))

        # Record thread count before the benchmark body.
        threads_before = threading.active_count()

        # Warm-up.
        bench_portal.call(_fanout_round, ports)

        def _iter() -> None:
            bench_portal.call(_fanout_round, ports)

        rounds = max(20, 200 // max(1, n_ports // 8))
        benchmark.pedantic(_iter, rounds=rounds, iterations=1, warmup_rounds=1)

        # SS11 target: no thread growth from IOCP dispatch.
        threads_after = threading.active_count()
        # Allow a margin of 2 threads for GC / interpreter housekeeping.
        assert threads_after <= threads_before + 2, (
            f"Thread count grew from {threads_before} to {threads_after} "
            f"during {n_ports}-port fanout — IOCP should not spawn worker threads"
        )
    finally:
        for a, b in ports:
            with contextlib.suppress(Exception):
                bench_portal.call(a.aclose)
            with contextlib.suppress(Exception):
                bench_portal.call(b.aclose)


# ---------------------------------------------------------------------------
# Scenario 4: Open / close cycle
# ---------------------------------------------------------------------------

_LIFECYCLE_CFG = SerialConfig(baudrate=115_200)


async def _open_close_cycle(path: str) -> None:
    """Open a port, immediately close it."""
    port = await open_serial_port(path, _LIFECYCLE_CFG)
    await port.aclose()


def test_open_close_cycle_com0com(
    benchmark: BenchmarkFixture,
    bench_portal: BlockingPortal,
    com_pair: tuple[str, str],
) -> None:
    """SS11 scenario 4: open/close cycle time.

    Target: < 50 ms per cycle. Catches handle leaks, driver-state regressions,
    and DCB round-trip overhead from the GetCommState strategy (SS6.2.1).
    """
    a_path, _b_path = com_pair

    # Warm-up.
    bench_portal.call(_open_close_cycle, a_path)

    def _iter() -> None:
        bench_portal.call(_open_close_cycle, a_path)

    benchmark.pedantic(_iter, rounds=200, iterations=1, warmup_rounds=2)
