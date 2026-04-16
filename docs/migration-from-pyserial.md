# Migration from pySerial

[pySerial](https://pyserial.readthedocs.io/) is the default Python
serial library and an obvious starting point. `anyserial` is a
ground-up rewrite with a different shape — async-first, explicit
capabilities, frozen configs, typed attributes — so the migration
isn't a one-line import swap. This page is the mapping guide and
the catalogue of behaviour differences that matter in real code.

See [DESIGN §2](https://github.com/GraysonBellamy/anyserial/blob/main/DESIGN.md#2-relationship-to-pyserial)
for the rationale behind rewriting rather than forking.

## What changes conceptually

| pySerial | anyserial |
|---|---|
| Blocking API with optional async variant (`pyserial-asyncio`) | Async-first; sync wrapper (`anyserial.sync`) is a thin portal |
| Mutable `Serial` with property setters | Frozen `SerialConfig`; reconfigure via `port.configure(new_config)` |
| Boolean capability hints on the class | Tri-state `Capability` per feature |
| Module-level baud / byte-size constants | `StrEnum`s (`ByteSize.EIGHT`, `Parity.NONE`, `StopBits.ONE`) |
| `Serial(port="/dev/...", ...)` opens at construction | `await open_serial_port(path, config)` — construction is lazy |
| Partial writes returned as int | `send` always writes every byte |
| No cancellation | Full AnyIO cancel-scope support |

## Opening a port

**pySerial**

```python
import serial

ser = serial.Serial(
    port="/dev/ttyUSB0",
    baudrate=115_200,
    bytesize=serial.EIGHTBITS,
    parity=serial.PARITY_NONE,
    stopbits=serial.STOPBITS_ONE,
    timeout=1.0,
    rtscts=True,
)
```

**anyserial** (async)

```python
import anyio
from anyserial import FlowControl, SerialConfig, open_serial_port


async def main() -> None:
    config = SerialConfig(
        baudrate=115_200,
        flow_control=FlowControl(rts_cts=True),
    )
    async with await open_serial_port("/dev/ttyUSB0", config) as port:
        ...


anyio.run(main)
```

**anyserial** (sync wrapper)

```python
from anyserial import FlowControl, SerialConfig
from anyserial.sync import SerialPort

config = SerialConfig(baudrate=115_200, flow_control=FlowControl(rts_cts=True))
with SerialPort.open("/dev/ttyUSB0", config) as port:
    ...
```

Note that `timeout=` is **not** on `SerialConfig`. In async code, use
`anyio.fail_after` / `anyio.move_on_after`; in sync code, pass
`timeout=` per method call. See [Cancellation](cancellation.md).

## Constants

pySerial exposes module-level constants — `PARITY_NONE`,
`EIGHTBITS`, `STOPBITS_ONE`. `anyserial` uses `StrEnum` classes,
which compare equal to their string form. If you need a drop-in
mapping:

```python
from anyserial import ByteSize, Parity, StopBits

# pyserial.PARITY_NONE == "N"; anyserial.Parity.NONE == "none"
# These are NOT interchangeable strings. Use the enum directly.
```

There is no compatibility shim that re-exports pySerial's constant
names. The migration cost is one import swap and one enum lookup per
field, and the explicit spelling pays off in error messages and
type-checker coverage.

## API surface mapping

### Read / write

| pySerial | anyserial (async) | Notes |
|---|---|---|
| `ser.read(n)` | `await port.receive(n)` | Returns as soon as any bytes are available. Never returns `b""`; EOF raises `SerialDisconnectedError`. |
| `ser.read_all()` | `await port.receive_available(limit=...)` | One readiness wakeup, one `os.read`. |
| `ser.read_until(delim)` | `BufferedByteStream(port).receive_until(delim, max_bytes=...)` | Use `anyio.streams.buffered`. |
| `ser.in_waiting` | `port.input_waiting()` | Non-awaiting snapshot. |
| `ser.write(data)` | `await port.send(data)` | Returns `None`; writes every byte. |
| `ser.write(data)` (partial write return) | — | `anyserial` loops internally; use `receive_into` for zero-copy reads. |
| `ser.out_waiting` | `port.output_waiting()` | Non-awaiting snapshot. |
| `ser.flush()` | `await port.drain()` | Async poll on `TIOCOUTQ`. Use `await port.drain_exact()` for true `tcdrain` (FIFO). |

### Control / modem lines

| pySerial | anyserial | Notes |
|---|---|---|
| `ser.rts = True` | `await port.set_control_lines(rts=True)` | Leaves DTR unchanged when `dtr=None`. |
| `ser.dtr = True` | `await port.set_control_lines(dtr=True)` | Same. |
| `ser.cts`, `ser.dsr`, `ser.ri`, `ser.cd` | `await port.modem_lines()` | Returns a single `ModemLines` snapshot. |
| `ser.send_break(duration)` | `await port.send_break(duration=0.25)` | BREAK is always de-asserted in `finally`, even on cancel. |

### Buffers

| pySerial | anyserial |
|---|---|
| `ser.reset_input_buffer()` | `await port.reset_input_buffer()` |
| `ser.reset_output_buffer()` | `await port.reset_output_buffer()` |

### Lifecycle

| pySerial | anyserial (async) | anyserial (sync) |
|---|---|---|
| `ser.close()` | `await port.aclose()` | `port.close()` |
| `ser.is_open` | `port.is_open` | `port.is_open` |
| `with serial.Serial(...) as ser:` | `async with await open_serial_port(...) as port:` | `with SerialPort.open(...) as port:` |

### Reconfiguration

pySerial mutates the instance:

```python
ser.baudrate = 1_000_000
ser.apply_settings({"baudrate": 1_000_000})
```

anyserial derives a new config and applies it atomically:

```python
await port.configure(port.config.with_changes(baudrate=1_000_000))
```

See [Runtime reconfiguration](runtime-reconfiguration.md).

## Timeouts

pySerial has a per-port `timeout` (read) and `write_timeout` (write).
`anyserial` uses AnyIO cancel scopes instead, which are composable
and survive nesting:

```python
# pySerial
ser.timeout = 0.5
data = ser.read(64)   # returns early on timeout

# anyserial async
import anyio

with anyio.move_on_after(0.5):
    data = await port.receive(64)

# anyserial sync wrapper
data = port.receive(64, timeout=0.5)  # raises TimeoutError on expiry
```

See [Cancellation](cancellation.md).

## Port discovery

pySerial ships `serial.tools.list_ports` — `anyserial` can route
through it as a fallback, or use its own native walker:

```python
from anyserial import find_serial_port, list_serial_ports

# Native (Linux sysfs, macOS IOKit, BSD /dev scan).
ports = await list_serial_ports()

# pySerial fallback — useful on BSD when you need VID/PID metadata.
ports = await list_serial_ports(backend="pyserial")

# Filter by VID/PID.
ftdi = await find_serial_port(vid=0x0403, pid=0x6001)
```

See [Discovery](discovery.md).

## `pyserial-asyncio` comparison

`pyserial-asyncio` wraps pySerial in `loop.add_reader` / `add_writer`
hooks. `anyserial` uses the same underlying primitives but exposes
them through AnyIO, so:

- Same event-loop integration on `asyncio`.
- Works unchanged under `uvloop` and `trio` without a second adapter.
- First-class cancellation and typed attributes.
- Native discovery instead of `pyserial.tools.list_ports`.

Head-to-head numbers on pty and on a real Alicat MFC over USB are in
[performance.md](performance.md) and the
[hardware case study](performance.md#hardware-case-study-alicat-mfc).

## Where each library wins (honestly)

The performance picture is more nuanced than "anyserial is always
faster" — here's the unvarnished version for you to match against
your workload:

| Workload                                     | Recommendation |
|----------------------------------------------|----------------|
| Single device, one-shot request/response     | **Either.** On real USB hardware the p50 gap between pyserial and pure-async anyserial is ≤100 µs. Use what fits your codebase. |
| Line-framed single device                    | **anyserial with [`BufferedByteStream`](quickstart.md#line-framed-protocols-with-bufferedbytestream)** for cleaner code; no performance cost. |
| Deadline-bounded reads / cancellable I/O     | **anyserial.** p99 cancellation latency < 1 ms on real USB; pyserial has no true cancellation, only blocking reads with `timeout=`. |
| Mixing serial with network / file I/O        | **anyserial.** One event loop handles all of it. Doing this with pyserial means a thread. |
| Many-device fan-out (N ≥ 4 ports concurrent) | **anyserial.** 6× faster than thread-per-port pyserial on the benchmark rig; scales flat per-port while threads grow with GIL contention. |
| You already have a large sync pyserial codebase and it works | **Stay on pyserial** until you actually need async or cancellation. `anyserial` isn't a drop-in upgrade. |
| You need pyserial-specific features (RFC 2217, specific obscure adapters) | **Stay on pyserial** — `anyserial`'s platform coverage is narrower. |

The case study at
[performance.md#hardware-case-study-alicat-mfc](performance.md#hardware-case-study-alicat-mfc)
walks through the numbers on a live Alicat MFC over a Prolific USB
adapter. TL;DR:

- **Single-device p50**: pyserial 5.61 ms, anyserial async 5.52 ms —
  within ~100 µs.
- **Cancellation p99**: all anyserial async backends < 1 ms;
  pyserial-asyncio comparable; pyserial's `timeout=` at 449 µs but
  measures timeout-on-block, not true cancel.
- **Fan-out N=16**: anyserial 84 ms; pyserial threaded 520 ms.

## Behavioural differences

Worth calling out explicitly:

- **`receive()` never returns `b""`.** A clean EOF raises
  `SerialDisconnectedError`. Code that relied on `b""` as an EOF
  sentinel needs to handle the exception instead.
- **`send()` returns `None`.** No partial-byte-count to check.
- **Exclusive access is `flock`.** pySerial uses a `fcntl` advisory
  lock too; the behaviour is equivalent, but the config spelling is
  `SerialConfig(exclusive=True)`.
- **BREAK is cancellable.** pySerial's `send_break` blocks for its
  duration; `anyserial`'s sleeps in AnyIO and de-asserts in a
  `finally` so cancellation still ends with BREAK off.
- **Discovery is async.** `list_serial_ports` is a coroutine — in
  sync contexts call it via the sync wrapper or wrap it in
  `anyio.run`.

## Incremental migration

One codebase can run both libraries side by side during the
transition — `pyserial` on the paths you haven't migrated yet,
`anyserial` where you need async or the new features:

```python
# Still using pyserial here…
import serial
legacy = serial.Serial("/dev/ttyUSB0", 9600)

# …and anyserial for the new hot path.
async with await open_serial_port("/dev/ttyUSB1", SerialConfig()) as fast:
    ...
```

They don't conflict — different imports, different devices. Move one
code path at a time.

## See also

- [Quickstart](quickstart.md) — the 30-second tour end-to-end.
- [Configuration](configuration.md) — every `SerialConfig` field.
- [Cancellation](cancellation.md) — timeouts without `timeout=`.
- [Discovery](discovery.md) — native vs. pyserial / pyudev backends.
