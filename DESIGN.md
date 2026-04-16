# anyserial — Design Plan

**Status:** Implemented
**Target Python:** 3.13+
**Target AnyIO:** >= 4.13 (latest stable as of 2026-04-15)
**License:** MIT
**Author:** Grayson Bellamy
**Last updated:** 2026-04-15

---

## 1. Purpose

`anyserial` is a ground-up design for a high-performance, robust, maintainable Python package that provides async-native serial-port I/O, built around AnyIO.

Linux is the first-class platform. macOS and BSD are fully supported POSIX targets. Windows is anticipated through backend boundaries but deferred to a later release. Low latency is the highest-priority performance characteristic. Correctness, predictable cancellation, and honest handling of hardware variability are prioritized over cleverness.

---

## 2. Design Principles

- AnyIO is the public concurrency abstraction.
- Linux is the first-class platform; other POSIX targets are fully supported second-class.
- POSIX support is factored cleanly via composition, not bolted onto Linux behavior.
- Windows is anticipated through backend boundaries but not allowed to distort the POSIX design.
- Composition over inheritance: platform specifics are a Backend strategy, not a parent class.
- Immutable, validated configuration with explicit runtime reconfiguration.
- Unsupported features fail explicitly when requested; silent fallback is opt-in.
- Hardware and driver variability is treated as normal, not exceptional — surfaced via an explicit capability model.
- Benchmarks are measurable, reproducible, and run against real hardware and pseudoterminals.
- The public API is small, typed, documented, and stable.
- Core is raw-bytes only. Framing and protocols belong in user code or downstream packages.

### 2.1 Python 3.13+ feature policy

Requiring 3.13 lets the codebase use modern features directly — no compatibility shims for older interpreters.

**Used aggressively:**
- `typing.Self` for fluent-return typing
- `typing.override` on every method that overrides a base / Protocol
- `typing.Protocol` for all backend interfaces (runtime-checkable only when needed)
- PEP 695 `type` aliases (e.g., `type BytesLike = collections.abc.Buffer`)
- `collections.abc.Buffer` (PEP 688) for bytes-like accepting parameters on performance paths
- `enum.StrEnum` for user-facing enums — stable string serialization for logs, JSON configs, CLI args
- `dataclasses.dataclass(frozen=True, slots=True, kw_only=True)` everywhere
- `warnings.deprecated` decorator (PEP 702) for typed deprecation warnings if the API evolves
- `contextlib.aclosing`, `contextlib.AsyncExitStack` for clean async resource composition
- Modern union syntax `A | B` (no `Optional[T]`, no `Union[A, B]`)
- `except*` / `ExceptionGroup` where it genuinely helps (tests across multiple ports, multi-error cleanup)
- `memoryview` used precisely in every hot-path byte accept signature

### 2.2 Python 3.14+ awareness

3.14 is the latest stable release line as of this design's date. Library code targets 3.13 as the floor but must be forward-compatible:

- CI tests 3.13 and 3.14; 3.15-dev runs as allowed-failure when available.
- Avoid fragile runtime annotation introspection — 3.14 changes annotation evaluation (PEP 649). If introspection is needed, use `typing.get_type_hints()` or version-gated `annotationlib`, only in tooling (never in the hot path).
- Keep the library pure-Python and low on global mutable state so free-threaded (PEP 703) CPython builds work without surprises.
- Benchmark on regular CPython and free-threaded builds where CI images are available. Do not make performance claims that depend on the experimental JIT.

### 2.3 Free-threaded Python

`anyserial` is I/O-bound, so free-threaded Python is not the primary performance lever. The design still avoids patterns that break in free-threaded builds:

- No mutable module-global backend state.
- No unsynchronized shared registries.
- No reliance on the GIL for internal invariants.
- No C extension in the first release.
- Clear event-loop and thread ownership rules (§15, §7.3).

---

## 3. Goals

1. Provide an AnyIO-native bidirectional serial byte stream.
2. Implement serial I/O directly on nonblocking file descriptors on POSIX.
3. Expose common serial configuration options:
   - baud rate (standard, platform-extended, and custom)
   - byte size
   - parity
   - stop bits
   - software flow control (XON/XOFF)
   - RTS/CTS flow control
   - DTR/DSR flow control where supported
   - RTS/DTR control-line writes
   - CTS/DSR/RI/CD modem-line reads
   - break signaling
   - hangup-on-close
   - exclusive access
   - low-latency mode where supported
   - RS-485 where supported
4. Support port discovery.
5. Support runtime reconfiguration where feasible.
6. Provide clear capability reporting by platform and device.
7. Provide a strong test suite and benchmark suite from day one.
8. Avoid hidden threads in the primary POSIX I/O path.
9. Avoid pySerial as the runtime engine for the low-latency POSIX path.
10. Use pySerial as an API/behavior reference and as an optional discovery fallback.
11. Offer a thin synchronous wrapper for scripts and test benches.

## 4. Non-Goals

- No hard real-time guarantees.
- No forced event loop or AnyIO backend on applications.
- `uvloop` is not a required dependency.
- No silent emulation of unsupported hardware features.
- No protocol parsers in the core serial transport (no framing, line delimiting, Modbus, NMEA, SLIP).
- No full Windows support in the initial release.
- No exposure of every low-level ioctl as public API.
- No `io_uring`, kernel-bypass, or C extension in the hot path (see §26).
- No Python < 3.13.

---

## 5. Target Audience

- Embedded systems engineers writing host-side control and monitoring code.
- Hardware test benches and production test fixtures.
- Scientific instrument integrations (lab equipment, sensors).
- Industrial applications requiring RS-485.
- Library authors building higher-level protocol stacks on top of `anyserial`.

---

## 6. Architecture

### 6.1 Layered model

```
+-----------------------------------------------------------+
|  User code                                                |
+-----------------------------------------------------------+
|  Public API:                                              |
|   open_serial_port()    SerialPort     SerialConfig       |
|   list_serial_ports()   SerialCapabilities                |
|   PortInfo  ModemLines  SerialConnectable  exceptions     |
|   sync.open_serial_port() / sync.SerialPort (sync wrapper)|
+-----------------------------------------------------------+
|  SerialPort (async orchestration) — owns:                 |
|   * readiness loop  (anyio.wait_readable / wait_writable) |
|   * cancellation + partial-write handling                 |
|   * ResourceGuards, close lock, configure lock            |
|   * aclose() lifecycle (anyio.notify_closing + close)     |
|   * errno -> exception mapping                            |
|   * capability resolution + typed attributes              |
+-----------------------------------------------------------+
|  Backend Protocols (platform boundary):                   |
|   SyncSerialBackend  — OS primitives; zero AnyIO coupling |
|   AsyncSerialBackend — for platforms without fd readiness |
+-----------------------------------------------------------+
|  Platform backends (implementations):                     |
|   LinuxBackend  DarwinBackend  BsdBackend   } SyncSerialBackend
|   PosixBackend  MockBackend (tests)         }             |
|   WindowsBackend (future)                   } AsyncSerialBackend
+-----------------------------------------------------------+
|  OS:  termios | ioctl | fcntl | overlapped I/O (Win)      |
+-----------------------------------------------------------+
```

The backend layer is pure OS mechanics with **zero AnyIO coupling** on POSIX. All async logic — readiness waiting, cancellation, resource guards, close sequencing — lives in one place: `SerialPort`. This matches the canonical Python async-I/O pattern used by Trio's own `SocketStream` and AnyIO's internal backend implementations. See §25 for the Protocol split; Appendix G and Appendix C carry the deeper rationale.

### 6.2 Package layout

```
src/anyserial/
  __init__.py               # Public re-exports, __version__
  __about__.py              # Version string (hatch-vcs)
  py.typed                  # PEP 561 marker
  _types.py                 # Enums, ModemLines
  config.py                 # SerialConfig, FlowControl, RS485Config
  capabilities.py           # SerialCapabilities, UnsupportedPolicy
  exceptions.py             # Exception hierarchy + errno mapping
  discovery.py              # list_serial_ports, find_serial_port, PortInfo
  stream.py                 # SerialPort (async, primary API)
  sync.py                   # sync.SerialPort (sync wrapper)
  testing.py                # Public MockBackend helpers
  _backend/
    __init__.py
    protocol.py             # SyncSerialBackend, AsyncSerialBackend Protocols
    selector.py             # Platform dispatch
  _posix/
    __init__.py
    termios_apply.py        # Pure termios attr builders
    ioctl.py                # Shared ioctl helpers
    discovery.py            # Generic POSIX discovery
    backend.py              # PosixBackend (sync)
  _linux/
    __init__.py
    baudrate.py             # TCGETS2/TCSETS2, BOTHER
    capabilities.py
    low_latency.py          # ASYNC_LOW_LATENCY, FTDI latency timer
    rs485.py                # TIOCSRS485
    discovery.py            # /sys/class/tty, pyudev optional
    backend.py              # LinuxBackend (sync)
  _darwin/
    __init__.py
    baudrate.py             # IOSSIOSPEED
    capabilities.py
    discovery.py            # IOKit via ctypes
    backend.py              # DarwinBackend (sync)
  _bsd/
    __init__.py
    baudrate.py
    capabilities.py
    backend.py              # BsdBackend (sync)
  _windows/                 # Placeholder; stubs + design notes only
    __init__.py
    backend.py              # WindowsBackend (async, future — AsyncSerialBackend)
    notes.md
  _mock/
    __init__.py
    backend.py              # MockBackend (sync; loopback pair, fault injection)

tests/
  conftest.py
  unit/                     # MockBackend-driven; no hardware
  integration/              # socat pty pairs on Linux
  hardware/                 # Opt-in, real device required
  property/                 # Hypothesis-based invariants
  typing/                   # reveal_type assertions

benchmarks/
  latency_roundtrip.py
  throughput.py
  many_ports.py
  allocation_profile.py
  compare_pyserial.py
  compare_trio_pyserial.py

docs/
  index.md
  quickstart.md
  api.md
  configuration.md
  capabilities.md
  hardware-tuning.md
  low-latency.md
  discovery.md
  performance.md
  benchmarks.md
  troubleshooting.md
  migration-from-pyserial.md
  changelog.md
```

`_windows/` is a placeholder from day one: no implementation, but its presence forces the backend Protocol to stay neutral of POSIX-specific types (e.g., raw int fds). Composition-over-inheritance rationale lives in Appendix C.

---

## 7. Public API

### 7.1 Primary entry point

```python
import anyio
from anyserial import SerialConfig, open_serial_port


async def main() -> None:
    config = SerialConfig(baudrate=115200, low_latency=True)
    async with await open_serial_port("/dev/ttyUSB0", config) as port:
        await port.send(b"ping\n")
        with anyio.fail_after(1.0):
            reply = await port.receive(1024)
        print(reply)


anyio.run(main)
```

Opening is explicit and async. `__init__` never touches the OS — this is testable, aligned with AnyIO, and avoids surprises at construction time.

### 7.2 `SerialPort` class

```python
from collections.abc import Buffer
from typing import override

type BytesLike = Buffer     # PEP 688 — bytes, bytearray, memoryview, array.array, numpy, ...

class SerialPort(anyio.abc.ByteStream):
    # ByteStream introspection is exposed via AnyIO typed attributes (see §7.4),
    # not ad-hoc properties. These convenience properties exist for ergonomic
    # user code but are duplicates of what extra_attributes offers generically.
    @property
    def path(self) -> str: ...
    @property
    def is_open(self) -> bool: ...

    # --- anyio.abc.ByteStream ------------------------------------------------
    @override
    async def receive(self, max_bytes: int = 65536) -> bytes: ...
    @override
    async def send(self, item: bytes) -> None: ...          # exact ByteSendStream signature
    @override
    async def send_eof(self) -> None: ...                   # drains; see §14.2
    @override
    async def aclose(self) -> None: ...

    async def __aenter__(self) -> Self: ...
    async def __aexit__(self, *exc_info: object) -> None: ...

    # --- Serial-specific I/O extensions -------------------------------------
    async def receive_into(self, buffer: bytearray | memoryview) -> int: ...
    async def receive_available(self, *, limit: int | None = None) -> bytes: ...
    async def send_buffer(self, data: BytesLike) -> None: ...   # zero-copy bytes-like

    # --- Runtime reconfiguration (§10) --------------------------------------
    async def configure(self, config: SerialConfig) -> None: ...

    # --- Buffer and line control -------------------------------------------
    async def reset_input_buffer(self) -> None: ...
    async def reset_output_buffer(self) -> None: ...
    async def drain(self) -> None: ...                    # async TIOCOUTQ poll; fast path
    async def drain_exact(self) -> None: ...              # tcdrain via worker thread; FIFO-exact
    async def send_break(self, duration: float = 0.25) -> None: ...

    # --- Modem / control lines ---------------------------------------------
    async def modem_lines(self) -> ModemLines: ...
    async def set_control_lines(
        self, *, rts: bool | None = None, dtr: bool | None = None,
    ) -> None: ...

    # --- Snapshots (non-awaiting) ------------------------------------------
    def input_waiting(self) -> int: ...
    def output_waiting(self) -> int: ...

    # --- AnyIO typed attributes (§7.4) -------------------------------------
    @property
    def extra_attributes(self) -> Mapping[Any, Callable[[], Any]]: ...
```

**`send(bytes)` vs `send_buffer(BytesLike)`.** `send` matches `anyio.abc.ByteSendStream` exactly — `bytes` in, full-write semantics, LSP-clean for generic code that accepts any `ByteStream`. `send_buffer` is the serial-specific zero-copy convenience that accepts any `collections.abc.Buffer` (PEP 688): `bytes`, `bytearray`, `memoryview`, `array.array`, numpy arrays exposing the buffer protocol, etc. Internally both share one `memoryview`-based write loop; `send` wraps `bytes` into a `memoryview` and delegates.

No `receive_exactly` / `receive_until` / `send_some` in the core surface. Users get these by wrapping the port in `anyio.streams.buffered.BufferedByteStream` (§13.2). This keeps the public API compact and the one-way-to-do-each-thing rule intact. `send_some` may be added later if benchmarks or real users need partial-write control.

**Convenience alternate constructor** (thin wrapper over `open_serial_port`):

```python
async with await SerialPort.open("/dev/ttyUSB0", baudrate=115200) as port:
    ...
```

The `SerialPort` object implements `anyio.abc.ByteStream`, so it composes with every AnyIO stream helper (`BufferedByteStream`, `stapled_memory_object`, `TLSStream`, etc.).

### 7.3 `SerialConnectable` — deferred connection

For users and frameworks that want a "recipe" object they can open later, `SerialConnectable` implements `anyio.abc.ByteStreamConnectable`:

```python
@dataclass(frozen=True, slots=True, kw_only=True)
class SerialConnectable(anyio.abc.ByteStreamConnectable):
    path: str
    config: SerialConfig

    async def connect(self) -> SerialPort: ...
```

This mirrors AnyIO's connectable protocol (used by `TCPConnectable`, `UNIXSocketConnectable`, etc.) without making construction perform I/O. Composable with any AnyIO code that accepts a `ByteStreamConnectable`:

```python
connectable = SerialConnectable(path="/dev/ttyUSB0", config=config)
async with await connectable.connect() as port:
    ...
```

### 7.4 AnyIO typed attributes

AnyIO streams expose implementation details through typed attributes rather than forcing backend-specific public properties. `SerialPort` implements `extra_attributes` and supports:

| Attribute | Source | Available when |
|---|---|---|
| `anyio.streams.file.FileStreamAttribute.fileno` | stdlib-adjacent | POSIX backends |
| `anyio.streams.file.FileStreamAttribute.path` | stdlib-adjacent | All backends |
| `SerialStreamAttribute.capabilities` | `anyserial` | All backends |
| `SerialStreamAttribute.config` | `anyserial` | All backends |
| `SerialStreamAttribute.port_info` | `anyserial` | When discovery metadata is available |

Usage:

```python
from anyio.streams.file import FileStreamAttribute
from anyserial import SerialStreamAttribute

fd           = port.extra(FileStreamAttribute.fileno)          # POSIX only
capabilities = port.extra(SerialStreamAttribute.capabilities)
config       = port.extra(SerialStreamAttribute.config)
```

This is the canonical AnyIO pattern for exposing backend details. It keeps generic stream composition working (code that knows nothing about `SerialPort` can still ask for typed attributes it understands), and it lets the Windows backend cleanly omit the POSIX-only `fileno` attribute without a type-level compromise.

### 7.5 Sync wrapper (deferred)

Async is the primary promise; the sync wrapper is deferred to M7 (§34), after the async core is stable. For scripts and test benches that don't want an event loop:

```python
from anyserial.sync import SerialPort as SyncSerialPort
# or: from anyserial.sync import open_serial_port

with SyncSerialPort.open("/dev/ttyUSB0", baudrate=115200) as port:
    port.send(b"ping\n")
    reply = port.receive(1024, timeout=1.0)
```

**Implementation.** Built on `anyio.from_thread.BlockingPortalProvider` (AnyIO 4.4+) — a refcounted singleton portal that runs one AnyIO event-loop thread shared by every sync `SerialPort` instance in the process. Each sync call dispatches to the portal via `portal.call(coro, *args)`; lifecycle methods use `portal.wrap_async_context_manager(open_serial_port(...))`. The portal is lazily spawned on first use and torn down when the last reference is released.

```python
_PROVIDER = anyio.from_thread.BlockingPortalProvider(
    backend="asyncio",
    backend_options={"use_uvloop": True},  # configurable
)

class SerialPort:  # sync
    def __init__(self, config: SerialConfig) -> None: ...
    def open(self) -> None:
        self._portal_cm = _PROVIDER  # refcount
        portal = self._portal_cm.__enter__()
        self._async_cm = portal.wrap_async_context_manager(
            open_serial_port(self._path, self._config)
        )
        self._async_port = self._async_cm.__enter__()
    def receive(self, max_bytes=65536, timeout: float | None = None) -> bytes:
        return self._portal.call(self._do_receive, max_bytes, timeout)
    # ...
```

API parity with async `SerialPort`, minus the `async`/`await`, plus optional per-call `timeout` arguments (implemented internally with `anyio.fail_after`). Zero duplicated I/O code — the sync wrapper is a pure delegation layer.

**Rationale — `BlockingPortalProvider`.** Multiple sync ports share one event-loop thread (one OS thread total for the whole process, not one per port). The portal selects the async backend and `use_uvloop` option at construction, giving users one configuration surface for both sync and async code. This replaces the hand-rolled `run_coroutine_threadsafe` pattern common in pre-AnyIO-4.4 designs.

### 7.6 Public re-exports

`anyserial/__init__.py` exposes:

- `SerialPort`, `SerialConnectable`, `open_serial_port`
- `SerialConfig`, `FlowControl`, `RS485Config`
- `Parity`, `StopBits`, `ByteSize`, `UnsupportedPolicy`, `Capability`
- `ModemLines`, `ControlLines`, `SerialCapabilities`, `SerialStreamAttribute`
- `PortInfo`, `list_serial_ports`, `find_serial_port`
- `BytesLike` type alias
- All exception classes
- `__version__`

`anyserial.sync` (deferred) mirrors the port-related exports as their sync counterparts.

---

## 8. Configuration Model

### 8.1 Enums

All user-facing enums use `StrEnum` (PEP 663 / 3.11+) for stable string serialization in logs, JSON configs, and CLI args. Internally we convert to numeric termios flags via a lookup table in `_posix/termios_apply.py`.

```python
from enum import StrEnum

class ByteSize(StrEnum):
    FIVE  = "5"
    SIX   = "6"
    SEVEN = "7"
    EIGHT = "8"

class Parity(StrEnum):
    NONE  = "none"
    ODD   = "odd"
    EVEN  = "even"
    MARK  = "mark"
    SPACE = "space"

class StopBits(StrEnum):
    ONE            = "1"
    ONE_POINT_FIVE = "1.5"
    TWO            = "2"
```

A `StrEnum` instance is also a `str`, so logs show `"Parity.NONE"` repr and `"none"` when formatted, and `json.dumps({"parity": Parity.NONE})` works out of the box.

### 8.2 Flow control

Flow control is a dataclass, not an enum, because the modes are independent booleans and the platform may reject specific combinations:

```python
@dataclass(frozen=True, slots=True)
class FlowControl:
    xon_xoff: bool = False
    rts_cts: bool = False
    dtr_dsr: bool = False

    @classmethod
    def none(cls) -> "FlowControl":
        return cls()
```

### 8.3 RS-485

```python
@dataclass(frozen=True, slots=True)
class RS485Config:
    enabled: bool = True
    rts_on_send: bool = True
    rts_after_send: bool = False
    delay_before_send: float = 0.0
    delay_after_send: float = 0.0
    rx_during_tx: bool = False
```

### 8.4 SerialConfig

```python
@dataclass(frozen=True, slots=True)
class SerialConfig:
    baudrate: int = 115200
    byte_size: ByteSize = ByteSize.EIGHT
    parity: Parity = Parity.NONE
    stop_bits: StopBits = StopBits.ONE
    flow_control: FlowControl = field(default_factory=FlowControl)
    exclusive: bool = False
    hangup_on_close: bool = True
    low_latency: bool = False
    read_chunk_size: int = 65536
    rs485: RS485Config | None = None
    unsupported_policy: UnsupportedPolicy = UnsupportedPolicy.RAISE

    def with_changes(self, **changes) -> "SerialConfig":
        return dataclasses.replace(self, **changes)
```

Validation runs in `__post_init__`: baudrate > 0, `read_chunk_size >= 64`, no mutually exclusive flow-control combos, etc. All violations raise `UnsupportedConfigurationError`.

### 8.5 Runtime reconfiguration

```python
await port.configure(port.config.with_changes(baudrate=1_000_000))
```

- Serialized by an internal `_configure_lock`.
- Does not block already-in-flight reads/writes longer than necessary.
- Termios changes apply atomically from the package's perspective (via `tcsetattr` in one call).
- If the new config requests a feature the device doesn't support, the behavior follows `unsupported_policy`.

---

## 9. Capability Model

Serial hardware is inconsistent. Rather than forcing users to infer feature support from exception catches, the package exposes capability metadata:

```python
@dataclass(frozen=True, slots=True)
class SerialCapabilities:
    platform: str                     # "linux", "darwin", "freebsd", ...
    backend: str                      # "linux", "darwin", "bsd", "posix", "mock"
    custom_baudrate:           Capability
    mark_space_parity:         Capability
    one_point_five_stop_bits:  Capability
    xon_xoff:                  Capability
    rts_cts:                   Capability
    dtr_dsr:                   Capability
    modem_lines:               Capability
    break_signal:              Capability
    exclusive_access:          Capability
    low_latency:               Capability
    rs485:                     Capability
    input_waiting:             Capability
    output_waiting:            Capability
    port_discovery:            Capability
```

Each feature is modeled as a tri-state, not a bool:

```python
class Capability(StrEnum):
    SUPPORTED   = "supported"
    UNSUPPORTED = "unsupported"
    UNKNOWN     = "unknown"     # platform advertises; actual driver/device will say yes or no
```

**Why tri-state.** Serial feature support is inherently multi-level:

1. **Platform level** — known at import time (e.g., Linux has `BOTHER`).
2. **Driver level** — known after device open (e.g., FTDI kernel driver supports `TIOCSRS485`; CP210x may not).
3. **Device level** — a specific adapter may reject a specific baud rate or handshake.
4. **Operation level** — an ioctl fails at runtime.

A boolean collapses levels 1–3 into one bit and lies to the user. The `Capability` tri-state says: `SUPPORTED` means the stack definitely supports it, `UNSUPPORTED` means it definitely does not, `UNKNOWN` means the platform can advertise it but the answer depends on driver or device. Example: Linux may report `custom_baudrate = SUPPORTED` while a specific USB adapter rejects a specific rate at runtime with `UnsupportedConfigurationError`.

### 9.1 Unsupported-feature policy

```python
class UnsupportedPolicy(StrEnum):
    RAISE  = "raise"     # default; explicit feature requests raise
    WARN   = "warn"      # best-effort with warnings.warn(...)
    IGNORE = "ignore"    # silent best-effort
```

Default is `RAISE`. Users who want best-effort behavior (e.g., `low_latency=True` on a kernel that lacks the ioctl) opt in explicitly via `SerialConfig(..., unsupported_policy=UnsupportedPolicy.WARN)`. Core configuration errors (invalid baud, impossible flow-control combo) always raise — policy applies only to *optional* features.

---

## 10. Exception Hierarchy

```python
class SerialError(OSError):
    """Base class for serial-port failures."""

class ConfigurationError(SerialError, ValueError):
    """The supplied SerialConfig is internally invalid (bad baud, impossible flow-control combo)."""

class PortNotFoundError(SerialError, FileNotFoundError):
    """The requested port does not exist."""

class PortBusyError(SerialError):
    """The port is already in use or locked exclusively."""

class UnsupportedFeatureError(SerialError, NotImplementedError):
    """A requested feature is unsupported by the backend, driver, or device."""

class UnsupportedConfigurationError(SerialError, ValueError):
    """A requested configuration is unsupported at runtime (driver/device rejects it)."""

class SerialClosedError(SerialError, anyio.ClosedResourceError):
    """Operation attempted on a closed port."""

class SerialDisconnectedError(SerialError, anyio.BrokenResourceError):
    """Device was removed or became unusable during I/O."""

class UnsupportedAsyncBackendError(SerialError, RuntimeError):
    """The active async backend is unsupported."""
```

Multi-inheritance is deliberate: each exception inherits from the standard-library class that most naturally describes the failure. Callers that already catch `OSError`, `ValueError`, `FileNotFoundError`, `NotImplementedError`, `anyio.ClosedResourceError`, or `anyio.BrokenResourceError` automatically handle our exceptions — no new catch clauses required. Our exceptions also carry the original errno, filename, and strerror through `OSError`.

### 10.1 Errno mapping

| Errno / condition | Exception |
|---|---|
| `ENOENT`, `ENODEV`, `ENXIO` on open | `PortNotFoundError` |
| `EBUSY`, `fcntl(LOCK_EX)` failure | `PortBusyError` |
| `EINVAL`, `ENOTTY` on specific ioctl | `UnsupportedFeatureError` or `UnsupportedConfigurationError` (context-dependent) |
| `EIO`, repeated zero-length reads after readiness, USB removal | `SerialDisconnectedError` (aliased to AnyIO `BrokenResourceError`) |
| Operation after `aclose()` on this port | `anyio.ClosedResourceError` |
| Concurrent operation forbidden by `ResourceGuard` | `anyio.BusyResourceError` |
| Peer closed / unrecoverable EOF on `receive()` | `anyio.EndOfStream` |

Original `OSError.__cause__` is always preserved.

**AnyIO-compatible exceptions.** Where AnyIO defines a canonical exception type for the condition, we raise that type directly (no wrapping, no `anyserial`-specific parallel). Specifically:

- `anyio.ClosedResourceError` — this port was closed locally (`aclose()` returned).
- `anyio.BrokenResourceError` — the stream is unusable due to external causes (device disconnect, EIO storm). `SerialDisconnectedError` is a subclass.
- `anyio.EndOfStream` — `receive()` observed a clean EOF from the peer.
- `anyio.BusyResourceError` — `ResourceGuard` violation (second reader or second writer).

Per the AnyIO `ByteStream` contract, `receive()` **never returns `b""`**. Callers do not need to special-case the empty-bytes sentinel. EOF / disconnect becomes an exception, always.

`anyserial`-specific exceptions (`SerialError`, `PortNotFoundError`, `PortBusyError`, `UnsupportedFeatureError`, `UnsupportedConfigurationError`, `SerialClosedError`, `SerialDisconnectedError`) are used for conditions AnyIO does not model. `SerialClosedError` is a subclass of `anyio.ClosedResourceError`; `SerialDisconnectedError` is a subclass of `anyio.BrokenResourceError`. This means user code catching the AnyIO base class handles our subclasses correctly.

---

## 11. AnyIO Integration

The library is AnyIO-native, backend-neutral, and uses only AnyIO's public unified API. No `asyncio`-specific calls. No `sniffio` dependency or backend branching.

### 11.1 Canonical API surface

| Operation | AnyIO call | Version added |
|---|---|---|
| Wait for fd readable | `await anyio.wait_readable(fd)` | 4.7 |
| Wait for fd writable | `await anyio.wait_writable(fd)` | 4.7 |
| Wake pending waiters before close | `anyio.notify_closing(fd)` | 4.10 |
| Mutual exclusion per direction | `anyio.ResourceGuard("reading from")` / `("writing to")` | 4.1 |
| Serialize reconfiguration | `anyio.Lock(fast_acquire=True)` | 4.x |
| Per-call timeout | `with anyio.fail_after(delay): ...` | — |
| Sync → async bridge | `anyio.from_thread.BlockingPortalProvider` | 4.4 |
| Buffered full-duplex wrapper (user-side) | `anyio.streams.buffered.BufferedByteStream` | 4.10 |
| Cancellation exception class | `anyio.get_cancelled_exc_class()` | — |

### 11.2 Serial fds use `wait_readable` / `wait_writable`, never the socket variants

```python
await anyio.wait_readable(self._fd)
data = os.read(self._fd, n)
```

The older `wait_socket_readable` / `wait_socket_writable` functions are deprecated since 4.7. Serial fds use the fd-generalized API directly — no socket-wrapping workaround.

**Windows caveat (per AnyIO docs):** on Windows, `wait_readable` / `wait_writable` accept only `SOCKET` handles — **not** arbitrary file handles or COM-port HANDLEs. The future Windows backend cannot reuse this path; it must use a thread bridge or overlapped I/O. See §24.5.

### 11.3 `notify_closing` is mandatory in `aclose()`

Any task parked in `wait_readable(fd)` or `wait_writable(fd)` must be woken **before** `os.close(fd)`. AnyIO provides `anyio.notify_closing(fd)` for exactly this:

```python
async def aclose(self) -> None:
    async with self._close_lock:
        if self._closed:
            return
        self._closed = True
        anyio.notify_closing(self._fd)   # wake pending wait_readable/writable
        os.close(self._fd)
        self._fd = -1
```

Pending tasks receive `ClosedResourceError`. Skipping `notify_closing` causes misleading `OSError`s on `ProactorEventLoop` (Windows asyncio) and can hang on Trio.

### 11.4 No `sniffio`, no backend branching

AnyIO 4.12 dropped `sniffio` as a direct dependency. `anyserial` does not import `sniffio` and does not branch on the running backend. The unified AnyIO API is sufficient. If library code ever needs the backend's cancellation exception class, it uses `anyio.get_cancelled_exc_class()`.

### 11.5 Event-loop selection is the user's choice

The library does not own the process event loop. It never calls `uvloop.install()` or mutates global loop state. Users choose:

```python
anyio.run(main)                                        # asyncio default
anyio.run(main, backend="asyncio",
          backend_options={"use_uvloop": True})        # uvloop (POSIX) / winloop (Windows, 4.12+)
anyio.run(main, backend="trio")                        # trio
```

`uvloop` (POSIX) and `winloop` (Windows, wired up by AnyIO 4.12's `use_uvloop` shorthand) are documented and benchmarked but not required dependencies.

### 11.6 Cancellation discipline

- `anyio.wait_readable(fd)` is itself a checkpoint. **Do not insert explicit `anyio.lowlevel.checkpoint()` calls** inside read/write loops that already await on every iteration.
- Explicit checkpoints are warranted only in CPU-bound inner loops that never await (we don't have any).
- Finalization in `aclose()` runs inside `with anyio.CancelScope(shield=True):` so a cancelled scope cannot prevent clean fd teardown.
- `CancelScope.cancel(reason=...)` (4.11) is used when cancelling internal scopes for clearer diagnostics.

### 11.7 TaskGroup discipline

`SerialPort` does not internally spawn an `anyio.TaskGroup`. Concurrency is the caller's concern — the port is a `ByteStream` whose `receive()` and `send()` are safe to call from two different tasks. Callers bring their own `create_task_group()`. This mirrors every built-in AnyIO stream (`SocketStream`, `UNIXSocketStream`, `BufferedByteStream`).

### 11.8 Canonical-pattern checklist

A one-page reference for reviewers, to catch AnyIO anti-patterns at PR time:

| Do | Don't |
|---|---|
| `await anyio.wait_readable(fd)` | `await anyio.wait_socket_readable(sock)` (deprecated) |
| `anyio.notify_closing(fd); os.close(fd)` in `aclose()` | `os.close(fd)` without waking waiters |
| `anyio.ResourceGuard("reading from")` per direction | Single `Lock` serializing reads and writes |
| `anyio.Lock()` for reconfiguration (add `fast_acquire=True` only if benchmarks justify) | `asyncio.Lock()` or `threading.Lock()` |
| `with anyio.fail_after(t):` for timeouts | `timeout=` parameter on every method |
| `anyio.CancelScope(shield=True)` around close critical section | Letting cancellation leak an open fd |
| `anyio.from_thread.BlockingPortalProvider` for sync wrapper | `asyncio.run_coroutine_threadsafe` ad-hoc |
| Raise `EndOfStream` on peer EOF / disconnect | Return `b""` |
| Raise `ClosedResourceError` on use-after-close | Ad-hoc `SerialClosedError` without AnyIO parent |
| Let callers bring their own `TaskGroup` | Spawn background tasks inside `SerialPort` |
| Use `anyio.get_cancelled_exc_class()` if needed | Import asyncio/trio cancellation exceptions directly |
| Depend on `anyio>=4.13` only | Depend on `sniffio` directly |
| Use `port.extra(FileStreamAttribute.fileno)` | Public `.fd` property |
| `anyio.streams.buffered.BufferedByteStream(port)` | Hide a readahead buffer inside `SerialPort` |
| Built-in `pytest.mark.anyio` plugin | Add `pytest-anyio` as a test dep |
| POSIX backends implement sync `SyncSerialBackend`, zero `import anyio` | Async methods in POSIX backend code |
| Readiness loop lives once in `SerialPort` | Readiness loop duplicated per backend |
| `anyio.to_thread.run_sync` for blocking syscalls (`tcdrain`, `tcsendbreak`) | Calling them inline from a coroutine (freezes the event loop) |
| `O_NONBLOCK` fd + sync `os.read`/`os.write` in hot path | Blocking `os.read` dispatched to a worker thread (the pyserial-asyncio anti-pattern) |
| Document `use_uvloop=True` in docs | Call `uvloop.install()` in library code |

---

## 12. POSIX I/O Design

File descriptors are opened with:

- `O_RDWR`
- `O_NOCTTY`
- `O_NONBLOCK`
- `O_CLOEXEC` where available

There is **no separate fd-transport class**. The sync backend (§25) is itself the OS-primitives layer — it owns the fd and exposes `fileno()`, `read_nonblocking(buf)`, and `write_nonblocking(data)` as sync methods. The async read/write loops live in `SerialPort` and drive the backend via `anyio.wait_readable` / `wait_writable`.

### 12.1 Read loop (in `SerialPort`)

```python
async def receive(self, max_bytes: int = 65536) -> bytes:
    with self._receive_guard:
        self._raise_if_closed()
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        buf = bytearray(min(max_bytes, self._config.default_receive_size))
        fd = self._backend.fileno()
        while True:
            await anyio.wait_readable(fd)        # checkpoint + backpressure
            self._raise_if_closed()
            try:
                count = self._backend.read_nonblocking(buf)
            except InterruptedError:
                continue
            except BlockingIOError:
                continue
            if count == 0:
                raise SerialDisconnectedError("device returned EOF after readiness")
            return bytes(buf[:count])
```

**Invariants**

1. The `wait_readable` call is the only cancellation point; no explicit `checkpoint()`.
2. EINTR / EAGAIN loop back to `wait_readable` — a spurious wakeup re-parks the task.
3. `b""` from `os.read` after a readiness wakeup means disconnect — raise, never return.
4. `_raise_if_closed()` after wakeup catches the race where `aclose()` fired during the wait.

### 12.2 Write loop (in `SerialPort`)

```python
async def send(self, item: bytes) -> None:
    with self._send_guard:
        await self._send_buffer(memoryview(item))

async def _send_buffer(self, view: memoryview) -> None:
    self._raise_if_closed()
    fd = self._backend.fileno()
    offset = 0
    while offset < len(view):
        await anyio.wait_writable(fd)
        self._raise_if_closed()
        try:
            written = self._backend.write_nonblocking(view[offset:])
        except InterruptedError:
            continue
        except BlockingIOError:
            continue
        if written <= 0:
            continue
        offset += written
```

### 12.3 Close path

```python
async def aclose(self) -> None:
    async with self._close_lock:
        if self._closed:
            return
        self._closed = True
        fd = self._backend.fileno()
        with anyio.CancelScope(shield=True):
            anyio.notify_closing(fd)     # wakes pending wait_readable / wait_writable
            self._backend.close()        # sync: os.close, restore latency timer, etc.
```

### 12.4 Handling syscalls that would block

The backend's hot path (`read_nonblocking`, `write_nonblocking`) is non-blocking by construction — the fd is `O_NONBLOCK` and the syscall either returns immediately or raises `BlockingIOError`. No worker thread needed; readiness waiting happens in `SerialPort`'s async loop.

Most control-path syscalls (`tcsetattr`, `FIONREAD`, `TIOCMGET`) are fast kernel operations that complete in microseconds and can run inline. But two POSIX operations would genuinely block for extended periods: `tcdrain` and `tcsendbreak`. For each, we prefer an async reformulation over a worker-thread dispatch:

| Operation | Approach | Why |
|---|---|---|
| `read_nonblocking` / `write_nonblocking` | Inline; `O_NONBLOCK` fd + readiness loop | Never blocks; this is the hot path |
| `fileno`, `input_waiting`, `output_waiting` | Inline | `FIONREAD` / `TIOCINQ` / `TIOCOUTQ`, ~µs |
| `configure` (`tcsetattr` + ioctls) | Inline | Driver applies synchronously, ~µs |
| `modem_lines`, `set_control_lines` | Inline | `TIOCMGET` / `TIOCMBIS` / `TIOCMBIC`, ~µs |
| `reset_input_buffer`, `reset_output_buffer` | Inline | `tcflush`, ~µs |
| **`send_break`** | **Async: `TIOCSBRK` + `anyio.sleep` + `TIOCCBRK`** | Clean async equivalent of `tcsendbreak` (§12.4.1) |
| **`drain`** | **Async: poll `TIOCOUTQ` + `anyio.sleep`** | Avoids blocking `tcdrain` for most cases (§12.4.2) |
| `drain_exact` (opt-in) | `anyio.to_thread.run_sync(backend.tcdrain_blocking)` | True `tcdrain` semantics when UART-FIFO timing matters |
| `close` (`os.close` + latency-timer restore) | Inline, inside shielded scope | ~µs |
| `open` | `anyio.to_thread.run_sync` | Usually fast, but USB adapter negotiation can be slow |

#### 12.4.1 `send_break` — async via `TIOCSBRK` / `TIOCCBRK`

`tcsendbreak(fd, duration)` blocks for the break duration. The tty driver exposes two instantaneous ioctls that let us replace it with a cancellable async sleep:

```python
async def send_break(self, duration: float = 0.25) -> None:
    with self._send_guard:
        self._raise_if_closed()
        fd = self._backend.fileno()
        fcntl.ioctl(fd, termios.TIOCSBRK)        # start break — ~µs
        try:
            await anyio.sleep(duration)          # cancellable async wait
        finally:
            fcntl.ioctl(fd, termios.TIOCCBRK)    # stop break — ~µs
```

Strictly better than `tcsendbreak`: cancellable, non-blocking, no worker thread. The `finally` guarantees the break is de-asserted even if the coroutine is cancelled mid-sleep. Supported on Linux, macOS, and the BSDs.

#### 12.4.2 `drain` — async via `TIOCOUTQ` polling

`tcdrain(fd)` blocks until the kernel output queue is empty. Polling `TIOCOUTQ` gives the same information and lets us wait asynchronously:

```python
async def drain(self) -> None:
    with self._send_guard:
        self._raise_if_closed()
        bps = max(self._config.baudrate // 10, 1)   # ~10 bits per byte framed
        while True:
            self._raise_if_closed()
            pending = self._backend.output_waiting()   # TIOCOUTQ, ~µs
            if pending == 0:
                return
            wait_s = max(pending / bps, 0.001)
            await anyio.sleep(min(wait_s, 0.050))      # cap per-poll interval
```

Fully async, cancellable, no worker thread. Typical call does 2–5 polls; per-poll cost is one ioctl.

**Semantic difference from `tcdrain`.** `TIOCOUTQ == 0` means the kernel buffer is empty, but 16–64 bytes may still be in the UART hardware FIFO that `tcdrain` would wait for. At typical baud rates the FIFO drains in 1–5 ms — invisible for most uses (closing the port, waiting before a buffer flush, synchronization in user-level protocols).

For the narrow cases where FIFO-drain timing matters — most importantly **user-space RS-485 transceiver direction switching** — we expose an opt-in method:

```python
async def drain_exact(self) -> None:
    """True tcdrain semantics: waits for UART FIFO to empty too.
    Blocks in a worker thread. Use when FIFO timing matters (rare)."""
    with self._send_guard:
        self._raise_if_closed()
        await anyio.to_thread.run_sync(self._backend.tcdrain_blocking)
```

When Linux kernel-level RS-485 (`TIOCSRS485`, §19) is in use, the kernel handles direction switching internally and `drain_exact` is not needed — the kernel already waits for the FIFO correctly. `drain_exact` is the escape hatch for user-space RS-485 emulation or other exotic timing requirements.

#### 12.4.3 Non-goal: never thread the hot path

We do NOT dispatch `read_nonblocking` / `write_nonblocking` to a worker thread. That would reintroduce the per-op threadpool overhead that makes `pyserial-asyncio` slow. The whole point of the sync-backend + readiness-loop pattern is to keep the hot path cheap and thread-free.

#### 12.4.4 `anyio.to_thread.run_sync` discipline

Where we do use it (`drain_exact`, `open`), AnyIO's `CapacityLimiter` caps concurrent thread usage. We pass `cancellable=False` for tty syscalls — most can't be interrupted safely, and AnyIO will correctly detach the thread on cancellation so the coroutine returns promptly even if the syscall hasn't finished.

### 12.5 `AsyncSerialBackend` path (Windows, future)

If the backend implements `AsyncSerialBackend` instead of `SyncSerialBackend`, `SerialPort` detects this at open time (via `runtime_checkable` Protocol check or a backend-declared flag) and delegates directly: `receive` awaits `backend.receive(n)`, `send` awaits `backend.send(data)`, with the same resource guards and close lock wrapped around it. The readiness loop is skipped — the backend owns its own async I/O primitives (overlapped I/O, worker-thread bridge, etc.). The to-thread dispatch for blocking ops is the backend's responsibility in this case, not `SerialPort`'s. See §25 for the Protocol split and §24.5 for the Windows strategy.

---

## 13. Read Semantics

`receive(max_bytes)` honors AnyIO `ByteStream`:

- Returns at most `max_bytes`.
- Returns as soon as at least one byte is available.
- Does not return `b""` during normal operation.
- Supports cancellation.
- Raises on closed or broken resources.

### 13.1 Serial-specific helpers

| Method | Behavior |
|---|---|
| `receive_exactly(n)` | Loops `receive` until `n` bytes collected. Raises `EndOfStream` if closed mid-read. |
| `receive_until(delim, max_bytes=None)` | Reads until `delim` found. Raises `UnsupportedOperation` or a size-limit error if `max_bytes` exceeded. |
| `receive_available(limit=None)` | One readiness wakeup; drains up to `TIOCINQ`/`FIONREAD` bytes in a single `os.read`. Returns `b""` only when no bytes are available after wakeup. Useful for latency-sensitive request/response protocols. |
| `receive_into(buf)` | Reads directly into caller-owned buffer. Zero-allocation path. Returns bytes read. |

### 13.2 Buffering

The core stream is **unbuffered from the user's perspective** — one `receive()` waits for readiness and reads from the OS. The library does not inject an internal readahead buffer in the core path. Users who want line buffering or length-prefix semantics use AnyIO's buffered wrappers:

```python
from anyio.streams.buffered import BufferedByteStream
buffered = BufferedByteStream(port)              # full-duplex wrapper (AnyIO 4.10+)
line = await buffered.receive_until(b"\n", max_bytes=4096)
hdr = await buffered.receive_exactly(8)
await buffered.send(payload)
```

`BufferedByteStream(stream)` is bidirectional and forwards `send()` / `send_eof()` to the underlying stream. `BufferedByteReceiveStream` is the receive-only variant. `receive_until` requires an explicit `max_bytes` and raises `DelimiterNotFound` / `IncompleteRead` on failure.

For high-throughput users:

- `receive_into()` avoids allocation.
- `read_chunk_size` in `SerialConfig` controls the default max-read size per syscall.
- Internal reusable scratch buffers where measurably beneficial (and never user-observable).

**Rationale.** A hidden internal buffer violates the "one way to do each thing" principle and surprises users when cancellation is observed but buffered bytes were already consumed from the kernel. AnyIO's composable stream helpers handle this cleanly, and `BufferedByteStream` offers exactly the API surface users want without duplicating it on `SerialPort`.

---

## 14. Write Semantics

### 14.1 `send(item: bytes)` and `send_buffer(data: BytesLike)`

`send` matches `anyio.abc.ByteSendStream` exactly: accepts `bytes`, writes the full buffer unless cancelled or an error occurs, handles partial writes internally, runs under `_send_guard`.

`send_buffer` is the serial-specific zero-copy variant: accepts any `collections.abc.Buffer` (PEP 688) — `bytes`, `bytearray`, `memoryview`, `array.array`, numpy arrays exposing the buffer protocol, etc. Both methods share one internal `memoryview`-based write loop. `send` wraps its `bytes` argument in a `memoryview` and delegates.

### 14.2 `send_break(duration)`

Implemented as `TIOCSBRK` + `anyio.sleep(duration)` + `TIOCCBRK` (§12.4.1), not `tcsendbreak`. Fully async, cancellable, and the break is guaranteed to be de-asserted via `finally` even if cancelled mid-sleep. Raises `UnsupportedFeatureError` on platforms that lack the break ioctls, per `unsupported_policy`.

### 14.3 `drain()` and `drain_exact()`

- **`drain()`** — default method. Polls `TIOCOUTQ` with `anyio.sleep` until the kernel output queue is empty (§12.4.2). Fully async, cancellable, no worker thread. `TIOCOUTQ == 0` means the kernel buffer is empty; up to ~64 bytes may still be in the UART hardware FIFO (drains in 1–5 ms typically).
- **`drain_exact()`** — opt-in method that dispatches `tcdrain` to a worker thread. Waits for UART-FIFO-complete semantics. Use for user-space RS-485 direction switching or other cases where FIFO-drain timing matters. When kernel-level RS-485 (§19) is configured, `drain_exact` is not needed — the kernel handles the FIFO wait internally.

Neither is called automatically by `send`. Callers drain explicitly when they need to know bytes have left the kernel.

### 14.4 `send_eof()`

Serial ports have no true half-close (unlike TCP sockets). The method exists for `anyio.abc.ByteStream` contract compliance and behaves as follows:

- **Idempotent.** Calling it twice is a no-op.
- **Drains pending output** via `await self.drain()` (the async `TIOCOUTQ`-polling variant; §14.3). Fast and cancellable. Gives generic AnyIO code a sensible "I'm done sending for now" behavior without blocking the event loop.
- **Does not signal anything to the device** — there is no serial equivalent of TCP `FIN`.
- **Does not close the port.** The port remains fully usable; `send()` and `receive()` still work.
- Logs at DEBUG level: "serial has no true half-close; send_eof drained output."

Users wanting a real shutdown call `await port.drain()` then `await port.aclose()`.

### 14.5 Cancellation semantics

- `send()` is cancellable at every `wait_writable` checkpoint.
- **Cancellation during `send` may leave a partial write on the wire.** The bytes written before cancellation *have* hit the kernel's output buffer and will be transmitted. This is an inherent property of serial I/O — there is no way to un-send bits already clocked out. Documented prominently in the API reference and troubleshooting docs.
- `receive()` is cancellable at every `wait_readable` checkpoint; partial reads are never delivered (the buffer either fills or the call raises cancellation).

---

## 15. Concurrency Model

Primitives (all from `anyio`):

```python
self._receive_guard  = anyio.ResourceGuard("reading from")
self._send_guard     = anyio.ResourceGuard("writing to")
self._configure_lock = anyio.Lock()
self._close_lock     = anyio.Lock()
```

**On `Lock(fast_acquire=True)`.** AnyIO provides a fast-path constructor arg for the uncontended case. Do not enable it by default — the configure and close locks are rarely contended, so the fast path buys nothing. Turn it on only if benchmarks show a measurable difference.

- One task may receive at a time. A second concurrent `receive()` raises `anyio.BusyResourceError`.
- One task may send at a time. Same contract on the send side.
- Concurrent send + receive is always allowed — serial ports are full-duplex.
- Configure is serialized by `_configure_lock`; it waits for, but does not abort, in-flight I/O on the opposite direction.
- Close is serialized by `_close_lock` and is idempotent. `aclose()` calls `anyio.notify_closing(fd)` (§11.3) to wake pending `wait_readable` / `wait_writable`, then closes the fd. In-flight `receive` / `send` wake with `ClosedResourceError`.
- `aclose()` wraps its critical section in `with anyio.CancelScope(shield=True):` so that a cancelled caller cannot leak an open fd.

**Task groups.** `SerialPort` does not spawn its own task group. Callers own concurrency (§11.7).

**No `__del__`-based cleanup.** Finalizer-driven `_close` paths are fragile at interpreter shutdown. `anyserial` requires explicit `aclose()` or context manager use. If the port is garbage-collected while open, a `ResourceWarning` is emitted via `warnings.warn` and the fd is closed synchronously as a best-effort — but `notify_closing` is not safe to call at finalization time, so any pending tasks are left to AnyIO's own cleanup.

**Thread safety.** A single `SerialPort` is bound to one event loop. Cross-thread access is unsupported; that is what `anyserial.sync` exists for (§7.3).

---

## 16. Termios Configuration

Termios handling is pure and composable. Each concern is a small function over an immutable termios-attrs tuple:

```python
def apply_raw_mode(attrs: TermiosAttrs) -> TermiosAttrs: ...
def apply_baudrate(attrs: TermiosAttrs, baudrate: int, ops: PlatformOps) -> tuple[TermiosAttrs, BaudPlan]: ...
def apply_byte_size(attrs: TermiosAttrs, byte_size: ByteSize) -> TermiosAttrs: ...
def apply_parity(attrs: TermiosAttrs, parity: Parity) -> TermiosAttrs: ...
def apply_stop_bits(attrs: TermiosAttrs, stop_bits: StopBits) -> TermiosAttrs: ...
def apply_flow_control(attrs: TermiosAttrs, flow: FlowControl) -> TermiosAttrs: ...
def apply_hangup(attrs: TermiosAttrs, hangup_on_close: bool) -> TermiosAttrs: ...
```

The builders stay side-effect-free: each returns new termios bits or raises `UnsupportedFeatureError` when the running platform's `termios` module lacks the needed constant. Capability-driven decisions (which features to request, which to skip per `unsupported_policy`) live at the backend orchestrator layer below, not inside the builders.

The backend orchestrates:

```python
def configure_fd(fd: int, config: SerialConfig, ops: PlatformOps) -> None:
    original = termios.tcgetattr(fd)
    attrs, plan = build_termios_attrs(original, config, ops.capabilities)
    termios.tcsetattr(fd, termios.TCSANOW, attrs)
    if plan.custom_baudrate is not None:
        ops.set_custom_baudrate(fd, plan.custom_baudrate)
```

Every `apply_*` function is pure and fully unit-testable without hardware.

### 16.1 Python stdlib `termios` gaps

CPython's `termios` module wraps `<termios.h>` but only surfaces a subset of the constants the kernel defines. The gaps we hit during M2, and the policy for each, are:

| Constant | Kernel has it? | Python exposes? | Our handling |
|---|---|---|---|
| `CMSPAR` (mark/space parity) | Linux, newer BSD | No | Pure `apply_parity` raises `UnsupportedFeatureError`; a Linux-only hardcoded fallback (`0o10000000000`) can land later for users who need it. Darwin never had this — raises by design. |
| `TIOCSBRK` / `TIOCCBRK` (break assert/de-assert ioctls) | Every POSIX | No | `_posix/ioctl.py` has a Linux numeric fallback (`0x5427` / `0x5428`) routed through a probe that checks `termios` first. macOS / BSD values land in M6. |
| `TCGETS2` / `TCSETS2` (custom baud) | Linux | No | `_linux/baudrate.py` hardcodes the `_IOR` / `_IOW` encoded numbers (`0x802C542A` / `0x402C542B`) and the `struct termios2` layout. Stable kernel ABI; pySerial relies on the same numbers. |

**Policy.** Helpers probe `getattr(termios, NAME, None)` first, then fall back to a hardcoded platform-specific number gated on `sys.platform`. Request codes unreachable on the running platform raise `UnsupportedFeatureError` at call time, not at import. Struct sizes are asserted == the kernel-ABI value at import — any drift surfaces as a clean error rather than a mysterious kernel fault.

**No ctypes in the hot path.** Where we need a kernel-ABI constant the stdlib doesn't expose, we hardcode the integer and use `fcntl.ioctl` with `struct.pack` / `struct.unpack`. ctypes adds per-call marshalling overhead that's larger than a native C-extension boundary, so reaching for it would be a performance regression on top of an ergonomic one.

---

## 17. Baud Rate Support

Three baud categories:

1. **Standard termios constants** — `B9600`, `B115200`, etc.
2. **Platform-extended constants** — Linux `B4000000`, etc.
3. **Custom baud rates** — arbitrary integer via platform-specific mechanism.

| Platform | Custom-baud mechanism |
|---|---|
| Linux | `TCGETS2` / `TCSETS2` with `BOTHER` flag |
| Darwin | `IOSSIOSPEED` ioctl |
| BSD | Passthrough literal baud (with care; tested per BSD variant) |
| Generic POSIX | `UnsupportedConfigurationError` for non-standard rates |

If a custom rate is requested and the platform or device rejects it, `UnsupportedConfigurationError` is raised (or warned per `unsupported_policy`).

---

## 18. Low-Latency Design

Low latency is an explicit feature, not a byproduct.

```python
SerialConfig(baudrate=115200, low_latency=True)
```

### 18.1 Linux implementation
- `TIOCGSERIAL` / `TIOCSSERIAL` with `ASYNC_LOW_LATENCY` flag.
- Optional FTDI-specific auto-tuning: detect via `/sys/class/tty/<name>/device/driver`; reduce the latency timer via sysfs `/sys/bus/usb-serial/devices/<name>/latency_timer` to 1 ms when the device exposes it.
- **Restore on close.** If the library modified the `ASYNC_LOW_LATENCY` flag or the FTDI `latency_timer` value, `aclose()` restores the original value. Leaving a device in a modified state across application runs is rude and surprising; saving the original and restoring it on close is a correctness-and-courtesy requirement.
- Unsupported ioctl raises `UnsupportedFeatureError` by default (configurable via `unsupported_policy`).

### 18.2 Darwin / BSD
- No direct equivalent; request with `low_latency=True` either raises (default) or is a no-op (best-effort policy).

### 18.3 Documentation caveats
The docs spell out what low-latency mode *cannot* fix:

- USB adapter firmware latency.
- Kernel scheduling jitter.
- Wire time at low baud rates.
- Event-loop overhead (use `uvloop` if it matters).

---

## 19. RS-485 Support

RS-485 support is important for industrial applications but is platform and driver dependent.

### 19.1 Linux
Uses `TIOCSRS485` ioctl with `serial_rs485` struct populated from `RS485Config`. If the driver does not support it (most USB-serial adapters), `UnsupportedFeatureError` is raised.

### 19.2 Darwin / BSD
Not supported in the initial release — `UnsupportedFeatureError` by default.

### 19.3 Manual RTS toggling
Manual RTS toggling around writes is **not** offered in the core path — it is timing-sensitive and not equivalent to kernel RS-485. If a user explicitly needs it, they can implement it with `set_control_lines` and `drain`.

---

## 20. Modem & Control Lines

```python
@dataclass(frozen=True, slots=True)
class ModemLines:
    cts: bool
    dsr: bool
    ri: bool
    cd: bool
```

Methods:

```python
async def get_modem_lines(self) -> ModemLines: ...
async def set_control_lines(
    self,
    *,
    rts: bool | None = None,
    dtr: bool | None = None,
) -> None: ...
```

Internally: `TIOCMGET`, `TIOCMBIS`, `TIOCMBIC` on POSIX. `None` means "leave unchanged."

---

## 21. Timeouts

AnyIO cancellation scopes are the canonical pattern:

```python
with anyio.fail_after(0.5):
    data = await port.receive(1024)
```

No per-call `timeout` parameter is added to the async API — it would duplicate AnyIO.

The sync API (§7.3) exposes optional `timeout` arguments that are implemented internally as `fail_after` scopes.

---

## 22. Event Loop Performance

The library should not require users to pick asyncio, Trio, or uvloop. Benchmarks measure all three (§26).

- **asyncio default**: baseline.
- **asyncio + uvloop**: better event-loop overhead, helpful for many-ports scenarios and request/response latency.
- **Trio**: excellent cancellation semantics; performance comparable to asyncio for single-port workloads.

**Never call `uvloop.install()` inside the library.** Document how users enable it:

```python
anyio.run(main, backend="asyncio", backend_options={"use_uvloop": True})
```

Expected observations:

- At low baud, wire time dominates and backend choice is irrelevant.
- At high baud, allocation and syscall behavior matter more than loop choice.
- For request/response protocols at 115200, adapter latency typically dominates; `low_latency=True` helps more than `uvloop`.
- For many concurrent ports, `uvloop` measurably wins.

---

## 23. Port Discovery

Discovery is available but not part of the performance path.

```python
ports = await list_serial_ports()
for p in ports:
    print(p.device, p.description, p.vid, p.pid, p.serial_number)

match = await find_serial_port(vid=0x0403, pid=0x6001)
```

**Why async.** Discovery performs filesystem and platform metadata I/O (sysfs walks on Linux, IOKit calls on macOS, possibly slow USB-bus enumeration). Making it async keeps the AnyIO-first promise honest and lets users run discovery inside cancellation scopes. The sync wrapper (M7) will expose synchronous aliases for scripts.

Data model:

```python
@dataclass(frozen=True, slots=True, kw_only=True)
class PortInfo:
    device: str
    name: str | None = None
    description: str | None = None
    hwid: str | None = None
    vid: int | None = None
    pid: int | None = None
    serial_number: str | None = None
    manufacturer: str | None = None
    product: str | None = None
    location: str | None = None
    interface: str | None = None
```

### 23.1 Backends

| Platform | Primary mechanism | Fallback |
|---|---|---|
| Linux | sysfs scan (`/sys/class/tty`, resolve device paths, parse USB metadata) | `pyudev` optional extra; `pyserial.tools.list_ports` optional extra |
| Darwin | IOKit via `ctypes` + `CoreFoundation` | `pyserial.tools.list_ports` optional extra |
| BSD | `/dev` scan + `sysctl` for USB metadata | `pyserial.tools.list_ports` optional extra |

Native Linux discovery is first-class. pySerial discovery is available via the `anyserial[discovery-pyserial]` extra for users who want to reuse existing behavior.

Discovery is **always live** — no caching. Caching is a user-side concern.

---

## 24. Platform Specifics

### 24.1 Linux (`_linux/`)
- Custom baud via `TCGETS2`/`TCSETS2` + `BOTHER`.
- `low_latency=True` via `TIOCGSERIAL`/`TIOCSSERIAL` + `ASYNC_LOW_LATENCY`; FTDI latency timer via sysfs.
- `TIOCSRS485` for RS-485.
- Discovery via sysfs (+ `pyudev` optional).

### 24.2 Darwin (`_darwin/`)
- Custom baud via `IOSSIOSPEED`.
- Flow-control constants differ from Linux (`CCTS_OFLOW`/`CRTS_IFLOW` vs `CRTSCTS`).
- Discovery via IOKit through `ctypes`.
- `low_latency` not supported — `UnsupportedFeatureError` by default.

### 24.3 BSD (`_bsd/`)
- Passthrough baud constants.
- FreeBSD/NetBSD/OpenBSD share enough logic for one backend with conditional imports.
- Hardware testing required before any BSD-specific claim.

### 24.4 Generic POSIX (`_posix/`)
- Fallback for unknown POSIX variants. Vanilla termios only. Documented best-effort.

### 24.5 Windows (`_windows/`, future)
- Deferred. **Requires a written design review before any implementation work lands** — the Windows serial model is not a simple port of POSIX readiness, and a hasty implementation will either underperform or fight the Windows kernel. The design-review doc must pick one of the options below, justify the choice with benchmark targets, and be approved before M10 starts.
- **Implements `AsyncSerialBackend`** (§25.2), not `SyncSerialBackend`. Windows owns its own async I/O primitives and does not participate in `anyio.wait_readable` dispatch. `SerialPort` detects the Protocol at open time and uses the async-backend dispatch path (§12.4, §25.3).
- Design keeps options open:
  1. Overlapped I/O via `ctypes` or `pywin32`, driven by a dedicated worker thread that completes `OVERLAPPED` structures and signals AnyIO via `anyio.from_thread.run_sync` (or via a `MemoryObjectReceiveStream` fed by the worker).
  2. Worker-thread bridge around blocking Windows serial APIs (simplest; lowest performance ceiling).
  3. pySerial-backed fallback (delegates to `pyserial` and wraps its sync API in a worker thread).
- `anyio.wait_readable` / `anyio.wait_writable` **do not work on Windows COM-port HANDLEs** — AnyIO documents that they accept only `SOCKET` handles on Windows. This is exactly why Windows uses `AsyncSerialBackend` and owns its own async primitives.
- Public API **does not expose** `.fd` as a required property — it is only available on backends that satisfy `SyncSerialBackend` (via `port.extra(FileStreamAttribute.fileno)`). Windows backends simply don't publish that typed attribute.

---

## 25. Backend Protocol

The platform boundary is **two Protocols**, one for each I/O dispatch model. `SerialPort` detects which one a backend satisfies and picks the matching I/O path.

### 25.1 `SyncSerialBackend` — OS primitives (POSIX)

Backends that own an `O_NONBLOCK` fd and rely on the caller to do async readiness waiting. This is the primary Protocol — every POSIX backend (Linux, Darwin, BSD, generic POSIX) and `MockBackend` implement it. **Zero AnyIO imports in these files.**

```python
from typing import Protocol, runtime_checkable
from collections.abc import Buffer

@runtime_checkable
class SyncSerialBackend(Protocol):
    @property
    def path(self) -> str: ...
    @property
    def is_open(self) -> bool: ...
    @property
    def capabilities(self) -> SerialCapabilities: ...

    # Lifecycle — sync, no async work to do
    def open(self, path: str, config: SerialConfig) -> None: ...
    def close(self) -> None: ...

    # OS-primitives hot path — nonblocking; caller owns readiness waiting
    def fileno(self) -> int: ...
    def read_nonblocking(self, buffer: bytearray | memoryview) -> int: ...
    def write_nonblocking(self, data: memoryview) -> int: ...

    # Configuration + control — sync termios/ioctl calls (all fast)
    def configure(self, config: SerialConfig) -> None: ...
    def reset_input_buffer(self) -> None: ...              # tcflush(TCIFLUSH)
    def reset_output_buffer(self) -> None: ...             # tcflush(TCOFLUSH)
    def set_break(self, on: bool) -> None: ...             # TIOCSBRK / TIOCCBRK (no duration; SerialPort owns the sleep)
    def tcdrain_blocking(self) -> None: ...                # blocking tcdrain; only called via to_thread for drain_exact

    # Modem / control lines
    def modem_lines(self) -> ModemLines: ...
    def set_control_lines(self, *, rts: bool | None = None, dtr: bool | None = None) -> None: ...

    # Snapshots
    def input_waiting(self) -> int: ...                    # FIONREAD / TIOCINQ
    def output_waiting(self) -> int: ...                   # TIOCOUTQ
```

**Contract for `read_nonblocking` / `write_nonblocking`:**

- `read_nonblocking(buf)` reads into `buf` via one `os.read` call. Returns bytes read. Raises `BlockingIOError` on EAGAIN, `InterruptedError` on EINTR. Returns 0 only on genuine EOF / disconnect.
- `write_nonblocking(data)` writes via one `os.write` call. Returns bytes written (may be short). Same EAGAIN / EINTR semantics.
- Neither method ever blocks — the fd is opened `O_NONBLOCK`. If the kernel has no data / buffer space, the call raises `BlockingIOError` immediately.
- Cancellation is not the backend's concern. `SerialPort` is already inside `anyio.wait_readable` / `wait_writable` when it calls these methods.

### 25.2 `AsyncSerialBackend` — self-contained async I/O (Windows, future)

Backends that own their own async primitives and cannot be driven by fd-based readiness. Windows COM HANDLEs, hypothetical network-bridged serial, or any platform where `anyio.wait_readable(fd)` is not meaningful.

```python
@runtime_checkable
class AsyncSerialBackend(Protocol):
    @property
    def path(self) -> str: ...
    @property
    def is_open(self) -> bool: ...
    @property
    def capabilities(self) -> SerialCapabilities: ...

    async def open(self, path: str, config: SerialConfig) -> None: ...
    async def aclose(self) -> None: ...

    async def receive(self, max_bytes: int) -> bytes: ...
    async def receive_into(self, buffer: bytearray | memoryview) -> int: ...
    async def send(self, data: memoryview) -> None: ...

    async def configure(self, config: SerialConfig) -> None: ...
    async def reset_input_buffer(self) -> None: ...
    async def reset_output_buffer(self) -> None: ...
    async def drain(self) -> None: ...
    async def send_break(self, duration: float) -> None: ...

    async def modem_lines(self) -> ModemLines: ...
    async def set_control_lines(self, *, rts: bool | None = None, dtr: bool | None = None) -> None: ...

    def input_waiting(self) -> int: ...
    def output_waiting(self) -> int: ...
```

The Windows backend (§24.5, M10) will implement this Protocol by whatever mechanism fits best: overlapped I/O via ctypes, a worker-thread bridge around blocking Windows APIs, or a pySerial-backed fallback. The contract only demands that `receive` / `send` honor AnyIO cancellation and that `aclose` is idempotent.

### 25.3 `SerialPort` dispatch

```python
async def open_serial_port(path: str, config: SerialConfig) -> SerialPort:
    backend = _select_backend(path, config)   # platform factory
    if isinstance(backend, SyncSerialBackend):
        backend.open(path, config)            # sync open is fine
        return _PosixSerialPort(backend)      # uses wait_readable loop
    if isinstance(backend, AsyncSerialBackend):
        await backend.open(path, config)
        return _AsyncBackendSerialPort(backend)  # delegates directly
    raise UnsupportedPlatformError(...)
```

Both private `SerialPort` variants expose the same public `SerialPort` interface (§7.2) — callers never see the difference. Resource guards, close locks, configure locks, typed attributes, and exception mapping are shared between the two variants via a common base; only the read/send hot paths differ.

### 25.4 Why two Protocols, not one

A single Protocol forces a bad compromise: all-async POSIX means every backend imports AnyIO and `async def close()` has nothing to await; all-sync Windows means wrapping overlapped I/O in a fake `read_nonblocking`. Two Protocols separate the concerns — POSIX backends are pure OS mechanics with no async dependency; non-fd platforms implement async natively — and `SerialPort` absorbs a ~10-line dispatch to keep the public API uniform. Appendix G has the full argument.

### 25.5 Adding a new backend

- **New POSIX platform** (WASI, QNX, hypothetical): implement `SyncSerialBackend`. No AnyIO import needed. `MockBackend` is the template.
- **New non-fd platform**: implement `AsyncSerialBackend`. The contract is essentially `ByteStream`-shaped, so wrapping an existing async API is usually a direct mapping.

Either way, no changes to core `SerialPort` code. The dispatch in `open_serial_port` handles the rest.

---

## 26. Performance Strategy

### 26.1 Targets

| Metric | Target | Notes |
|---|---|---|
| pty single-byte round-trip p50 | < 200 µs | asyncio+uvloop on Linux; revisit after baseline |
| pty single-byte round-trip p99 | tracked, no hard first target | p99 is heavily kernel- and scheduler-dependent; track trends not absolutes until we have baseline data |
| Syscall rate for `receive(1)` bursts | 1 per `receive_available()` call | With `receive_available` helper |
| Throughput at 4 Mbaud, pty | ≥ 90% of line rate | Bulk-transfer bench; hardware-dependent |
| Allocation per `receive_into()` | zero payload allocation | Hot path |
| CPU at 115200 baud continuous | < 2% of one core | Best effort, steady-state |
| Cancellation latency | < 1 ms from cancel to raise | All backends |
| Regression threshold | 10% from previous baseline | Benchmark gate |

Targets are revised after we have baseline data. p99 and CPU numbers especially are best-effort until we can measure on reference hardware.

### 26.2 Optimizations (priority order)
1. Native `anyio.wait_readable(fd)` — no socket wrapper, no sniffio dispatch.
2. Single `os.read(fd, read_chunk_size)` per readiness wakeup.
3. `receive_into()` zero-allocation path for hot loops.
4. `receive_available()` to drain `TIOCINQ`/`FIONREAD` bytes per wakeup.
5. Low-latency termios + FTDI timer tuning.
6. `memoryview` throughout the write path.
7. Optional `uvloop`, documented.
8. `os.readv`/`os.writev` scatter-gather — deferred; add only if benchmarks justify.

### 26.3 Rejected techniques

Documented explicitly so future contributors don't relitigate:

- **`io_uring`.** Immature Python bindings, complexity far exceeds benefit at serial speeds. Revisit in 2–3 years.
- **C extension in hot path.** CPython `os.read` overhead ≈ 1 µs; not the bottleneck at any serial rate Python is used for.
- **Custom event loop.** `uvloop` / `winloop` cover it via AnyIO.
- **Internal ring-buffer readahead in core.** Surprises cancellation and violates "one way to do each thing." Users who want buffering use AnyIO's `BufferedByteStream`.
- **Free-threaded Python 3.13t optimizations.** This workload is I/O-bound.
- **Socket wrappers around serial fds** — historically a hot-path issue; the fd-generalized AnyIO API makes them unnecessary.
- **`sniffio` backend branching.** AnyIO 4.12 dropped `sniffio` as a direct dependency. The unified AnyIO public API covers every backend-specific need. Any library still doing `sniffio.current_async_library()` dispatch on modern AnyIO is carrying legacy weight.
- **Explicit `anyio.lowlevel.checkpoint()` in I/O loops.** `wait_readable` / `wait_writable` are themselves checkpoints; scattering additional `checkpoint()` calls costs extra yields and obscures the cancellation model. Only use `checkpoint()` in CPU-bound loops that never await.
- **`anyio.wait_socket_readable` / `anyio.wait_socket_writable`.** Deprecated in AnyIO 4.7. The fd-generalized `wait_readable` / `wait_writable` supersede them.

---

## 27. Testing Strategy

Testing is designed before implementation.

### 27.1 Unit tests — `MockBackend`

A fully featured in-memory loopback backend lives at `anyserial.testing.MockBackend`. It implements the `SyncSerialBackend` Protocol and is the primary unit-test fixture. **Because the backend is pure sync, `MockBackend` is a plain class with no AnyIO dependency** — backend-level tests can run in a synchronous pytest function, and async tests against `SerialPort` drive the mock transparently through the same readiness loop the POSIX backends use. Creates a pair via `socketpair`-style API: `a, b = MockBackend.pair()`.

Capabilities:
- Paired loopback (tx of one port appears on rx of the partner) — uses a real `socketpair` internally so `wait_readable` actually fires.
- Configurable simulated latency and bandwidth cap (injected at the pair level, not inside the backend).
- Fault injection: disconnect, parity error, EAGAIN storms, partial writes, EINTR bursts — all modeled via the sync `read_nonblocking` / `write_nonblocking` contract.
- Deterministic sequencing for reproducible tests.

Benefits over pty-based tests:
- Fast (no OS kernel tty path).
- Deterministic (no timing-dependent flakes).
- Cross-platform (runs on macOS CI too, not just Linux).
- Easy to exercise error paths (disconnect, EIO) that real kernels won't produce on demand.
- Backend unit tests are plain sync pytest functions — zero async setup.

Covered via MockBackend:
- Config validation, capability resolution, exception mapping.
- Lifecycle (open, close, aclose idempotency, double-open).
- Concurrency guards.
- Read/write loops including EINTR/EAGAIN retries and short writes.
- Cancellation at every `await` point.
- `receive_*` helper semantics.
- Runtime reconfiguration locking.

Target: 95% line coverage, 90% branch coverage.

### 27.2 Integration tests — Linux pseudoterminals

On Linux CI, `pty.openpty()` from the stdlib (or `socat -d -d pty,raw,echo=0 pty,raw,echo=0` for paired `/dev/pts/N` paths) creates real pty pairs for exercising the genuine termios path. A shared `pty_port` fixture lives in `tests/integration/conftest.py`.

Covered:
- Open/close through the real fd path.
- One-byte and multi-byte reads/writes, including ≥16 KiB bulk transfers that exercise the partial-write path.
- Full-duplex concurrent send + receive.
- ResourceGuard on concurrent reads (second reader raises `BusyResourceError`).
- Cancellation during receive (`fail_after`), port remains usable afterwards.
- Close while receiving — `notify_closing(fd)` wakes the parked reader with `ClosedResourceError`.
- `reset_input_buffer`, `reset_output_buffer`, `drain`, `send_eof` on a real tty.
- `receive_available()` against real kernel queues; `receive_into()` zero-copy path.
- Exclusive access via `flock(LOCK_EX | LOCK_NB)` — second open with `exclusive=True` raises `PortBusyError`.
- Custom baud via `TCSETS2` + `BOTHER` on Linux; `c_ispeed` / `c_ospeed` round-trip.

#### 27.2.1 Pty testing gotchas

Four pty quirks cost real debugging time during M2. Leaving them documented here so the next person doesn't re-learn them.

1. **Default line discipline is cooked.** `pty.openpty()` returns fds with `ICANON` + `OPOST` active — the kernel buffers writes on the controller side until a newline arrives, and `\n` is translated to `\r\n` on output. Byte-oriented tests hang waiting for data the kernel is withholding, or see inflated byte counts that don't match the payload length. Fixture solution: apply an inline `cfmakeraw` to the follower fd before closing it, so the tty state persists on the `/dev/pts/N` path for whatever fd reopens it next. Raw mode must include `CS8 | CREAD | CLOCAL` — plain `cfmakeraw` clears `PARENB` but leaves receiver/modem-line handling up to the caller.

2. **Controller fd is blocking by default.** `pty.openpty()` does not mark either end `O_NONBLOCK`. If a test task runs `os.read(controller, N)` while a sibling task in the same event loop is parked in `wait_writable(port_fd)`, the reader holds the loop hostage inside a blocking syscall and the writer never resumes to drain the pty buffer — deadlock. Fixture solution: `fcntl(controller, F_SETFL, flags | O_NONBLOCK)` up front; helpers poll on `BlockingIOError` with a short `anyio.sleep`.

3. **The Linux pty driver silently normalizes `CSIZE` and `PARENB`.** Requesting `CS7` or `PARENB` via `tcsetattr` on a pty succeeds, but a follow-up `tcgetattr` shows the kernel reset the bits back to `CS8` / no parity. Ptys don't implement 5/6/7-bit framing or parity hardware. Integration tests that assert on these fields against a pty will fail intermittently. **Policy:** byte-size and parity coverage lives in the pure-builder unit tests (which operate on in-memory bitflags, not a kernel pty). The pty integration tests pick flags that do round-trip — `CSTOPB`, `HUPCL`, `CREAD`, `CLOCAL`, baud, `CRTSCTS` — to prove the builder pipeline reaches `tcsetattr`.

4. **A second `os.open(follower_path)` on the same pty does not receive the controller's writes.** Data only flows to the originally-opened follower fd. Implications for test design:
   - **Lifecycle** (`open` / `close` / `configure` / `tcgetattr` round-trip): fine — open a second fd to the same `/dev/pts/N` path; termios operations work on both.
   - **Hot path** (`read_nonblocking` / `write_nonblocking` with real data flow): not fine — inject the original follower fd directly into a backend instance via a test helper. The `_backend_from_fd` pattern in `tests/integration/test_posix_backend.py` is the template.

### 27.3 Hardware tests (opt-in)

```bash
ANYSERIAL_TEST_PORT=/dev/ttyUSB0 pytest -m hardware
```

Marker-gated. Not run in default CI. Scenarios:
- USB serial loopback (pins 2–3 jumpered).
- High baud (up to adapter max).
- Low-latency mode measurement.
- RTS/CTS and DTR/DSR handshake exercise.
- RS-485 adapter if available.
- Disconnect mid-I/O (pull USB plug; fixture waits for `SerialDisconnectedError`).

### 27.4 Property tests (Hypothesis)
- Config validation invariants.
- Termios attr builder reversibility where applicable.
- `receive_into`/`receive_exactly`/`receive_until` round-tripping.
- Errno → exception mapping is total and unambiguous.

### 27.5 Type-check tests
- `mypy --strict` against the package and tests (CI-enforced).
- `pyright --warnings` as a secondary type checker — catches different classes of issues than mypy.
- Dedicated `tests/typing/` file with `reveal_type` and `assert_type` assertions for the public API.

**`warn_unreachable` + `sys.platform` narrowing.** mypy (and pyright) narrow `sys.platform == "linux"` and `sys.platform.startswith("linux")` at check time based on the host the analyzer is running on. An inline `if sys.platform.startswith("linux"): pytest.skip(...)` followed by any code looks "unreachable" on whichever platform the checker is invoked on, and `warn_unreachable` flags it. Two patterns defuse this without disabling the warning:

1. **Route through a `Final[bool]` constant.** mypy doesn't narrow arbitrary `Final` bools, only direct comparisons against the `sys.platform` string literal.

    ```python
    _IS_LINUX: Final[bool] = sys.platform.startswith("linux")

    if _IS_LINUX:
        return linux_specific_value
    return fallback          # stays reachable on every host
    ```

2. **Use `@pytest.mark.skipif(_IS_LINUX, reason=...)` instead of inline skips.** Skipif expressions are evaluated at collection time, not as narrowing-carrying type predicates, so post-skip code isn't considered unreachable.

Both patterns live in the current codebase — `_posix/ioctl.py` uses (1); the selector and stream tests use (2).

**Private-attribute access in tests.** Integration tests sometimes need to poke at `backend._fd` (e.g., fd injection for hot-path tests that bypass `open()`). pyright's `reportPrivateUsage` fires on these. Suppress per-line with `# pyright: ignore[reportPrivateUsage]` and a brief justification — a test-only classmethod on the backend would pollute the production surface for no gain.

### 27.6 AnyIO test backend matrix

AnyIO ships its pytest plugin inside the `anyio` package itself — **do not depend on a separate `pytest-anyio` package**. Use the built-in `anyio_backend` fixture, parametrized across the full backend matrix so every async test runs against asyncio, asyncio+uvloop, and trio:

```python
# tests/conftest.py
import pytest

@pytest.fixture(
    params=[
        pytest.param(("asyncio", {"use_uvloop": False}), id="asyncio"),
        pytest.param(("asyncio", {"use_uvloop": True}),  id="asyncio+uvloop"),
        pytest.param("trio",                             id="trio"),
    ]
)
def anyio_backend(request: pytest.FixtureRequest) -> object:
    return request.param
```

Tests use explicit `@pytest.mark.anyio` rather than any auto-async mode to avoid conflicts with other pytest async plugins that may be installed.

### 27.7 CI matrix

| Job | OS | Python | Notes |
|---|---|---|---|
| `lint` | ubuntu-latest | 3.13 | `ruff check` + `ruff format --check` |
| `typecheck` | ubuntu-latest | 3.13 | `mypy --strict` + `pyright --warnings` |
| `test-unit` | ubuntu-latest, macos-latest | 3.13, 3.14 | MockBackend only, full AnyIO backend matrix |
| `test-integration-linux` | ubuntu-latest | 3.13, 3.14 | socat pty pairs, full AnyIO backend matrix |
| `test-windows-smoke` | windows-latest | 3.13 | Package imports; no backend tests until M9 |
| `test-py315-dev` | ubuntu-latest | 3.15-dev | Allowed-failure until stable |
| `benchmark-nightly` | ubuntu-latest (self-hosted if available) | 3.13 | Regressions > 10% fail |
| `build` | ubuntu-latest | 3.13 | sdist + wheel |
| `docs` | ubuntu-latest | 3.13 | Zensical build |
| `publish` | ubuntu-latest | 3.13 | OIDC trusted publishing on tag |

---

## 28. Benchmark Strategy

Explicit, reproducible, machine-readable. Not intuition-driven.

### 28.1 Benchmarks
- p50/p95/p99 single-byte receive latency.
- Request/response round-trip latency.
- Small-chunk and large-chunk throughput.
- CPU usage at target baud.
- Allocation counts per operation (`tracemalloc`).
- Many-port scalability (2, 8, 32, 128 concurrent ports).
- Cancellation latency.

### 28.2 Environments
- Linux pty baseline.
- USB serial loopback.
- FTDI adapter (FT232R).
- CP210x adapter.
- CH340 adapter.
- Real target device when available.

### 28.3 Backend matrix
- AnyIO on asyncio (default).
- AnyIO on asyncio with uvloop.
- AnyIO on Trio.

### 28.4 Controls
- Baud rate.
- `read_chunk_size`.
- `low_latency` on/off.
- Adapter-specific latency timer.
- Message size.
- Request/response cadence.
- Number of concurrent ports.

### 28.5 Output
- Machine-readable JSON per run (committed to a `benchmarks/results/` archive).
- Human-readable Markdown summary.
- Regression detection comparing to previous release baseline (10% threshold).

### 28.6 Comparison
- Head-to-head with `pyserial-asyncio` and `trio_pyserial` on the same hardware.
- Results published in `docs/performance.md`.

---

## 29. Tooling, CI, Packaging

### 29.1 Build
- `hatchling` build backend.
- `hatch-vcs` for version-from-git-tag (no hand-maintained version).
- Pure-Python universal wheel.

### 29.2 Dev tooling
- **ruff** — lint + format. Rule sets: `E`, `F`, `W`, `B`, `I`, `UP`, `SIM`, `PL`, `RUF`, `PTH`, `TCH`. Line length 100.
- **mypy** — `--strict`.
- **pyright** — secondary type checker; catches issues mypy misses.
- **pre-commit** — ruff, ruff-format, mypy, check-yaml, check-toml, trailing-whitespace, no-merge-conflicts.
- **pytest** + AnyIO's built-in pytest plugin (not `pytest-anyio`) + `pytest-cov` + `hypothesis`.
- **Typing policy:** every public function annotated; narrow `# type: ignore[code]` with a written justification; `@override` on every Protocol implementation; `Self` for fluent returns; PEP 695 `type` aliases for complex unions; `py.typed` shipped.

### 29.3 `pyproject.toml` baseline

```toml
[build-system]
requires = ["hatchling", "hatch-vcs"]
build-backend = "hatchling.build"

[project]
name = "anyserial"
dynamic = ["version"]
requires-python = ">=3.13"
dependencies = ["anyio>=4.13"]

[project.optional-dependencies]
uvloop             = ["uvloop>=0.22.1; platform_system != 'Windows'"]
winloop            = ["winloop>=0.1;    platform_system == 'Windows'"]
trio               = ["trio>=0.33"]
discovery-pyudev   = ["pyudev>=0.24;    platform_system == 'Linux'"]
discovery-pyserial = ["pyserial>=3.5"]
test               = ["pytest", "coverage[toml]", "hypothesis"]
dev                = ["ruff", "mypy", "pyright", "pre-commit", "hatch", "hatch-vcs"]
docs               = ["zensical>=0.0.33"]
```

**Dependency rules:**

- `anyio` is the only required runtime dep. Floor is `>=4.13` (latest stable, April 2026) — we want every currently shipped AnyIO feature available, not the narrowest possible support window.
- **No `pytest-anyio`.** AnyIO ships its pytest plugin inside the `anyio` package. Tests use `pytest.mark.anyio` and the built-in `anyio_backend` fixture directly (see §27.7). Depending on a separate `pytest-anyio` package is a common mistake and would pull in an outdated fork.
- No `sniffio` — AnyIO 4.12+ does not use it, and neither do we.
- No unrelated type stubs (no `types-click`, `types-redis`, etc.).
- No `pyserial` in core; available as opt-in discovery extra only.
- Optional extras are justified per-dep: `uvloop`/`winloop` for users who want the faster event loop; `trio` for users who prefer it; `discovery-pyudev` for richer Linux metadata; `discovery-pyserial` for pyserial-backed fallback enumeration.

**AnyIO feature floor rationale.** 4.13 gets us every API referenced in this design: fd-generalized `wait_readable` / `wait_writable` (4.7), `notify_closing` (4.10), `BufferedByteStream` (4.10), `anyio.lowlevel.current_token()` + `CancelScope.cancel(reason=...)` (4.11), `get_available_backends()` + `sniffio`-drop + `winloop`-via-`use_uvloop=True` (4.12), and the 4.13 typing refinements. No reason to target an older floor.

### 29.4 Pre-release checks
- Changelog updated (Keep a Changelog format).
- All CI green.
- Benchmarks within 10% of previous release on reference hardware.

---

## 30. Documentation

Zensical, hosted on GitHub Pages, built in CI.

Required documents:

- `index.md` — what it is, when to use it.
- `quickstart.md` — 5-minute echo loop.
- `configuration.md` — full `SerialConfig` reference.
- `capabilities.md` — feature-support matrix by platform.
- `hardware-tuning.md` — cookbook for FTDI, CP210x, CH340; `dialout` group on Linux; exclusive-mode caveats.
- `low-latency.md` — what works, what doesn't, measurements.
- `discovery.md` — discovery usage; pyudev/pyserial extras.
- `performance.md` — published benchmark numbers, backend comparison, tuning guide.
- `api.md` — API reference generated from docstrings once the Zensical integration is adopted.
- `troubleshooting.md` — common errors and their causes.
- `migration-from-pyserial.md` — side-by-side examples.
- `changelog.md`.

README contains one realistic example + links. No duplication of full docs.

---

## 31. Logging & Diagnostics

Standard `logging` module. No prints. No payload bytes by default.

Logger namespaces:

- `anyserial` — root.
- `anyserial.posix`
- `anyserial.linux`
- `anyserial.darwin`
- `anyserial.bsd`
- `anyserial.discovery`
- `anyserial.performance`
- `anyserial.sync`

Useful DEBUG-level events:

- Selected backend.
- Opened port path + config (no payload).
- Applied config changes.
- Capability decisions.
- Low-latency feature applied or rejected.
- Failed optional features.
- Close / disconnect.

Per-byte / per-read logging is **not** enabled by default — gated behind an explicit diagnostic hook, measured for cost, and disabled unless the user opts in.

Format strings use `%` style so args aren't formatted unless the level is enabled:

```python
logger.debug("opened serial port %s with config %r", path, config)
# Not: logger.debug(f"opened serial port {path} with config {config!r}")
```

The f-string form formats the arguments even when DEBUG is disabled — measurable cost on the hot path.

### 31.1 Optional stats hook

Future opt-in diagnostics (not in the initial release; designed here so the shape is agreed):

```python
@dataclass(frozen=True, slots=True)
class SerialStats:
    bytes_read:     int
    bytes_written:  int
    read_syscalls:  int
    write_syscalls: int
    receive_waits:  int
    send_waits:     int
    eagain_count:   int
    eintr_count:    int
```

Enabled via `SerialConfig(..., collect_stats=True)` in a later minor release. When disabled (default), stats collection is a single `if not self._stats: return` guard — zero-cost on the hot path. Exposed via `port.stats()` returning a snapshot dataclass.

---

## 32. Security & Safety

Serial ports are local, but safety still matters.

- No shelling out anywhere in core.
- sysfs strings validated before use (no trust in filenames or symlinks).
- No blanket validation of user-supplied device paths — fail from the OS error naturally and map cleanly.
- Permissions requirements documented (Linux `dialout` group).
- Original `OSError`s preserved in `__cause__`.
- Raw serial payloads never logged at default levels.
- No mutation of process-global event-loop state.
- No mutation of tty settings outside the target fd.

---

## 33. Versioning & Release

- Semantic versioning. First release is `v0.1.0`.
- Versions come from git tags via `hatch-vcs`.
- PyPI publishing via GitHub Actions OIDC trusted publishing — no long-lived tokens.
- Public API deprecations go through one minor release of `DeprecationWarning` before removal.

---

## 34. Phased Delivery

All milestones below have shipped as part of the `v0.1.0` initial
release. Retained as a record of the delivery order and the exit
criterion each phase met.

| Milestone | Scope | Exit criteria (met) |
|---|---|---|
| **M0** Skeleton ✅ | Repo scaffold, CI, lint/typecheck/format, `py.typed`, docs skeleton, test skeleton, `anyio_backend` fixture wired up | Empty package installed; CI green on all backends |
| **M1** Core + MockBackend ✅ | `SerialConfig`, `FlowControl`, enums (`StrEnum`), exceptions (multi-inherited), `SerialCapabilities` with tri-state `Capability`, `SyncSerialBackend` + `AsyncSerialBackend` Protocols, `SerialPort`, `SerialConnectable`, typed attributes, `MockBackend` | Full MockBackend-driven unit test suite passed across all AnyIO backends |
| **M2** Linux POSIX core ✅ | `PosixBackend`, `LinuxBackend`, nonblocking fd read/write via `anyio.wait_readable`, `anyio.notify_closing` in close path, termios raw mode, standard + extended baud, pty integration tests | Linux echo round-trip worked end-to-end across asyncio/uvloop/trio |
| **M3** Linux features ✅ | RTS/DTR control lines, CTS/DSR/RI/CD modem lines, flush, break, exclusive access, custom baud (`TCSETS2`/`BOTHER`), low-latency mode with restore-on-close, capability reporting | All Linux-advertised features covered by tests; FTDI hardware test green |
| **M4** Discovery + benchmarks ✅ | Linux native sysfs discovery, `list_serial_ports`, `find_serial_port`, benchmark suite, backend matrix (asyncio/uvloop/trio), hardware test markers | Published bench numbers on one adapter; perf targets from §26.1 met |
| **M5** Runtime reconfig + RS-485 ✅ | `configure()` with config lock, Linux RS-485 (`TIOCSRS485`), `receive_available`, `receive_into` | Runtime reconfigure tested with pty and mocked ioctls; RS-485 worked on adapter |
| **M6** POSIX expansion ✅ | macOS backend (`IOSSIOSPEED`, IOKit discovery), BSD backend, macOS CI | macOS tests passed in CI |
| **M7** Sync wrapper ✅ | `anyserial.sync.SerialPort` via `BlockingPortalProvider`, optional `timeout=` per call, sync parity tests | API parity tests passed; sync wrapper benched vs. async |
| **M8** Documentation ✅ | Zensical site (overview, quickstart, configuration, capabilities, runtime reconfiguration, discovery, linux-tuning, RS-485, AnyIO backend selection, uvloop usage, cancellation, performance, hardware testing, troubleshooting, migration from pySerial, changelog) | Docs live on GitHub Pages |
| **M9** Initial release ✅ | Tag, PyPI publish via OIDC trusted publishing, announcement | Package installable from PyPI |
| **M10** Windows backend ✅ | Written design review (see §24.5), then `WindowsBackend` via runtime-native IOCP dispatch (Trio + asyncio Proactor), SetupAPI discovery, full Win32 error translation | Windows tests green on com0com; capability surface complete |

---

## 35. Open Design Decisions

Resolved decisions are captured in the body sections and summarized in Appendix I. Remaining opens:

1. **Package name.** Keep `anyserial` or pick a new name? Lean: **keep `anyserial`**.
2. **pySerial discovery.** Optional extra only, or an opt-in default fallback when native is not implemented for a platform? Lean: **optional extra only**.
3. **pyserial-compatible constants.** Offer a thin compatibility shim exposing `pyserial`-style constants (`PARITY_NONE`, `EIGHTBITS`, etc.) to ease migration, or keep the surface minimal? Lean: **minimal surface**; migration guide covers it.
4. **RS-485 manual RTS fallback.** Provide an explicit opt-in manual-toggle fallback for drivers without `TIOCSRS485`, or leave it entirely to user code? Lean: **leave to user code** for now; revisit if demand warrants.
5. **Trio-on-asyncio support.** AnyIO allows running Trio APIs on an asyncio loop and vice versa. Commit to both native backends only, or support the cross-runtime modes? Lean: **native only** — simpler promise, better-tested.

---

## 36. Risk Register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Platform-specific termios edge cases | Medium | High | socat pty tests in CI; hardware tests pre-release; capability model surfaces limitations |
| AnyIO API changes | Low | Medium | Pin compatible range; CI integration test against latest |
| Windows backend harder than expected | Medium | Medium | Two-Protocol split accommodates overlapped-I/O design natively (AsyncSerialBackend); deferred to M10; design-review gate; worst-case pySerial fallback |
| Benchmark regressions go unnoticed | Medium | Medium | Nightly bench job with 10% regression gate |
| `ctypes` IOKit bindings fragile on macOS upgrades | Medium | Low | Localized `# type: ignore` with linked issues; pySerial discovery extra as fallback |
| Subtle differences across BSD variants | Medium | Medium | Hardware-test gate before claiming first-class BSD support; documented as best-effort otherwise |
| USB-serial disconnect semantics vary by kernel | High | Low | `SerialDisconnectedError` detected from multiple signals (EIO, repeated zero-read after readiness) |
| FTDI latency-timer sysfs path changes | Low | Low | Feature-detect path; fall back to `UnsupportedFeatureError` per policy |

---

## 37. Appendices

### Appendix A — Why not pySerial?
pySerial is sync-first, Windows-centric, and its async story (`pyserial-asyncio`) wraps the sync API in a threadpool, adding per-call overhead. `anyserial` is async-native with a thin sync wrapper, inverting the layering. We still treat pySerial as a valuable behavior reference and offer its `list_ports` utility as an optional discovery backend.

### Appendix B — Why not `trio_pyserial`?
Excellent library, but Trio-only. `anyserial` supports both asyncio and Trio via AnyIO, with native `anyio.wait_readable(fd)` in the hot path — avoiding Trio-exclusivity and any socket-wrapper workaround.

### Appendix C — Why composition over inheritance?
A deep inheritance chain (e.g. `ByteStream → AbstractSerialStream → PosixSerialStream → LinuxSerialStream`) makes it hard to determine which class defines `_configure_port` and impossible to test the logic layer without a real fd. A `SerialPort` class holding a `Backend` Protocol instead gives:

- One class per concern.
- Each backend unit-testable in isolation.
- New platform backends require implementing one Protocol, not extending a class hierarchy.
- Testing without hardware via `MockBackend`.

### Appendix D — Why raw bytes only?
Framing is always opinionated. Line-delimited, length-prefixed, Modbus RTU inter-frame timing, SLIP, COBS — each has its own semantics and failure modes. Keeping the core at the byte-stream layer means it composes with whatever framing the user needs. AnyIO's `BufferedByteReceiveStream` handles the common line/delimiter case. A sibling package (`anyserial-modbus`, `anyserial-frame`) can build on top if demand emerges.

### Appendix E — Why no internal readahead buffer?
An internal buffer surprises cancellation semantics (bytes already consumed from the kernel but not delivered to the user), introduces a second place to ask "where are my bytes," and violates "one way to do each thing." AnyIO's `BufferedByteReceiveStream` provides composable buffering when users want it. Measurements inform whether internal scratch buffers help specific operations (e.g., `send`) without being user-observable.

### Appendix F — Why `anyio.wait_readable` instead of a sniffio dispatch layer?
AnyIO 4.7+ ships native fd-generalized readiness APIs (`wait_readable`, `wait_writable`) that accept any object with a `.fileno()` method or an integer fd. Older code that predated them used `anyio.wait_socket_readable`, which required wrapping the tty fd in a socket — a major source of overhead. Using the native API eliminates the workaround and any backend-dispatching code.

As of AnyIO 4.12, `sniffio` is no longer even an AnyIO dependency — the unified public API is fully backend-neutral. `anyserial` does not import `sniffio`, branch on backends, or inspect the running event loop.

### Appendix G — Why the backend Protocol is sync on POSIX
The canonical async-I/O pattern in Python — used by Trio's own `SocketStream`, curio, and AnyIO's internal backend implementations — separates two concerns:

1. A sync layer that owns the OS primitives (open fd with `O_NONBLOCK`, call `os.read` / `os.write` that return immediately with bytes or raise `BlockingIOError`).
2. An async layer that waits for readiness (`await anyio.wait_readable(fd)`) and then invokes the sync primitive.

Our earlier design put both concerns in the backend (every backend method was `async def`). That meant:
- Every POSIX backend had to `import anyio`, even though it was only wrapping `os.read`.
- `MockBackend` had to be written with async methods, making pure unit tests awkward.
- `async def close()` was fake — it had nothing to await, because `os.close` is synchronous.
- The readiness loop would have to be reimplemented in every backend (or pushed into a shared async base class, reintroducing the inheritance we deliberately rejected in Appendix C).

Splitting the Protocol into `SyncSerialBackend` (POSIX) and `AsyncSerialBackend` (Windows / non-fd platforms) fixes all of these. The POSIX backends become pure OS mechanics with zero AnyIO coupling; the async orchestration lives in one place (`SerialPort`). Windows, which genuinely has no fd-readiness model, implements `AsyncSerialBackend` natively via overlapped I/O and doesn't have to pretend to fit a sync-primitives contract.

This is not theory: the same layering powers every mature Python async-I/O library. We're adopting the well-established pattern, not inventing one.

### Appendix H — Why `anyio.notify_closing` is mandatory
If `aclose()` calls `os.close(fd)` while another task is parked inside `await anyio.wait_readable(fd)`, the outcomes vary by backend:
- Trio may hang or raise a confusing error when the fd vanishes out from under it.
- asyncio `ProactorEventLoop` (Windows default) raises an `OSError` from the selector that the user can't meaningfully act on.
- asyncio `SelectorEventLoop` (Linux/macOS default) may raise an `OSError` or silently lose the waiter depending on the selector.

`anyio.notify_closing(fd)` (added 4.10) wakes every task parked on that fd with `ClosedResourceError` in a backend-uniform way. It is the only correct close pattern for any resource using `wait_readable` / `wait_writable`.

### Appendix I — Final Decisions (quick reference)

One-page summary of every locked decision in this design:

**Architecture**
- AnyIO-first; AnyIO is the only required runtime dependency.
- Python 3.13+ only; tested forward against 3.14 and 3.15-dev.
- Linux is first-class; macOS and BSD are fully supported POSIX; Windows is deferred behind a backend boundary.
- Composition over inheritance; `SerialPort` holds a backend that implements one of two Protocols.
- **Two Backend Protocols**: `SyncSerialBackend` (POSIX, OS primitives, zero AnyIO coupling) and `AsyncSerialBackend` (Windows / non-fd platforms). `SerialPort` dispatches on Protocol at open time.
- POSIX backends expose `fileno()` + sync `read_nonblocking` / `write_nonblocking`; async readiness loop lives in `SerialPort`.
- `MockBackend` is a sync `SyncSerialBackend` implementation — first-class test fixture from M1.

**Public API**
- Primary class is `SerialPort` (async); sync wrapper lives in `anyserial.sync.SerialPort`, deferred to M7.
- `SerialPort` implements `anyio.abc.ByteStream`.
- `SerialConnectable` implements `anyio.abc.ByteStreamConnectable` for deferred connection.
- Backend details exposed via AnyIO typed attributes (`extra_attributes`), not ad-hoc public properties.
- `send(bytes)` matches `ByteSendStream` exactly; `send_buffer(BytesLike)` is the zero-copy variant.
- `send_eof()` drains and is idempotent; does not close.
- Discovery is async: `list_serial_ports()`, `find_serial_port()`.
- No `receive_until` / `receive_exactly` in core — compose with `anyio.streams.buffered.BufferedByteStream`.

**Types and configuration**
- `StrEnum` for all user-facing enums.
- `SerialConfig` is frozen, slotted, kw-only.
- `FlowControl` is a dataclass with independent booleans, not an enum.
- `BytesLike = collections.abc.Buffer` (PEP 688).
- `Capability` is a tri-state `StrEnum` (`SUPPORTED` / `UNSUPPORTED` / `UNKNOWN`), never a bool.
- `UnsupportedPolicy` default: `RAISE`.

**AnyIO canonical patterns**
- `await anyio.wait_readable(fd)` / `wait_writable(fd)` (never socket variants).
- `anyio.notify_closing(fd)` before `os.close(fd)` in every `aclose()`.
- `anyio.ResourceGuard` per direction (one for receive, one for send).
- `anyio.Lock()` for configure and close sequencing (plain `Lock`; `fast_acquire` only if benchmarks justify).
- `with anyio.fail_after(t):` for timeouts — no per-call timeout parameters on the async API.
- Shielded cancel scope around the close critical section.
- No internal `TaskGroup` — caller owns concurrency.
- No explicit `anyio.lowlevel.checkpoint()` inside I/O loops that already await readiness.
- `anyio.from_thread.BlockingPortalProvider` for the sync wrapper.

**Exceptions**
- Multi-inherit from the most natural stdlib class and from AnyIO's canonical exception where applicable.
- Never return `b""` from `receive()`; raise `EndOfStream` / `BrokenResourceError`.
- Preserve OS errors via `raise ... from exc`.

**Performance**
- Default is unbuffered; AnyIO `BufferedByteStream` for user-side buffering.
- `receive_into` for allocation-sensitive paths.
- `receive_available` drains `TIOCINQ`/`FIONREAD` per wakeup.
- Low-latency tuning with restore-on-close.
- uvloop / winloop documented and opt-in via `backend_options={"use_uvloop": True}`; never installed by the library.
- Rejected: io_uring, C extension, custom event loop, hidden readahead, `sniffio` dispatch, socket wrappers around fds.

**Testing**
- AnyIO's built-in pytest plugin, parametrized `anyio_backend` fixture across asyncio / asyncio+uvloop / trio.
- Do not depend on a separate `pytest-anyio` package.
- Unit tests use `MockBackend`; integration tests use socat pty pairs; hardware tests are opt-in and marker-gated.
- `mypy --strict` + `pyright` in CI.

**Dependencies**
- Runtime: `anyio>=4.13`. No `sniffio`. No `pySerial` in core.
- Optional extras: `uvloop`, `winloop`, `trio`, `discovery-pyudev`, `discovery-pyserial`.

**Versioning**
- First release is `v0.1.0`. Version from git tags via `hatch-vcs`. PyPI publish via OIDC trusted publishing.

### Appendix J — References

AnyIO:
- Documentation — https://anyio.readthedocs.io/
- API reference — https://anyio.readthedocs.io/en/stable/api.html
- Backend options / `use_uvloop` — https://anyio.readthedocs.io/en/stable/basics.html
- Cancellation & timeouts — https://anyio.readthedocs.io/en/stable/cancellation.html
- Streams — https://anyio.readthedocs.io/en/stable/streams.html
- Threads / `BlockingPortal` — https://anyio.readthedocs.io/en/stable/threads.html
- Version history — https://github.com/agronholm/anyio/blob/master/docs/versionhistory.rst

Event loops:
- uvloop — https://uvloop.readthedocs.io/
- winloop — https://github.com/Vizonex/Winloop

Serial references:
- pySerial API — https://pyserial.readthedocs.io/
- Linux `setserial` low-latency — https://www.mankier.com/8/setserial
- Linux `termios(3)` — https://man7.org/linux/man-pages/man3/termios.3.html
- Linux RS-485 ioctl — https://www.kernel.org/doc/html/latest/driver-api/serial/serial-rs485.html
- Darwin `IOSSIOSPEED` — IOKit SerialFamily headers
