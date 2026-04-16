# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0]

Initial release.

### Core

- Async-native serial transport built on AnyIO (>= 4.13).
- Immutable `SerialConfig` with `with_changes`, `FlowControl`,
  `RS485Config`, and `StrEnum` types (`ByteSize`, `Parity`, `StopBits`).
- Multi-inherited exception hierarchy compatible with stdlib and AnyIO
  bases.
- Tri-state `Capability` model and `SerialCapabilities` snapshot per
  backend.
- `Backend` Protocol split into `SyncSerialBackend` (POSIX) and
  `AsyncSerialBackend` (Windows).
- `SerialPort`, `SerialConnectable`, and `open_serial_port` with full
  AnyIO typed-attribute support.
- Runtime reconfiguration via `port.configure(new_config)` with
  serialized concurrent calls; failed applies leave `port.config`
  unchanged.
- Raw-bytes API: `receive`, `receive_available`, `receive_into`,
  `send`, `drain`, `drain_exact`, `input_waiting`, `output_waiting`.
- `MockBackend` and `FaultPlan` under `anyserial.testing` for
  hardware-free unit testing, plus `serial_port_pair` helper.
- Blocking `anyserial.sync.SerialPort` wrapper backed by a
  process-wide `BlockingPortalProvider`; per-call `timeout=` keyword;
  `configure_portal(backend=..., backend_options=...)` for AnyIO
  backend selection.

### Linux backend

- `LinuxBackend` with nonblocking fd I/O via `anyio.wait_readable` /
  `anyio.wait_writable`, raw-mode termios, modem-line ioctls, BREAK,
  exclusive access via `flock`, queue-depth, and buffer flush.
- Standard and extended baud (`TCSETS2` / `BOTHER`).
- `ASYNC_LOW_LATENCY` low-latency mode with restore-on-close.
- Kernel RS-485 (`TIOCSRS485`) with read-modify-write that preserves
  driver-reserved bits and restores pre-touch state on close or
  `configure(rs485=None)`.
- Native sysfs-based port discovery with USB-ancestor resolution;
  populates VID / PID / serial / manufacturer / product / location /
  interface and emits a pyserial-compatible `hwid` string.

### macOS (Darwin) backend

- `DarwinBackend` with custom baud via `IOSSIOSPEED`, BREAK via the
  shared `<sys/ttycom.h>` numeric fallback, and `UnsupportedPolicy`-
  routed rejection of `low_latency` and `rs485`.
- Native IOKit discovery walks `IOSerialBSDClient`, prefers
  `/dev/cu.*` callout paths, and climbs the IORegistry parent chain
  for USB metadata. The ctypes facade over IOKit + CoreFoundation
  loads lazily so the module imports cleanly on Linux CI.

### BSD backend

- One `BsdBackend` for FreeBSD / NetBSD / OpenBSD / DragonFly.
  Custom baud via integer `c_ispeed` / `c_ospeed` passthrough;
  `low_latency` / `rs485` rejected via `UnsupportedPolicy`.
- `/dev`-scan discovery with per-variant glob sets. USB metadata is
  intentionally not populated — use `list_serial_ports(backend="pyserial")`
  when VID / PID is needed.

### Windows backend

- `WindowsBackend` implements `AsyncSerialBackend` and dispatches
  hot-path `receive` / `send` through each runtime's native IOCP
  machinery:
  - **Trio** → `trio.lowlevel.register_with_iocp` +
    `readinto_overlapped` / `write_overlapped`.
  - **asyncio on `ProactorEventLoop`** → `loop._proactor._register` +
    `_overlapped.Overlapped.ReadFileInto` / `WriteFile` (zero-copy via
    CPython 3.12+ `ReadFileInto`).
- No worker-thread fallback. `SelectorEventLoop` raises
  `UnsupportedPlatformError` at open time pointing at
  `WindowsProactorEventLoopPolicy`.
- SetupAPI port discovery via `GUID_DEVINTERFACE_COMPORT` populates
  VID / PID / serial / manufacturer / product / location on
  USB-attached adapters; `hwid` is pyserial-compatible. Falls back to
  `HKLM\HARDWARE\DEVICEMAP\SERIALCOMM` via `winreg` when SetupAPI
  enumeration fails.
- `WaitCommEvent` modem-line change notification (`EV_CTS | EV_DSR |
  EV_RING | EV_RLSD | EV_ERR | EV_BREAK`).
- DCB round-trip (`GetCommState` → overlay → `SetCommState`) preserves
  vendor state stored in reserved DCB fields by FTDI / Prolific /
  CH340 drivers.
- "Wait-for-any" `COMMTIMEOUTS` policy (`MAXDWORD / MAXDWORD / 1 ms`)
  with internal retry loop on zero-byte completions, so idle
  `receive()` doesn't surface spurious EOF.
- Win32 error translation (`ERROR_FILE_NOT_FOUND` → `PortNotFoundError`,
  `ERROR_ACCESS_DENIED` / `ERROR_SHARING_VIOLATION` → `PortBusyError`,
  `ERROR_INVALID_HANDLE` / `ERROR_OPERATION_ABORTED` →
  `SerialClosedError`, `ERROR_INVALID_PARAMETER` on config →
  `UnsupportedConfigurationError`, `ERROR_DEVICE_REMOVED` /
  `ERROR_NOT_READY` / `ERROR_GEN_FAILURE` → `SerialDisconnectedError`).
  Exceptions carry a `.winerror` attribute.
- Capability snapshot reports `SUPPORTED` for every feature except
  `low_latency` (no Windows equivalent of `ASYNC_LOW_LATENCY`) and
  `rs485` (FTDI VCP RS-485 is driver config, not a runtime API).

### Discovery

- `list_serial_ports()`, `find_serial_port(...)`, and the `PortInfo`
  data model — async, always-live, no caching.
- Optional `pyudev` (Linux) and `pyserial` (cross-platform) backends
  selectable via the `backend=` keyword. Each raises `ImportError`
  with the exact install command when the extra isn't installed.
- `port.port_info` typed attribute on `SerialPort`: `open_serial_port`
  resolves the device path through native discovery and exposes the
  result on `port.port_info` and via the typed-attribute interface.

### Tooling and CI

- `pyproject.toml` with `hatchling` + `hatch-vcs` build, AnyIO >= 4.13
  runtime dep, Python 3.13 / 3.14 support.
- uv-managed dependency groups (lint, type, test, docs, bench).
- Ruff, mypy, pyright, pytest, coverage, pre-commit configuration.
- GitHub Actions CI for lint, typecheck, and tests on Linux, macOS,
  and Windows across Python 3.13 / 3.14 × asyncio / asyncio + uvloop /
  trio (Windows: asyncio Proactor / trio). FreeBSD smoke job via
  `cross-platform-actions`.
- Hardware test marker default-deselected (`pytest -m "not hardware"`);
  opt-in via `pytest -m hardware` with `ANYSERIAL_TEST_PORT`.
- Benchmark suite under `benchmarks/`: receive / send latency,
  throughput, many-port fan-out, allocation profile, sync-vs-async,
  Windows IOCP scenarios over com0com, and a `pyserial-asyncio`
  head-to-head. Nightly bench workflow records baselines per backend.

### Documentation

- Full documentation site covering quickstart, configuration,
  capabilities, discovery, runtime reconfiguration, RS-485,
  AnyIO backend selection, uvloop, cancellation, performance, sync
  wrapper, hardware testing, troubleshooting, migration from pySerial,
  and per-platform pages (Linux tuning, macOS, BSD, Windows).
- MIT license.

[0.1.0]: https://github.com/GraysonBellamy/anyserial/releases/tag/v0.1.0
