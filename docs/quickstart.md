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

For higher-level framing, wrap the port in
`anyio.streams.buffered.BufferedByteStream`:

```python
from anyio.streams.buffered import BufferedByteStream

async with await open_serial_port("/dev/ttyUSB0", config) as port:
    buffered = BufferedByteStream(port)
    line = await buffered.receive_until(b"\n", max_bytes=1024)
```

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
