# anyserial

Low-latency async serial I/O for Python, built on [AnyIO](https://anyio.readthedocs.io/).

!!! warning "Pre-release"
    `anyserial` is in active development. The API is not yet stable.

## Features

- **Async-native** via AnyIO — asyncio, uvloop, trio.
- **Python 3.13+**, tested on 3.13 and 3.14.
- **POSIX first-class** (Linux, macOS, BSD) via `SyncSerialBackend`;
  **Windows first-class** via `AsyncSerialBackend` with native IOCP
  dispatch.
- **Low-latency by design** — O(μs) per syscall, O(ms) pty round-trips.
- **Raw bytes only** — compose with
  `anyio.streams.buffered.BufferedByteStream` for framing.
- **Explicit capabilities**, runtime reconfiguration, RS-485 on Linux.
- **Sync wrapper** for scripts and test benches.

## Install

```bash
uv add anyserial
# or
pip install anyserial
```

Optional extras:

```bash
uv add "anyserial[uvloop]"              # faster asyncio loop on POSIX
uv add "anyserial[trio]"                # trio support
uv add "anyserial[discovery-pyudev]"    # Linux udev-sourced metadata
uv add "anyserial[discovery-pyserial]"  # cross-platform discovery fallback
```

## 30-second tour

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

Scripts that don't want an event loop can use the sync wrapper:

```python
from anyserial.sync import SerialPort

with SerialPort.open("/dev/ttyUSB0", baudrate=115_200) as port:
    port.send(b"AT\r\n")
    print(port.receive(64, timeout=1.0))
```

## Where to next

- [Quickstart](quickstart.md) — open a port, send, receive, test.
- [Configuration](configuration.md) — every `SerialConfig` field.
- [Capabilities](capabilities.md) — what `SUPPORTED` / `UNKNOWN` /
  `UNSUPPORTED` mean and how to gate features on them.
- [Cancellation](cancellation.md) — how `SerialPort` composes with
  AnyIO cancel scopes.
- [Runtime reconfiguration](runtime-reconfiguration.md),
  [RS-485](rs485.md),
  [Discovery](discovery.md).

### Runtime

- [AnyIO backend selection](anyio-backends.md) — asyncio vs. uvloop
  vs. trio.
- [uvloop usage](uvloop.md) — when it helps, when it doesn't.
- [Performance](performance.md) — targets, measured numbers,
  methodology.

### Platforms

- [Linux tuning](linux-tuning.md) — permissions, `low_latency`,
  custom baud, udev rules.
- [macOS](darwin.md) — `IOSSIOSPEED`, IOKit discovery,
  unsupported-feature routing.
- [BSD](bsd.md) — FreeBSD / NetBSD / OpenBSD / DragonFly; best-effort.
- [Windows](windows.md) — `AsyncSerialBackend`, overlapped I/O,
  ProactorEventLoop requirement, SetupAPI discovery.

### Development

- [Sync wrapper](sync.md) — blocking `SerialPort` for scripts.
- [Hardware testing](hardware-testing.md) — opt-in markers, FTDI
  loopback wiring.
- [Troubleshooting](troubleshooting.md) — permission errors, EINVAL
  on baud, exclusive-access conflicts.
- [Migration from pySerial](migration-from-pyserial.md) — API
  mapping and behaviour diffs.

## Status

See the [changelog](changelog.md) and the full [design plan](design.md).
