# Hardware testing

Most of `anyserial`'s test suite runs against in-memory fakes and
kernel pseudo terminals — hermetic, fast, and runnable in any CI.
A small opt-in hardware suite exercises the paths that only a real
adapter exposes: FTDI discovery by VID/PID, FTDI `low_latency` with
the sysfs timer, and RS-485 round-trip through `TIOCSRS485`.

See [DESIGN §27.5](https://github.com/GraysonBellamy/anyserial/blob/main/DESIGN.md#275-hardware-tests)
for the marker design.

## The marker

Hardware tests are tagged with `@pytest.mark.hardware`. They're
**default-deselected** via `-m "not hardware"` in
[pyproject.toml](https://github.com/GraysonBellamy/anyserial/blob/main/pyproject.toml).
Running the normal test command picks up everything *except* the
hardware suite:

```bash
uv run pytest                   # skips hardware tests
uv run pytest -m hardware       # only the hardware tests
uv run pytest -m ""             # both (override the default deselect)
```

Collection still walks the hardware tree on every run so broken
syntax, imports, or fixture wiring fail fast — only execution is
gated.

## Required environment variables

Each hardware test looks for one or more env vars and skips cleanly
if none are set. Start by picking the adapter you have:

| Env var | Used by | What it points at |
|---|---|---|
| `ANYSERIAL_TEST_PORT` | `tests/hardware/test_discovery_ftdi.py`, `test_ftdi_low_latency.py` | The `/dev/ttyUSB*` or `/dev/cu.*` path of a connected adapter |
| `ANYSERIAL_TEST_VID` | `test_discovery_ftdi.py` | VID override when filtering by device (defaults to FTDI `0x0403`) |
| `ANYSERIAL_TEST_PID` | `test_discovery_ftdi.py` | PID override (defaults to `0x6001`) |
| `ANYSERIAL_RS485_PORT` | `test_rs485_adapter.py` | Path of an adapter whose driver implements `TIOCSRS485` |

Example — full hardware pass against a single FTDI adapter that also
speaks RS-485:

```bash
export ANYSERIAL_TEST_PORT=/dev/ttyUSB0
export ANYSERIAL_RS485_PORT=/dev/ttyUSB0
uv run pytest -m hardware
```

Unset variables mean the corresponding tests skip. Running with none
set is effectively a no-op.

## Loopback wiring

The FTDI latency test and the discovery test do **not** need a
loopback cable — they verify sysfs metadata and the driver's
`latency_timer` knob without moving bytes. The RS-485 adapter test
writes to `struct serial_rs485` and reads it back; also no wire
traffic.

For your own tests that send bytes, a TX↔RX self-loopback on a
DE-9 connector is enough:

```
  FTDI DB9     jumper
 ┌───────────┐
 │  1  DCD   │
 │  2  RX  ──┼─┐
 │  3  TX  ──┼─┤   bytes written to TX are read from RX
 │  4  DTR   │ │
 │  5  GND   │ │
 │  6  DSR   │ │
 │  7  RTS ──┼─┤
 │  8  CTS ──┼─┘   optional: also loops RTS↔CTS for hardware flow
 │  9  RI    │
 └───────────┘
```

Many FTDI breakout boards expose the same pins on a header. For
RS-485 adapters, self-loopback isn't meaningful — RS-485 needs a
second node on the bus. A pair of USB-to-RS-485 adapters wired A↔A
and B↔B across a short twisted pair is the canonical setup.

## What each test covers

- **`test_discovery_ftdi.py`** — `list_serial_ports()` and
  `find_serial_port(vid=..., pid=...)` return the adapter pointed
  at by `ANYSERIAL_TEST_PORT`. Covers the Linux sysfs walker
  end-to-end against real USB metadata.
- **`test_ftdi_low_latency.py`** — opening with
  `low_latency=True` drops the FTDI sysfs `latency_timer` from
  16 ms to 1 ms and restores it on close. Also confirms
  `ASYNC_LOW_LATENCY` round-trips via `TIOCSSERIAL`.
- **`test_rs485_adapter.py`** — writing `RS485Config` via
  `configure()` round-trips through `TIOCGRS485` (kernel hands
  back the exact state), and restores the pre-touch state on
  close. Adapters whose driver returns `ENOTTY` for
  `TIOCSRS485` skip with a descriptive message.

All three pass against a genuine FTDI FT232R on Linux 6.x; behaviour
on clones varies by driver.

## Running inside CI

The default CI matrix does not run hardware tests. Two ways to wire
them in:

**Self-hosted runner with an adapter attached.** Install the uv
environment, plug in an adapter, set `ANYSERIAL_TEST_PORT`, and
invoke `uv run pytest -m hardware`. This is the path recommended
for [Performance](performance.md) publishing once a runner is
available — the benchmark suite uses the same marker.

**Developer-local smoke run.** Before sending a PR that touches
discovery or low-latency code:

```bash
ANYSERIAL_TEST_PORT=/dev/ttyUSB0 uv run pytest -m hardware tests/hardware/
```

Expected wall time on an FTDI adapter: <2 s.

## macOS and BSD

The same markers apply on macOS (`/dev/cu.usbserial-*`) and
FreeBSD (`/dev/cuaU0`, etc.). Driver support for `TIOCSRS485` and
the `latency_timer` knob is platform-specific:

- **macOS** — `low_latency` and `rs485` are routed through
  `UnsupportedPolicy` (no kernel equivalent). The discovery test
  works against IOKit metadata; the FTDI latency test and RS-485
  test skip on macOS.
- **BSD** — `low_latency` is always unsupported; `rs485` is
  out of scope. The discovery test works against
  `/dev` scan patterns but USB metadata is `None` — use
  `backend="pyserial"` if your test needs VID/PID. See
  [BSD](bsd.md).

## Writing new hardware tests

Follow the existing pattern:

```python
import os
import pytest


_ENV = "ANYSERIAL_TEST_PORT"


pytestmark = pytest.mark.hardware


@pytest.fixture
def device_path() -> str:
    path = os.environ.get(_ENV)
    if not path:
        pytest.skip(f"set {_ENV} to enable this hardware test")
    return path


async def test_round_trip(device_path: str) -> None:
    ...
```

Skip paths are as important as assertions — a hardware test that
fails noisily on a machine without hardware is worse than one that
skips.

## See also

- [Linux tuning](linux-tuning.md) — kernel and sysfs knobs exercised
  by the hardware tests.
- [Performance](performance.md) — where the hardware numbers land
  when a runner is wired up.
- [RS-485](rs485.md) — what the RS-485 hardware test actually
  asserts.
