# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0]

### Added

- `canonical_port_name(path, *, platform=None)` returns one name for
  every spelling of a port, for use as a "same port?" key. On Windows it
  strips the `\\.\` / `\\?\` prefix and upper-cases (`com8`, `\\.\COM8`
  and `\\?\COM8` all give `COM8`, the `PortInfo.device` discovery
  reports); on POSIX it resolves symlinks when the path exists, so a
  `/dev/serial/by-id/...` link and its `/dev/ttyUSB0` target agree.
  Re-exported from `anyserial`.
- `anyserial.testing.faults_of(port)` returns the live `FaultPlan` of a
  port backed by `MockBackend`, such as either end of
  `serial_port_pair()`, so tests can inject faults without reaching into
  the private `port._backend`. Raises `TypeError` for other ports.
- Windows: `SerialPort.port_info` is populated. `open_serial_port`
  resolves the port through the SetupAPI discovery walk, in a worker
  thread before opening the handle; it was always `None` on Windows.

### Changed

- `find_serial_port(device=...)` matches any name of the port by
  comparing `canonical_port_name` results, instead of comparing strings
  verbatim: `device="com8"` finds `COM8` on Windows and a by-id symlink
  finds its target on POSIX. Every verbatim match still matches.
- `SerialPort.path` is documented as the path exactly as passed to
  `open_serial_port`; it is not normalised.

### Fixed

- Windows, Trio: `receive()` and `receive_into()` on an idle port no
  longer fail with `SerialError` ("[WinError 1460] This operation
  returned because the timeout period expired") about 1 ms after the
  call. Trio raises the `STATUS_TIMEOUT` that ends an empty read under
  the wait-for-any `COMMTIMEOUTS` policy as an error; the Trio read path
  now treats it as the empty completion asyncio reports, and the read is
  reissued.
- Windows, Trio: `WindowsBackend.wait_modem_event()` passes the
  `OVERLAPPED` to Trio by address. It previously passed the ctypes
  structure itself, which Trio cannot use as a dict key, so every pending
  wait failed with `TypeError` and left `WaitCommEvent` pending on
  buffers that were then released. A `WaitCommEvent` that completes
  synchronously now also waits for its completion packet.
- Windows, asyncio: cancelling `WindowsBackend.wait_modem_event()` no
  longer corrupts memory. The pending `WaitCommEvent` was left running
  while its buffers and event handle were released, so the kernel wrote
  into freed memory when the event later fired or `aclose()` woke it,
  and the process crashed with an access violation. The wait is now
  cancelled with `CancelIoEx` and its completion awaited before the
  buffers are released. Its `OVERLAPPED` also no longer queues a
  completion packet to the proactor's port, which has no entry for it.
- Windows: `open_serial_port` leaves a path that already starts with
  `\\?\` unchanged, as it does for `\\.\`. It previously prepended
  `\\.\`, so `\\?\COM8`, and the `\\?\…` device-interface paths
  discovery reports for ports without a `COMn` name, could not be opened.
- Windows discovery reports `vid`, `pid` and `serial_number` for FTDI
  adapters on FTDI's VCP driver, and `serial_number` for USB devices
  that have one. They are read from the device instance ID
  (`FTDIBUS\VID_0403+PID_6001+<serial>A\0000`,
  `USB\VID_xxxx&PID_xxxx\<serial>`); the hardware ID used before names
  no serial number and, for FTDI ports, is not in the `USB\VID_…` form
  that was parsed. FTDI's appended port letter is dropped so the serial
  number matches Linux and macOS. The IDs Windows generates for devices
  without a serial number are not reported as serial numbers.

### Documentation

- Windows: corrected the device-path rules. `open_serial_port` adds the
  `\\.\` prefix itself, so `"COM10"` opens the port; the docs said it
  opened a file in the current directory.

## [0.1.2]

### Fixed

- Windows: `HandleWrapper` now declares `__weakref__` in `__slots__` so it
  can be registered with the asyncio `IocpProactor._registered` `WeakSet`
  on CPython >= 3.12. Without this slot every `open_serial_port` call on
  the asyncio runtime path raised `TypeError: cannot create weak
  reference to 'HandleWrapper' object` at registration time, blocking
  every consumer that goes through `WindowsBackend.open` on the asyncio
  loop.

## [0.1.1]

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
  Windows IOCP scenarios over an opt-in configured serial pair, and a
  `pyserial-asyncio` head-to-head. Nightly bench workflow records
  baselines per backend.

### Documentation

- Full documentation site covering quickstart, configuration,
  capabilities, discovery, runtime reconfiguration, RS-485,
  AnyIO backend selection, uvloop, cancellation, performance, sync
  wrapper, hardware testing, troubleshooting, migration from pySerial,
  and per-platform pages (Linux tuning, macOS, BSD, Windows).
- MIT license.

[0.2.0]: https://github.com/GraysonBellamy/anyserial/releases/tag/v0.2.0
[0.1.2]: https://github.com/GraysonBellamy/anyserial/releases/tag/v0.1.2
[0.1.1]: https://github.com/GraysonBellamy/anyserial/releases/tag/v0.1.1
