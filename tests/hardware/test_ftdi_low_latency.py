"""Hardware test for ``low_latency=True`` on a real FTDI adapter.

Opt-in via the ``ANYSERIAL_TEST_PORT`` environment variable, matching the
``hardware`` marker registered in :file:`pyproject.toml`. The variable
must point at a loopback-wired (RX↔TX shorted) FTDI adapter so the test
can measure round-trip latency without a peer device.

Two things get verified:

1. Opening with ``low_latency=True`` actually drops the FTDI sysfs
   ``latency_timer`` to 1 ms and restores the original on close.
2. Round-trip latency at 115200 baud trends downward when the kernel
   ``ASYNC_LOW_LATENCY`` knob and the FTDI ``latency_timer`` are both
   enabled. The assertion is directional, not absolute — adapter
   firmware revisions and host scheduling vary too much to pin a number,
   but a 16 ms FTDI default vs 1 ms is large enough to be reliable.

Run via::

    ANYSERIAL_TEST_PORT=/dev/ttyUSB0 uv run pytest -m hardware
"""

from __future__ import annotations

import os
import statistics
import sys
import time

import anyio
import pytest

if not sys.platform.startswith("linux"):
    pytest.skip("FTDI sysfs path is Linux-only", allow_module_level=True)

from anyserial._linux.low_latency import ftdi_latency_timer_path
from anyserial.config import SerialConfig
from anyserial.stream import open_serial_port

pytestmark = pytest.mark.hardware

_ENV_VAR = "ANYSERIAL_TEST_PORT"
# Mirrors _FTDI_LATENCY_TIMER_TARGET_MS in anyserial._linux.low_latency.
# Hardcoded here so the test asserts against the documented contract
# rather than the implementation constant.
_TARGET_LATENCY_TIMER_MS = 1


def _port_from_env() -> str:
    """Return the env-supplied device path or skip the test."""
    path = os.environ.get(_ENV_VAR)
    if not path:
        pytest.skip(f"set {_ENV_VAR} to a loopback-wired FTDI port")
    return path


@pytest.fixture
def ftdi_port() -> str:
    """Resolve the device path and assert the adapter is actually FTDI."""
    path = _port_from_env()
    if ftdi_latency_timer_path(path) is None:
        pytest.skip(f"{path} is not driven by ftdi_sio")
    return path


async def _measure(
    port_path: str,
    config: SerialConfig,
    payload: bytes,
    samples: int,
) -> list[float]:
    """Time ``samples`` request/response round-trips on a loopback adapter."""
    durations: list[float] = []
    async with await open_serial_port(port_path, config) as port:
        # One warm-up round so any FTDI buffering settles before timing.
        await port.send(payload)
        with anyio.move_on_after(1.0):
            await port.receive(len(payload))
        for _ in range(samples):
            start = time.perf_counter()
            await port.send(payload)
            received = b""
            while len(received) < len(payload):
                chunk = await port.receive(len(payload) - len(received))
                received += chunk
            durations.append(time.perf_counter() - start)
    return durations


class TestSysfsRoundTrip:
    def test_latency_timer_set_to_one_then_restored(self, ftdi_port: str) -> None:
        timer_path = ftdi_latency_timer_path(ftdi_port)
        assert timer_path is not None
        original = int(timer_path.read_text().strip())

        async def _observe() -> int:
            async with await open_serial_port(
                ftdi_port,
                SerialConfig(baudrate=115200, low_latency=True),
            ):
                return int(timer_path.read_text().strip())

        observed_during_open = anyio.run(_observe)
        assert observed_during_open == _TARGET_LATENCY_TIMER_MS
        assert int(timer_path.read_text().strip()) == original


class TestLatencyImprovement:
    def test_low_latency_reduces_round_trip_at_115200(self, ftdi_port: str) -> None:
        payload = b"PING\n"
        samples = 20
        baseline = anyio.run(
            _measure,
            ftdi_port,
            SerialConfig(baudrate=115200, low_latency=False),
            payload,
            samples,
        )
        tuned = anyio.run(
            _measure,
            ftdi_port,
            SerialConfig(baudrate=115200, low_latency=True),
            payload,
            samples,
        )
        # Directional, not absolute. 70% of baseline is well above the
        # noise floor for a 16 ms→1 ms FTDI timer drop and survives the
        # scheduler jitter we routinely see on shared CI hosts.
        baseline_med_ms = statistics.median(baseline) * 1000
        tuned_med_ms = statistics.median(tuned) * 1000
        assert tuned_med_ms < baseline_med_ms * 0.7, (
            f"baseline median={baseline_med_ms:.2f} ms, tuned median={tuned_med_ms:.2f} ms"
        )
