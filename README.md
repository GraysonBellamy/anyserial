# anyserial

Async-native serial I/O for Python, built on [AnyIO](https://anyio.readthedocs.io/).

[![CI](https://github.com/GraysonBellamy/anyserial/actions/workflows/ci.yml/badge.svg)](https://github.com/GraysonBellamy/anyserial/actions/workflows/ci.yml)
[![PyPI version](https://img.shields.io/pypi/v/anyserial.svg)](https://pypi.org/project/anyserial/)
[![Python versions](https://img.shields.io/pypi/pyversions/anyserial.svg)](https://pypi.org/project/anyserial/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

> [!WARNING]
> **Alpha.** The API is not yet stable and may change between minor versions. See [DESIGN.md](DESIGN.md) for the architecture and design rationale.

## Overview

`anyserial` is a ground-up async serial transport. It runs on top of AnyIO, so the same code works under `asyncio`, `uvloop`, or `trio` without changes, and it exposes a thin blocking wrapper for scripts and test benches that don't want an event loop.

The focus is low-latency, predictable I/O against real hardware:

- **Async-native.** Readiness-driven I/O on nonblocking file descriptors — no worker threads on the POSIX hot path.
- **POSIX first-class.** Linux, macOS, and BSD; Windows via IOCP (trio and `ProactorEventLoop`).
- **Explicit capabilities.** Features that a given platform or adapter can't support fail loudly at configure time — no silent emulation.
- **Raw bytes only.** Framing belongs in user code or downstream libraries; compose with `anyio.streams.buffered.BufferedByteStream` for line- or delimiter-based reads.
- **Immutable, typed config.** Frozen dataclasses, strict type checking, full PEP 561 support.
- **Runtime reconfiguration.** Change baud, parity, or flow control on an open port without reopening.
- **RS-485, low-latency mode, custom baud rates** where the platform exposes them.

## Requirements

- Python 3.13 or 3.14
- Linux, macOS, BSD, or Windows
- `anyio >= 4.13`

## Installation

```bash
uv add anyserial
# or
pip install anyserial
```

Optional extras:

```bash
uv add "anyserial[uvloop]"              # Linux/macOS uvloop event loop
uv add "anyserial[winloop]"             # Windows winloop event loop
uv add "anyserial[trio]"                # trio runtime
uv add "anyserial[discovery-pyudev]"    # richer Linux port discovery
uv add "anyserial[discovery-pyserial]"  # pyserial-based discovery fallback
```

## Usage

### Async

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

`receive(max_bytes)` returns as soon as any bytes are available; a clean EOF raises `SerialDisconnectedError`. `send` handles partial writes internally. Use an AnyIO cancel scope to bound a read:

```python
with anyio.move_on_after(1.0):
    reply = await port.receive(64)
```

### Sync

```python
from anyserial.sync import SerialPort

with SerialPort.open("/dev/ttyUSB0", baudrate=115_200) as port:
    port.send(b"ping\n")
    reply = port.receive(1024, timeout=1.0)
```

The sync wrapper is backed by a process-wide `anyio.from_thread.BlockingPortalProvider`; every blocking call accepts an optional `timeout=`.

### Discovery

```python
from anyserial import find_serial_port, list_serial_ports

for info in await list_serial_ports():
    print(info.device, info.vid, info.pid, info.serial_number)

ftdi = await find_serial_port(vid=0x0403, pid=0x6001)
```

### Testing without hardware

```python
from anyserial.testing import serial_port_pair

a, b = serial_port_pair()
await a.send(b"hello")
assert await b.receive(5) == b"hello"
```

`MockBackend` and `FaultPlan` (also in `anyserial.testing`) cover fault-injection scenarios.

## Documentation

Full documentation lives at <https://graysonbellamy.github.io/anyserial/>. Starting points:

- [Quickstart](docs/quickstart.md)
- [Configuration](docs/configuration.md)
- [Capabilities](docs/capabilities.md)
- [Cancellation](docs/cancellation.md)
- [Runtime reconfiguration](docs/runtime-reconfiguration.md)
- [Performance](docs/performance.md) and [Linux tuning](docs/linux-tuning.md)
- [Sync wrapper](docs/sync.md)
- [Migration from pyserial](docs/migration-from-pyserial.md)

## Contributing

Issues and PRs are welcome. To get a local checkout running:

```bash
git clone https://github.com/GraysonBellamy/anyserial
cd anyserial
uv sync --all-extras
uv run pre-commit install
```

Before opening a PR:

```bash
uv run pytest
uv run ruff check
uv run ruff format --check
uv run mypy
```

Hardware-dependent tests are opt-in via `pytest -m hardware` with `ANYSERIAL_TEST_PORT` set; see [docs/hardware-testing.md](docs/hardware-testing.md).

## License

MIT. See [LICENSE](LICENSE).
