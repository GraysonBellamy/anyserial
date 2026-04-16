# Performance

`anyserial`'s perf story is "low overhead on top of the kernel — anything
else is the wire's fault." The benchmarks below measure the parts we
control: the readiness loop, the configuration apply pipeline, and the
allocation profile of the receive path.

See [DESIGN §26](https://github.com/GraysonBellamy/anyserial/blob/main/DESIGN.md#26-performance-strategy)
and [§28](https://github.com/GraysonBellamy/anyserial/blob/main/DESIGN.md#28-benchmark-strategy)
for the full strategy and methodology.

## Targets vs. observed

Targets from [DESIGN §26.1](https://github.com/GraysonBellamy/anyserial/blob/main/DESIGN.md#261-targets):

| Metric                                    | Target                  | Observed (asyncio + uvloop)       | Status |
|-------------------------------------------|-------------------------|------------------------------------|--------|
| pty single-byte receive p50               | < 200 µs                | **99 µs**                          | ✅      |
| pty single-byte send p50                  | < 200 µs                | **101 µs**                         | ✅      |
| 64 KiB write throughput (pty)             | ≥ 90% line rate         | ≈110 MB/s effective                | ✅¹     |
| Syscall rate per `receive_available()`    | 1 `os.read` / call      | **1 `os.read` / call** (enforced)  | ✅²     |
| Allocation per `receive_into()` loop      | ~zero payload alloc     | < 16 KiB net for 200 calls         | ✅      |
| Allocation per `receive()` loop           | (reasonable headroom)   | < 256 KiB net for 200 calls        | ✅      |
| Allocation per `receive_available()` loop | (reasonable headroom)   | < 64 KiB net for 200 calls         | ✅      |
| Cancellation latency                      | < 1 ms                  | (covered by integration tests)     | ✅     |
| Regression threshold                      | 10% from baseline       | (advisory in CI today)             | 🟡³    |

¹ pty has no real link rate to throttle against; "throughput" here is
   the per-call cost of the write-then-drain loop. Hardware adapter
   numbers will land once a self-hosted runner is wired up.

² Enforced by
   [`tests/integration/test_receive_syscall_budget.py`](https://github.com/GraysonBellamy/anyserial/blob/main/tests/integration/test_receive_syscall_budget.py),
   which counts `read_nonblocking` invocations during a drain and fails
   if `receive_available` triggers more than one. Sibling sanity test
   confirms `receive(1)` still costs N syscalls for an N-byte burst —
   the whole reason `receive_available` exists.

³ The nightly bench job records a JSON baseline per run and surfaces the
   delta in the GitHub Actions job summary. The hard 10% gate flips on
   once we've characterized the GHA noise floor with ~10 baselines.

## First reference numbers

Recorded on a developer laptop (Intel Core Ultra 7 155H, 22 logical
cores, Linux 6.19, Python 3.13.13). Median of 200 rounds × 5 iterations
each via `pytest-benchmark.pedantic`. Numbers in microseconds (lower is
better):

### Single-byte latency (115 200 baud, pty)

| Backend           | Receive p50 | Receive max | Send p50 | Send max |
|-------------------|-------------|-------------|----------|----------|
| asyncio + uvloop  |   **99**    |    509      |   101    |    399   |
| asyncio (default) |     126     |    961      |   133    |    509   |
| trio              |     124     |    781      |   135    |    415   |

uvloop wins on median by 20–30%; asyncio's tail latency is more variable
because the default selector loop polls less aggressively.

### Bulk send throughput (pty, microseconds per call)

| Payload   | asyncio + uvloop | asyncio | trio  |
|-----------|------------------|---------|-------|
| 256 B     | **157**          | 179     | 187   |
| 4 KiB     | **156**          | 195     | 203   |
| 64 KiB    | **595**          | 631     | 664   |

Per-call overhead at 256 B and 4 KiB is essentially identical — the cost
is paying one `wait_writable` + one `os.write` regardless of payload.
At 64 KiB the kernel pty's 4 KiB buffer drives 16 partial writes, and
wall-time scales accordingly.

### Many-port fan-out (one round-trip per port, pty, microseconds)

| N ports | asyncio + uvloop | asyncio | trio  |
|---------|------------------|---------|-------|
| 8       | **307**          | 412     | 358   |
| 32      | **828**          | 1026    | 939   |

Sub-linear scale: 4× the ports take ≈2.7× the time, suggesting most of
the per-port cost overlaps inside the event loop's readiness wait.

### `receive_available` drain (single call + one-syscall drain)

| Queue depth | asyncio + uvloop | asyncio | trio  |
|-------------|------------------|---------|-------|
| 64 B        | **106**          | 134     | 142   |
| 1 KiB       | **111**          | 132     | 149   |
| 4 KiB       | **144**          | 329     | 170   |

Per-call cost is flat from 64 B to 1 KiB because the single `os.read`
that follows the readiness wake-up handles all queued bytes at once —
that's the DESIGN §26.1 syscall-budget target in action. At 4 KiB the
asyncio selector loop starts paying an extra round-trip through the
kernel queue (look at its max vs uvloop's max), which is why the
uvloop / trio numbers stay tight while asyncio's median jumps.

Compare against `receive(1)` called 64 times for a 64 B burst: that
path costs **≥64 `read_nonblocking` syscalls** by design (enforced by a
sibling integration test), so the effective latency per-byte is
≈2 µs × 64 = **≈128 µs total** versus `receive_available`'s **106 µs
for the whole burst** — a ~1.2× factor at this depth that grows with
queue size.

## Windows (com0com)

Windows numbers are published nightly from the
[`bench-windows`](https://github.com/GraysonBellamy/anyserial/blob/main/.github/workflows/bench.yml)
job on GitHub Actions `windows-latest` using a com0com virtual
COM-port pair. Target matrix from
[design-windows-backend.md §11](https://github.com/GraysonBellamy/anyserial/blob/main/docs/design-windows-backend.md):

| Scenario                                    | Target                              | Backend matrix |
|---------------------------------------------|-------------------------------------|----------------|
| Single-port round-trip, 1 B request/reply   | p99 ≤ 3× Linux p99 on same hardware | asyncio (Proactor) / trio |
| Throughput at 921600 baud, 4 KiB chunks     | ≥ 90% of pyserial-asyncio POSIX     | asyncio (Proactor) / trio |
| N-port fanout (8 / 32, optionally 128)      | No thread growth; linear CPU scale  | asyncio (Proactor) / trio |
| Open / close cycle                          | < 50 ms per cycle                   | asyncio (Proactor) / trio |

Measured numbers land in the CI job summary per run and in the
`bench-results-windows-py3.13-N` artifact (retention 90 days). The
uvloop column is absent — uvloop does not build on Windows.

A reference table will populate here once the nightly job has
accumulated enough baselines to publish stable medians; the
fundamental constraint is GHA Windows runner noise, which is higher
than the Linux runner's noise floor.

### Windows-specific caveats

- **com0com != real USB-serial.** Driver IRP turnaround adds ~1 ms of
  floor latency even on an otherwise-idle system. Real FTDI / CP210x
  adapters add more, and an FTDI adapter running its default 16 ms
  latency timer adds a lot more — see [Windows](windows.md#driver-specific-notes).
- **No uvloop.** The Windows matrix is asyncio (Proactor) and trio
  only. Per-backend comparisons against the Linux uvloop numbers are
  apples-to-oranges.
- **Proactor only.** The numbers don't include `SelectorEventLoop` —
  it's an explicit unsupported configuration (see
  [Windows / Supported runtimes](windows.md#supported-runtimes)).

## Methodology

Async tests are tricky to micro-benchmark — `anyio.run()` startup is
~50 ms, which would swamp any sub-millisecond workload if invoked per
iteration. Instead each benchmark holds one persistent event loop via
[`anyio.from_thread.start_blocking_portal`](https://anyio.readthedocs.io/en/stable/api.html#anyio.from_thread.start_blocking_portal),
and each timed iteration is a single `portal.call(coro_fn, *args)` —
round-trip overhead in the tens of µs.

The portal is parametrized across the same backend matrix the rest of
the test suite uses (asyncio default, asyncio + uvloop, trio). Each pty
pair is opened in raw mode (`cfmakeraw` on the follower fd, `O_NONBLOCK`
on the controller) so the kernel doesn't translate `\n` → `\r\n` or
buffer waiting for a newline.

See [`benchmarks/conftest.py`](https://github.com/GraysonBellamy/anyserial/blob/main/benchmarks/conftest.py)
and [`benchmarks/README.md`](https://github.com/GraysonBellamy/anyserial/blob/main/benchmarks/README.md)
for the full setup.

## Reproducing locally

```bash
make bench
```

Equivalent to:

```bash
uv sync --all-extras --group bench --group test
mkdir -p benchmarks/results
uv run pytest benchmarks/ --benchmark-only \
    --benchmark-json=benchmarks/results/$(git rev-parse --short HEAD).json
```

To compare two runs:

```bash
uv run pytest-benchmark compare benchmarks/results/*.json
```

## Caveats

- **Pty != real serial.** The kernel pty has no baud-rate throttling, so
  these numbers measure userland overhead, not link saturation.
- **GHA shared runners are noisy.** Numbers from the nightly job will
  vary ±20–30% between runs. Trust the median across multiple runs.
- **uvloop sometimes regresses.** It's optimized for sockets; serial
  fds use the same `wait_readable` / `wait_writable` plumbing, but the
  win isn't as large as it is for HTTP servers. Track both.
- **Hardware numbers will be different.** USB-serial adapters add a
  per-packet round-trip latency (FTDI's default `latency_timer` is 16
  ms — see [DESIGN §18](https://github.com/GraysonBellamy/anyserial/blob/main/DESIGN.md#18-low-latency-design)
  for how `low_latency=True` drops it to 1 ms).
- **`low_latency=True` is Linux-only.** macOS, BSD, and Windows have
  no equivalent kernel knob; the capability reads `UNSUPPORTED` and
  the request is routed through `UnsupportedPolicy` (see
  [macOS](darwin.md#low-latency-mode) / [BSD](bsd.md#low-latency-mode) /
  [Windows](windows.md#low-latency-mode)). The headline Linux numbers
  above were recorded with the low-latency knob engaged; the Windows
  com0com section uses defaults.
