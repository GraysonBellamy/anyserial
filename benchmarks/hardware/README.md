# Hardware benchmarks

Opt-in benchmarks that run against real serial hardware. Unlike the
pty-backed suite in the parent `benchmarks/` directory, these scripts
require a specific device on a specific path — they are reference
material for the "why does this library exist" pitch, not part of CI.

## What's here

| File | Purpose |
|---|---|
| [alicat_demo.py](alicat_demo.py) | Exercises the core anyserial API against a live Alicat MFC: firmware query, manufacturing info, data-frame layout, available-gas list, poll, tare, restore streaming. A small end-to-end smoke test. |
| [alicat_benchmark.py](alicat_benchmark.py) | The case study that produced the numbers in [docs/performance.md](../../docs/performance.md#hardware-case-study-alicat-mfc). Round-trip, cancellation, streaming, and fan-out scaling — anyserial async / sync / pyserial / pyserial-asyncio. |

The Alicat command strings used in these scripts were taken from the
vendor's public "Serial Communications Primer" PDF — not redistributed
here; grab it from [alicat.com/support](https://www.alicat.com/support/)
if you need the full command reference.

## Test rig (what the published numbers used)

- **Device**: Alicat MCR-200SLPM-D mass flow controller, firmware
  8v17.0-R23, gas N2.
- **Adapter**: Prolific PL2303 USB-to-serial (no FTDI `latency_timer`
  sysfs support; no `ASYNC_LOW_LATENCY` effect in practice).
- **Link**: 115200-8N1, unit id `@` (streaming) — the scripts stop
  streaming + assign unit `A` for polling, then restore streaming on
  exit.
- **Host**: Linux 6.19, Python 3.13, anyio 4.13.

## Running

```bash
# Smoke test — one poll + query of every basic command.
uv run python benchmarks/hardware/alicat_demo.py

# Full benchmark — ~2 minutes end to end, leaves device as found.
uv run python benchmarks/hardware/alicat_benchmark.py

# Subset — any combination of roundtrip / cancel / stream / fanout.
uv run python benchmarks/hardware/alicat_benchmark.py fanout
```

Needs read-write on `/dev/ttyUSB0`. If you see `PermissionError`,
either `sudo chmod a+rw /dev/ttyUSB0` for the session or add yourself
to the `uucp` / `dialout` group (distro-dependent) and re-login.

## Reproducing with a different device

Everything except the Alicat command strings is reusable. To adapt
against another device:

1. Change `POLL_CMD`, `UNIT`, `POLL_CMD`, and the `@@`/`@ @`
   stop/start-streaming sequences to whatever your protocol uses.
2. If your device isn't line-framed on `\r`, change the `b"\r" in
   chunk` checks accordingly.
3. Fan-out uses pty pairs with synthetic echo bots — no device change
   needed there.

## Key findings (full writeup in [docs/performance.md](../../docs/performance.md#hardware-case-study-alicat-mfc))

- **Single-device request/response**: anyserial within ~100 µs of
  pyserial at p50 (5.52 ms vs 5.61 ms on this rig). The ~500 µs gap
  visible in portal-wrapped benchmarks is the `portal.call` thread hop,
  not library overhead.
- **Cancellation p99 < 1 ms** on every async backend (asyncio, uvloop,
  trio) — meets [DESIGN §26.1](../../DESIGN.md#261-targets) against
  real USB.
- **Fan-out at N=16 concurrent devices**: 6.2× faster than
  thread-per-port pyserial. This is the architectural payoff the
  library exists to deliver.
- **`BufferedByteReceiveStream` is free** — no measurable overhead vs a
  hand-rolled `receive(128)` loop with CR detection. Use it for
  line-framed protocols; see [docs/quickstart.md](../../docs/quickstart.md).
