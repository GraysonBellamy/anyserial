# Benchmarks

Reproducible micro- and macro-benchmarks for `anyserial`. Per
[DESIGN.md §28](../DESIGN.md#28-benchmark-strategy), each scenario emits
machine-readable JSON so regressions are diff-able and the published
numbers in `docs/performance.md` come from one canonical command.

## Quick start

```bash
make bench
```

Equivalent to:

```bash
uv sync --group bench
mkdir -p benchmarks/results
uv run pytest benchmarks/ --benchmark-only \
    --benchmark-json=benchmarks/results/$(git rev-parse --short HEAD).json
```

The `bench` Makefile target writes one JSON file per git short-SHA so
every committed measurement run is a separate, comparable file.

## Architecture

Async benchmarks use `anyio.from_thread.start_blocking_portal` rather
than `anyio.run` per iteration — `anyio.run` startup is ≈50 ms, which
swamps the µs-scale workloads we care about. The `bench_portal` fixture
in [conftest.py](conftest.py) holds one persistent loop per test and
parametrizes across the same AnyIO backend matrix the rest of the suite
uses (asyncio, asyncio+uvloop, trio).

Each timed iteration is a single `portal.call(coroutine_fn, *args)`
invocation — round-trip overhead in the tens of µs, small enough that
the timed body still reflects the workload we're measuring.

## Scripts

| File | What it measures | Default backend matrix |
|---|---|---|
| [test_roundtrip_latency.py](test_roundtrip_latency.py) | 1-byte receive and send latency at 115200 baud over a Linux pty | asyncio / asyncio+uvloop / trio |
| [test_throughput.py](test_throughput.py) | Bulk-transfer wall-time at 256 / 4 KiB / 64 KiB payloads | asyncio / asyncio+uvloop / trio |
| [test_many_ports.py](test_many_ports.py) | Fan-out cost: one round-trip across 8 / 32 ports | asyncio / asyncio+uvloop / trio |
| [test_allocation_profile.py](test_allocation_profile.py) | tracemalloc gate: net allocations per receive loop must stay under 256 KiB | asyncio / asyncio+uvloop / trio |
| [test_compare_pyserial.py](test_compare_pyserial.py) | `pyserial-asyncio` head-to-head on the same 1-byte scenario | asyncio only (pyserial-asyncio is asyncio-only) |
| [test_windows_throughput.py](test_windows_throughput.py) | Windows IOCP: round-trip, throughput, fanout, open/close (com0com) | asyncio (Proactor) / trio |

`test_compare_pyserial.py` skips unless `pyserial-asyncio` is installed —
no extra is declared for it; install it manually when you want the
comparison numbers (`uv pip install pyserial-asyncio`).

## Heavy mode

The 128-port fan-out scenario from DESIGN §28.1 is gated behind
`ANYSERIAL_BENCH_HEAVY=1` so laptop-class CI runs stay under a minute:

```bash
ANYSERIAL_BENCH_HEAVY=1 make bench
```

## Windows benchmarks

The Windows benchmark suite (`test_windows_throughput.py`) exercises the
`AsyncSerialBackend` IOCP path over a com0com virtual COM-port pair. It
covers the four scenarios from
[design-windows-backend.md §11](../docs/design-windows-backend.md):

| Scenario | Target |
|---|---|
| Single-port round-trip, 1 B request/reply | p99 ≤ 3× Linux p99 |
| Throughput at 921600 baud, 4 KiB chunks | ≥ 90% of pyserial-asyncio POSIX |
| 32 concurrent ports | No thread growth; CPU scales linearly |
| Open / close cycle | < 50 ms per cycle |

### Prerequisites

Install a com0com virtual serial pair. On CI this is handled by the
workflow; locally:

```bash
# Chocolatey (preferred)
choco install com0com -y

# Or: download the signed installer from https://github.com/paulakg4/com0com
# and run: setupc.exe install PortName=COM50 PortName=COM51
```

Set the pair via environment variable:

```bash
set ANYSERIAL_WINDOWS_PAIR=COM50,COM51
```

For the fanout test (scenario 3), multiple pairs are needed because
Windows COM ports open with exclusive access (`dwShareMode=0`). Provision
additional pairs and set:

```bash
set ANYSERIAL_WINDOWS_PAIRS=COM50,COM51;COM52,COM53;COM54,COM55;...
```

The fanout test skips if fewer pairs are available than requested.

### Running

```bash
uv run pytest benchmarks/test_windows_throughput.py --benchmark-only
```

Or with JSON output:

```bash
uv run pytest benchmarks/test_windows_throughput.py --benchmark-only ^
    --benchmark-json=benchmarks/results/windows-current.json
```

### Notes

- com0com has ~1 ms minimum loopback latency (driver IRP turnaround) —
  timing assertions account for this floor.
- com0com does not enforce baud-rate throttling, so throughput numbers
  measure userland + driver overhead, not wire-speed saturation.
- The backend matrix is asyncio (ProactorEventLoop) and trio only —
  uvloop does not build on Windows.
- The fanout test requires one com0com pair per port (exclusive
  access). Set `ANYSERIAL_WINDOWS_PAIRS` with enough pairs; the test
  skips if fewer are available than requested.

## Hardware benchmarks

The pty-based benchmarks above measure the userland → kernel pty path,
which is enough to catch regressions in the readiness loop and the
config-apply pipeline. Hardware-bound numbers (FTDI, CP210x, CH340 with
real link rates) live as `tests/hardware/` opt-in tests today; they'll
move into `benchmarks/hardware/` once a self-hosted runner is wired up.

## Comparing two runs

`pytest-benchmark`'s built-in compare command works on the JSON files
this directory archives:

```bash
uv run pytest-benchmark compare benchmarks/results/*.json
```

## CI

The [bench.yml](../.github/workflows/bench.yml) workflow runs this suite
nightly at 04:00 UTC on `ubuntu-latest` against the kernel pty backend
(no real adapters — those need a self-hosted runner; see the comment at
the top of the workflow file for the wire-up path). Each run fetches the
most recent prior result as a baseline and surfaces the delta in the
job summary.

The regression gate is **advisory** for now — runner variance on shared
GHA hardware would make a hard 10% gate flap. Once we have ~10 baselines
to characterize the noise floor, the workflow will flip to
`--benchmark-compare-fail=mean:10%` per [DESIGN §26.1](../DESIGN.md#261-targets).

Manually trigger via `workflow_dispatch` (set the `heavy` input to also
include the 128-port fan-out scenario).

## Targets (DESIGN §26.1)

| Metric | Target |
|---|---|
| pty single-byte round-trip p50 | < 200 µs (asyncio + uvloop) |
| Throughput at 4 Mbaud, pty | ≥ 90% line rate |
| Allocation per `receive_into` | ~zero payload allocation |
| Cancellation latency | < 1 ms |
| Regression threshold | 10% from previous baseline |

These are *targets*; the docs report tracks the current measured numbers.
