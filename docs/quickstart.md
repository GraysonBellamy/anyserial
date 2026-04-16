# Quickstart

Open a port, round-trip some bytes, clean up cleanly. Every snippet
below is runnable against a real adapter on Linux or macOS, and every
one works under `asyncio`, `uvloop`, or `trio` without modification.

If you don't have hardware handy, jump to [Testing without
hardware](#testing-without-hardware) — the `MockBackend` gives you a
connected pair of ports entirely in memory.

## Prerequisites

```bash
uv add anyserial
```

Python 3.13 or 3.14, POSIX host (Linux, macOS, or BSD). On Linux the
user running the process needs read/write on the device node —
typically via membership in `dialout` or `uucp`. See
[Linux tuning](linux-tuning.md#permissions) for the details.

## Open, send, receive

```python
import anyio
from anyserial import SerialConfig, open_serial_port


async def main() -> None:
    config = SerialConfig(baudrate=115_200)
    async with await open_serial_port("/dev/ttyUSB0", config) as port:
        await port.send(b"AT\r\n")
        reply = await port.receive(64)
        print(reply)


anyio.run(main)
```

- `open_serial_port` returns an awaitable `SerialPort`.
- `async with` guarantees close on exit — even under cancellation.
- `send` writes every byte (handles partial writes internally).
- `receive(max_bytes)` returns as soon as **any** bytes are available,
  up to `max_bytes`. It never returns `b""`; a clean EOF raises
  `SerialDisconnectedError`.

## `SerialPort.open` shortcut

When you don't need a separate `SerialConfig` instance, pass the
config fields directly:

```python
async with await SerialPort.open("/dev/ttyUSB0", baudrate=115_200) as port:
    await port.send(b"ping\n")
```

Equivalent to `open_serial_port("/dev/ttyUSB0", SerialConfig(baudrate=115_200))`.

## Reading a fixed-length frame

`receive` returns what the kernel has available; it is not a
"read exactly N bytes" call. Loop until you have what you need:

```python
async def read_exact(port: SerialPort, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        buf.extend(await port.receive(n - len(buf)))
    return bytes(buf)
```

## Line-framed protocols with `BufferedByteStream`

Most serial protocols terminate each message with a known byte — `\r`,
`\n`, `\r\n`, `\x03`, etc. Rather than hand-rolling a loop that keeps
calling `receive` and checking for the delimiter, wrap the port in
AnyIO's [`BufferedByteStream`][bbs]:

```python
from anyio.streams.buffered import BufferedByteStream

async with await open_serial_port("/dev/ttyUSB0", config) as port:
    buffered = BufferedByteStream(port)
    await buffered.send(b"AT\r")
    reply = await buffered.receive_until(b"\r", max_bytes=512)
```

### What it does

- **`receive_until(delimiter, max_bytes)`** reads until the delimiter
  appears, returns everything before it (the delimiter itself is
  consumed but not returned), and keeps any bytes that arrived *after*
  the delimiter in an internal buffer for the next call. No "partial
  read spans two `receive()` calls" bookkeeping to get wrong.
- **`receive_exactly(n)`** does the same trick for fixed-width frames:
  blocks until exactly `n` bytes are buffered, returns them as a
  single `bytes` object.
- **`send`** passes through unchanged to the wrapped port — the
  wrapper is full-duplex, so you don't juggle two objects.

### Advantages

- **Idiomatic AnyIO.** Reads like protocol code, not I/O plumbing.
- **Correct by construction.** Handles the delimiter-straddling edge
  case (delimiter arrives at the boundary of two `receive()` calls)
  without any extra work in your code.
- **No measurable overhead.** Benchmarked against a hand-rolled
  `receive(128)` + `b"\r" in chunk` loop on real USB hardware, the
  wrapper is indistinguishable at p50 and p99
  ([case study](performance.md#hardware-case-study-alicat-mfc)). Use
  it — there's no performance reason not to.
- **Raises `DelimiterNotFound` if the buffer fills** before the
  delimiter arrives — a bounded failure mode instead of an unbounded
  allocation.

### Disadvantages / caveats

- **Requires a single reader.** The wrapper owns the buffer; two
  concurrent `receive_until` callers on the same wrapper will
  interleave bytes incoherently. This matches the underlying
  `SerialPort`'s `ResourceGuard` (concurrent reads already raise
  `BusyResourceError`), so it's not a new constraint — just don't
  split a buffered stream across tasks.
- **Bytes already buffered are lost if you discard the wrapper.**
  Create the wrapper once, use it for the lifetime of the port. If
  you need raw `receive()` access alongside, call it on the wrapped
  port directly *before* the wrapper consumes data.
- **Not helpful if your framing is not delimiter-based.** Length-
  prefixed frames that don't have a fixed width, or protocols with
  escape sequences that change framing mid-message, still need custom
  parsing on top of `receive_available` or `receive(n)`.

[bbs]: https://anyio.readthedocs.io/en/stable/streams.html#buffered-byte-streams

## Bounding a read with a timeout

Use an AnyIO cancel scope — `SerialPort` honours cancellation
natively:

```python
with anyio.move_on_after(1.0):
    reply = await port.receive(64)
else:
    # 1 s elapsed before any bytes arrived.
    ...
```

See [Cancellation](cancellation.md) for the full semantics.

## Draining the receive queue in one call

When a device burst-writes a whole response, `receive_available`
returns every queued byte in a single syscall, instead of one
`receive` per chunk:

```python
await port.send(b"QUERY\r\n")
# One wait_readable + one os.read regardless of how many bytes arrived.
response = await port.receive_available(limit=4096)
```

See [Performance](performance.md#receive_available-drain-single-call--one-syscall-drain)
for the syscall-budget rationale.

## Fan-out: reading from many ports at once

The architectural win over sync / thread-per-port libraries: one event
loop handles N ports concurrently with one OS thread. Open each port
in its own task and collect the results through a shared dict:

```python
import anyio
from anyserial import SerialConfig, open_serial_port


async def poll_one(path: str, results: dict[str, bytes]) -> None:
    async with await open_serial_port(path, SerialConfig(baudrate=115_200)) as port:
        await port.send(b"A\r")
        results[path] = await port.receive(256)


async def main() -> None:
    paths = ["/dev/ttyUSB0", "/dev/ttyUSB1", "/dev/ttyUSB2"]
    results: dict[str, bytes] = {}
    async with anyio.create_task_group() as tg:
        for p in paths:
            tg.start_soon(poll_one, p, results)
    for path, frame in results.items():
        print(path, frame)


anyio.run(main)
```

If any task raises, the task group cancels the others and re-raises
the exception group. To keep one port's failure from taking down the
others, wrap each call site in a cancel-safe catch:

```python
async def poll_one(path: str, results: dict[str, bytes | Exception]) -> None:
    try:
        async with await open_serial_port(path, SerialConfig(baudrate=115_200)) as port:
            await port.send(b"A\r")
            results[path] = await port.receive(256)
    except Exception as exc:  # noqa: BLE001 — record per-port failure
        results[path] = exc
```

Scaling numbers vs. thread-per-port pyserial are in the
[hardware case study](performance.md#hardware-case-study-alicat-mfc)
— 6.4× faster at N=4 devices, 6.2× at N=16, on pty-backed peers.
Real-USB numbers will depend on the adapters but show the same
scaling shape: anyserial stays flat per-port; threaded approaches
climb linearly due to GIL contention.

## Changing settings mid-session

`SerialConfig` is frozen; derive new configs with `with_changes` and
hand them to `configure()`:

```python
await port.configure(port.config.with_changes(baudrate=1_000_000))
```

See [Runtime reconfiguration](runtime-reconfiguration.md) for the
concurrency and failure-semantics details.

## Discovering ports

```python
from anyserial import find_serial_port, list_serial_ports


async def main() -> None:
    for info in await list_serial_ports():
        print(info.device, info.vid, info.pid, info.serial_number)

    ftdi = await find_serial_port(vid=0x0403, pid=0x6001)
    if ftdi is None:
        raise RuntimeError("FT232R not connected")
    async with await open_serial_port(ftdi.device) as port:
        ...
```

See [Discovery](discovery.md) for backends, filters, and platform
coverage.

## Testing without hardware

The `MockBackend` gives you a connected pair of in-memory ports —
bytes written to one are readable from the other. Use it to drive
protocol-level unit tests without touching a device:

```python
import anyio
from anyserial.testing import serial_port_pair


async def main() -> None:
    a, b = serial_port_pair()
    try:
        await a.send(b"hello")
        assert await b.receive(5) == b"hello"
    finally:
        await a.aclose()
        await b.aclose()


anyio.run(main)
```

`serial_port_pair` exposes the same `SerialPort` surface as a real
device. The `anyserial.testing` module also exports `MockBackend` and
`FaultPlan` for fault-injection tests — see the module docstring.

## Sync wrapper

If the caller is not async, use the blocking wrapper:

```python
from anyserial.sync import SerialPort

with SerialPort.open("/dev/ttyUSB0", baudrate=115_200) as port:
    port.send(b"AT\r\n")
    reply = port.receive(64, timeout=1.0)
```

See [Sync wrapper](sync.md) for portal configuration, per-call
timeouts, and the async/sync decision table.

## Next steps

- Tune every field on the config → [Configuration](configuration.md).
- Understand the tri-state capability model →
  [Capabilities](capabilities.md).
- Pick a runtime → [AnyIO backend selection](anyio-backends.md).
- Diagnose failures → [Troubleshooting](troubleshooting.md).
