# Design review — Windows backend

> **Status:** Implemented. This document captures the design decisions
> behind the shipped `_windows/` backend (DESIGN.md §24.5).
>
> **Scope:** First-class Windows serial support. Implements the
> `AsyncSerialBackend` Protocol (DESIGN.md §25.2).

## 1. Decision

**Runtime-native IOCP dispatch.** `anyserial` uses each async runtime's
own overlapped-I/O machinery rather than running its own completion
worker:

- **Trio** → `trio.lowlevel.register_with_iocp` + `readinto_overlapped` /
  `write_overlapped` / `wait_overlapped`.
- **asyncio on `ProactorEventLoop`** → `loop._proactor._register` +
  `_overlapped.Overlapped.ReadFile` / `WriteFile`.

There is **no worker-thread fallback**. If the user is on
`SelectorEventLoop` (Windows-only forced selection) we raise a clear
error at open time telling them to use `WindowsProactorEventLoopPolicy`
(the default on Python ≥ 3.8).

uvloop on Windows is not a case: uvloop does not build or run there.
`winloop` (a uvloop fork for Windows) is untested and unsupported; it
exposes a proactor-like surface but its IOCP integration has not been
verified against our overlapped paths. Do not claim support without a
dedicated integration effort and CI coverage.

### Why no fallback

Two paths covers every real Windows runtime. A third (worker-thread
completion worker) would double maintenance cost to support the narrow
case of "user explicitly opted into `SelectorEventLoop` on Windows." We
prefer one clear error message to three code paths.

### Rejected alternatives

See the research notes that accompanied this design review for the full
comparison. Short form:

| Option | Why rejected |
|---|---|
| Worker thread + private IOCP | Extra thread per library; worse cancellation latency; 600+ LoC we don't need. |
| Thread-per-port blocking `ReadFile` | Thread-per-port scaling ceiling; timeout-bounded cancellation. |
| Wrap pyserial via `anyio.to_thread` | Contradicts Appendix A; inherits pyserial's `GetOverlappedResult(INFINITE)` Ctrl-C bug; no clean cancellation story. |
| Rust / C extension | Changes build/distribution story; not a pure-Python library anymore. |

## 2. Runtime detection

AnyIO 4.12 dropped `sniffio` as a direct dependency (anyio/#1021). We do
**not** re-add it. Detection uses runtime-native probes:

```python
# anyserial/_windows/_runtime.py
def detect_runtime() -> Literal["asyncio", "trio"]:
    try:
        import asyncio
        asyncio.get_running_loop()
        return "asyncio"
    except RuntimeError:
        pass
    try:
        import trio
        trio.lowlevel.current_task()
        return "trio"
    except (ImportError, RuntimeError):
        pass
    msg = "anyserial: no running asyncio or trio runtime detected"
    raise RuntimeError(msg)
```

Called once inside `WindowsBackend.open()`. No hot-path cost.

### Proactor loop check

```python
loop = asyncio.get_running_loop()
proactor = getattr(loop, "_proactor", None)
if proactor is None:
    raise UnsupportedPlatformError(
        "anyserial requires asyncio.ProactorEventLoop on Windows. "
        "This is the default since Python 3.8. If you have overridden "
        "the event loop policy, use WindowsProactorEventLoopPolicy."
    )
```

## 3. Architecture

```
anyserial/_windows/
├── __init__.py         # re-exports WindowsBackend factory
├── _win32.py           # ctypes bindings (DCB, COMMTIMEOUTS, OVERLAPPED, ...)
├── _runtime.py         # detect_runtime()
├── backend.py          # WindowsBackend (AsyncSerialBackend impl, dispatch shim)
├── _trio_io.py         # Trio read/write/wait paths
├── _asyncio_io.py      # asyncio Proactor read/write/wait paths
├── capabilities.py     # windows_capabilities() snapshot
├── discovery.py        # SetupAPI-based PortInfo resolution
├── baudrate.py         # DCB.BaudRate translation helpers (trivial on Win32)
└── dcb.py              # SerialConfig <-> DCB / COMMTIMEOUTS translation
```

`WindowsBackend` is a single class implementing `AsyncSerialBackend`.
The class holds the detected runtime flavour and delegates hot-path
`receive` / `send` to one of two module-level functions:

```python
class _HandleWrapper:
    """Wraps a raw Win32 HANDLE for CPython's proactor.

    ``IocpProactor._register_with_iocp`` calls ``obj.fileno()`` and uses
    ``obj`` as a dict key in its internal cache.  A raw ``int`` has no
    ``.fileno()`` and could alias Python's small-int cache, so we wrap it.
    Trio's ``register_with_iocp`` accepts a raw int — the wrapper is only
    needed on the asyncio path.
    """
    __slots__ = ("_handle",)
    def __init__(self, handle: int) -> None:
        self._handle = handle
    def fileno(self) -> int:
        return self._handle

class WindowsBackend:
    async def open(self, path, config):
        self._runtime = detect_runtime()
        self._handle = _open_comm_handle(path, config)
        _apply_dcb(self._handle, config)
        _apply_timeouts(self._handle)
        if self._runtime == "trio":
            trio.lowlevel.register_with_iocp(self._handle)
        else:
            self._handle_wrapper = _HandleWrapper(self._handle)
            proactor = asyncio.get_running_loop()._proactor
            proactor._register_with_iocp(self._handle_wrapper)

    async def receive_into(self, buffer):
        if self._runtime == "trio":
            return await _trio_io.readinto(self._handle, buffer)
        return await _asyncio_io.readinto(
            self._handle, self._handle_wrapper, buffer
        )
```

**Handle wrapper (asyncio only).** CPython's
`IocpProactor._register_with_iocp(obj)` calls `obj.fileno()` to obtain
the OS handle and stores `obj` as a dict key in its completion cache.
A raw `int` has no `.fileno()` method and would raise `AttributeError`
at registration time. The `_HandleWrapper` shim satisfies both
requirements. Trio's `register_with_iocp` accepts a raw int directly,
so the wrapper is not used on that path.

Trio and asyncio code paths live in separate modules so the one we
don't need for a given process is never imported.

## 4. Hot path

### Trio path (`_trio_io.py`)

Use Trio's batteries-included helpers:

```python
import trio

async def readinto(handle: int, buffer: bytearray | memoryview) -> int:
    # register_with_iocp called once in open(); idempotent-check not needed
    # because we track it on WindowsBackend.
    return await trio.lowlevel.readinto_overlapped(handle, buffer)

async def write(handle: int, data: memoryview) -> int:
    return await trio.lowlevel.write_overlapped(handle, data)
```

Trio owns the OVERLAPPED lifecycle, handles `CancelIoEx`, and waits for
actual completion before returning on cancellation. Buffer is caller-
owned; we must keep it alive across the `await`, which the calling
`SerialPort` hot path already guarantees.

Minimum version: **`trio >= 0.22`**.

### Asyncio path (`_asyncio_io.py`)

```python
import _overlapped                         # CPython private but ABI-stable since 3.4
from asyncio import get_running_loop

_NULL = 0

async def register(handle_wrapper: _HandleWrapper) -> None:
    """Associate the wrapped handle with the proactor's completion port."""
    proactor = get_running_loop()._proactor
    proactor._register_with_iocp(handle_wrapper)

async def readinto(
    handle: int, handle_wrapper: _HandleWrapper,
    buffer: bytearray | memoryview,
) -> int:
    # Zero-copy: ReadFileInto writes directly into the caller buffer.
    # Requires CPython >= 3.12; anyserial requires >= 3.13 so always present.
    #
    # handle_wrapper is passed to proactor._register so the proactor's
    # internal cache stays keyed on the same object used at registration.
    # The raw int handle is passed to ReadFileInto (kernel API).
    loop = get_running_loop()
    proactor = loop._proactor
    ov = _overlapped.Overlapped(_NULL)
    ov.ReadFileInto(handle, buffer)
    return await proactor._register(ov, handle_wrapper,
                                    lambda trans, key, ov: ov.getresult())

async def write(
    handle: int, handle_wrapper: _HandleWrapper, data: memoryview,
) -> int:
    loop = get_running_loop()
    proactor = loop._proactor
    ov = _overlapped.Overlapped(_NULL)
    ov.WriteFile(handle, data)
    return await proactor._register(ov, handle_wrapper,
                                    lambda trans, key, ov: ov.getresult())
```

**Why `handle_wrapper` in `_register` but raw `handle` in
`ReadFileInto` / `WriteFile`:** The proactor's `_register(ov, obj, cb)`
stores `obj` in an internal dict keyed on the same object given to
`_register_with_iocp`. Passing the raw int would miss the cache entry
and could produce orphaned futures. The kernel APIs (`ReadFileInto`,
`WriteFile`) require the raw integer HANDLE, not a Python wrapper.

The buffer must remain alive and unmutated until the future resolves
or the cancellation completion packet arrives — both conditions are
guaranteed by the `await` staying in scope across the operation.

Cancellation is automatic: if the awaiting task is cancelled, the
returned future is cancelled, which calls `ov.cancel()` → `CancelIoEx`.
The proactor waits for the actual completion packet before releasing
the buffer, so there is no post-cancel use-after-free.

### 4.1 Why we deliberately accept a private-API dep

`loop._proactor` and `_overlapped.Overlapped` are underscore-prefixed.
Their surface has been stable since Python 3.4; CPython
[discuss.python.org #102183](https://discuss.python.org/t/public-proactor-api/102183)
is tracking promotion. We accept the breakage risk because:

- The alternative is writing and maintaining our own IOCP completion
  worker, which is the exact thing CPython already does — badly
  duplicated.
- Breakage is detectable by CI: any signature change surfaces as an
  import-time or first-call error, not silent corruption.
- The asyncio path is pinned to specific Python minor versions in CI.

### 4.2 Zero-copy on both paths

Both runtimes land bytes directly in the caller buffer:

- **Trio:** `readinto_overlapped(handle, buffer)` is already zero-copy.
- **asyncio:** `_overlapped.Overlapped.ReadFileInto(handle, buffer)`
  (CPython 3.12+) writes through the buffer protocol directly to the
  caller's `bytearray` / `memoryview`. `anyserial` requires Python
  3.13, so the older `ReadFile` + copy path is never needed and is
  not implemented.

This closes the only real per-op overhead gap vs. a native extension
on any realistic serial workload. The only remaining allocation on
the asyncio hot path is the `_overlapped.Overlapped()` Python object
itself (small-object allocator; ~100 ns), which we accept as the cost
of reusing CPython's completion-cache machinery.

## 5. Cancellation protocol

Every overlapped op follows this contract:

```
issue overlapped op         →  ReadFile / WriteFile / WaitCommEvent
await completion             →  runtime handles it
on cancel:
    runtime calls CancelIoEx →  kernel begins teardown
    runtime awaits completion → buffer released safely
    task sees Cancelled
```

We never call `CancelIoEx` ourselves from user-facing code. Both
runtimes handle it, and calling it ourselves risks double-cancellation
races.

`aclose()` sequence (shielded, idempotent):

```
SetCommMask(handle, 0)        # wake any pending WaitCommEvent cleanly
PurgeComm(PURGE_RX|TX|ABORT)  # cancel in-flight I/O
CloseHandle(handle)           # final teardown
```

Trio's `register_with_iocp` has no deregister call — the HANDLE is
dissociated when closed.

## 6. Win32 surface

All Win32 bindings live in `_win32.py`. We write them fresh rather
than depending on pyserial's `serial/win32.py` (correctness:
`use_last_error=True` and proper `errcheck` hooks throughout; scope:
only what we actually use).

### 6.1 Control plane

| API | Purpose |
|---|---|
| `CreateFileW` | Open `\\.\COMn` with `FILE_FLAG_OVERLAPPED`, `dwShareMode=0`. |
| `CloseHandle` | Final teardown. |
| `GetCommState` / `SetCommState` | DCB round-trip for configuration. Prefer `GetCommState` → modify known fields → `SetCommState` over building a zeroed DCB from scratch (see §6.2.1). |
| `SetCommTimeouts` | Configure "wait-for-any" read timeouts (§6.3). |
| `SetupComm` | Request explicit 4 KiB input / 4 KiB output queue sizing. |
| `PurgeComm` | Drop queued bytes and abort in-flight ops on close. |
| `EscapeCommFunction` | RTS/DTR/BREAK control. |
| `GetCommModemStatus` | Synchronous CTS/DSR/RI/CD snapshot. |
| `ClearCommError` | `COMSTAT.cbInQue` / `cbOutQue` for `input_waiting` / `output_waiting`. |
| `CancelIoEx` | Internal only; runtimes call it, not us. |

### 6.2 DCB translation (`dcb.py`)

`SerialConfig` → `DCB`:

| `SerialConfig` | DCB field(s) |
|---|---|
| `baudrate` | `BaudRate` (raw integer — Windows has no B-constant table). |
| `bytesize` | `ByteSize` (5–8). |
| `parity` | `Parity` (`NOPARITY`/`ODDPARITY`/`EVENPARITY`/`MARKPARITY`/`SPACEPARITY`), `fParity=1` if enabled. |
| `stopbits` | `StopBits` (`ONESTOPBIT`/`ONE5STOPBITS`/`TWOSTOPBITS`). |
| `flow_control=XONXOFF` | `fOutX=1`, `fInX=1`, `XonChar=0x11`, `XoffChar=0x13`, `XonLim`/`XoffLim` default. |
| `flow_control=RTSCTS` | `fOutxCtsFlow=1`, `fRtsControl=RTS_CONTROL_HANDSHAKE`. |
| `flow_control=DSRDTR` | `fOutxDsrFlow=1`, `fDtrControl=DTR_CONTROL_HANDSHAKE`. |
| `flow_control=NONE` | All flow flags 0; `fDtrControl`/`fRtsControl` set from requested RTS/DTR initial state. |

Invariants for every DCB we ship:

- `DCBlength = sizeof(DCB)` (28 bytes).
- `fBinary = 1` (mandatory — Windows documents non-binary mode as
  unsupported).
- `fAbortOnError = 0` (matches pyserial; flipping forces
  `ClearCommError` after every error, which is a footgun).
- `fNull = 0`, `fErrorChar = 0`, `fParity = 0` unless parity selected.

#### 6.2.1 DCB construction strategy

> **Revision (2026-04-15):** The initial M10.2 implementation builds a
> zeroed `DCB` from scratch and sets every field deterministically. This
> ensures we own every byte and avoids inheriting driver garbage from
> a stale `GetCommState` snapshot. However, some USB-serial drivers
> (FTDI, Prolific, CH340) store vendor-specific hints in reserved or
> padding bytes of the DCB. Zeroing those fields can cause subtle
> misbehavior on exotic hardware.

**Revised strategy:** Use `GetCommState` → modify known fields →
`SetCommState`. This preserves any driver-specific state we don't
understand while still setting every field we *do* control
deterministically:

```python
def build_dcb(handle: int, config: SerialConfig) -> DCB:
    """Read current DCB, overlay our config, return the result."""
    dcb = DCB()
    dcb.DCBlength = sizeof(DCB)
    kernel32.GetCommState(handle, byref(dcb))
    # Overwrite every field we own — same invariants as before.
    dcb.BaudRate = config.baudrate
    dcb.ByteSize = ...
    dcb.fBinary = 1
    dcb.fAbortOnError = 0
    # ... remainder unchanged from §6.2 ...
```

The `build_dcb` signature changes to accept `handle` (needed for the
`GetCommState` call). The full list of fields we set and their values
remains identical to the table above — we still own every documented
field deterministically. The difference is that reserved/padding bytes
we *don't* touch retain whatever the driver put there.

**Trade-off acknowledged:** if `GetCommState` returns garbage on a
freshly-opened port (before any `SetCommState`), we inherit that
garbage in fields we don't overwrite. In practice this is safe because
(a) the fields we overwrite cover every documented DCB field, and
(b) drivers that put meaningful data in reserved fields are precisely
the ones that break when those fields are zeroed.

If M10.2 integration testing on virtual COM and real hardware (FTDI
FT232R, Prolific PL2303, CH340G) shows no issues with the zeroed-DCB
path, we can defer this change — but the `GetCommState` round-trip
should be the default before final release.

### 6.3 COMMTIMEOUTS

> **Correction (2026-04-15):** The original design specified all-zero
> timeouts with the rationale "reads return bytes as they arrive." This
> is wrong. The Microsoft documentation for `COMMTIMEOUTS` states:
> *"If all five DWORD values are zero, timeouts are not used, and the
> ReadFile function does not return until the number of bytes requested
> have been read."* With overlapped I/O this means the completion packet
> is not posted until the **entire** buffer is filled — directly
> violating `ByteStream.receive(max_bytes)` semantics, which must return
> as soon as **any** bytes are available. The corrected policy below
> uses the documented "wait-for-any" mode.

```
ReadIntervalTimeout         = MAXDWORD (0xFFFFFFFF)
ReadTotalTimeoutMultiplier  = MAXDWORD (0xFFFFFFFF)
ReadTotalTimeoutConstant    = 1        # ms — wait up to 1 ms for first byte
WriteTotalTimeoutMultiplier = 0
WriteTotalTimeoutConstant   = 0
```

The `MAXDWORD / MAXDWORD / positive-constant` triple is a
[documented special case](https://learn.microsoft.com/en-us/windows/win32/api/winbase/ns-winbase-commtimeouts):
the read waits up to `ReadTotalTimeoutConstant` milliseconds for the
**first byte**, then returns immediately with whatever bytes are
available. This matches `ByteStream.receive(max_bytes)`: return when
any bytes are available, not when the whole requested buffer has filled.

The 1 ms constant is a floor, not a ceiling — it does not limit how
long the overlapped read can pend. When no data has arrived, the
overlapped op completes after 1 ms with zero bytes transferred; we
reissue the read internally (the caller never sees the empty
completion). In practice, serial data arrives at wire speed so the
read almost always completes with bytes on the first try. AnyIO
cancellation scopes remain the authoritative timeout mechanism for
user-facing operations.

Write timeouts remain zero: writes complete when the kernel has
accepted the bytes into the output queue, and `drain()` uses
`FlushFileBuffers` for explicit completion waiting.

**Tuning note:** if integration testing shows that certain USB-serial
drivers (FTDI, Prolific, CH340) interact poorly with 1 ms — e.g.,
returning spurious zero-byte completions at high frequency when idle —
the constant can be raised to 10 ms with no API-contract impact.
Document tested values per driver in `docs/windows.md`.

### 6.4 Modem-line change notification

`WaitCommEvent` is lower priority than the data path; not in the M10.2
initial scope. It is **not** used as a readiness layer before
`ReadFile` — direct overlapped reads with the "wait-for-any" timeout
policy (§6.3) are simpler, lower-overhead, and avoid the race between
a readiness notification and the subsequent read. When we add
`WaitCommEvent` (M10.3), it is exclusively for modem-line / error
notifications:

- **Event mask:** `EV_CTS | EV_DSR | EV_RING | EV_RLSD | EV_ERR |
  EV_BREAK`. `EV_ERR` covers framing/overrun/parity errors that may
  not surface through the data path. `EV_BREAK` surfaces break
  conditions for callers monitoring line state. `EV_RXCHAR` is
  deliberately excluded — we do not use comm events for data-path
  readiness.
- **Trio:** raw `wait_overlapped` with a ctypes OVERLAPPED + DWORD mask.
- **asyncio:** `_overlapped.Overlapped` has no `WaitCommEvent` method,
  so we drive it with a manual-reset event + `proactor.wait_for_handle`.
- **Shutdown:** `SetCommMask(handle, 0)` (races cleanly; preferred over
  `CancelIoEx` for this op specifically).
- **Fallback role:** if real driver testing (M10.4) shows that certain
  drivers produce unreliable zero-byte completions under the
  "wait-for-any" timeout mode, `WaitCommEvent(EV_RXCHAR)` can be
  re-evaluated as a readiness gate for the data path. This is a
  contingency, not the default design.

## 7. Capabilities matrix

| Capability | Windows status | Notes |
|---|---|---|
| `standard_baud` | SUPPORTED | `DCB.BaudRate` is an integer. |
| `custom_baud` | SUPPORTED | Same mechanism; driver decides what it accepts. |
| `rtscts` / `xonxoff` / `dsrdtr` | SUPPORTED | DCB flags. |
| `break_condition` | SUPPORTED | `SetCommBreak`/`ClearCommBreak`. |
| `modem_lines_input` | SUPPORTED | `GetCommModemStatus`. |
| `modem_lines_output` | SUPPORTED | `EscapeCommFunction`. |
| `flush_input` / `flush_output` | SUPPORTED | `PurgeComm`. |
| `exclusive` | SUPPORTED-BY-DEFAULT | We always open with `dwShareMode=0`. No way to *disable* on Windows. |
| `low_latency` | UNSUPPORTED | Windows has no `ASYNC_LOW_LATENCY`; FTDI's latency timer is a driver-GUI setting. Documented. |
| `rs485` | UNSUPPORTED | FTDI VCP RS-485 mode is a driver config, not a runtime API. Revisit later. |
| `input_waiting` / `output_waiting` | SUPPORTED | `ClearCommError` → `COMSTAT`. |
| `drain_exact` | SUPPORTED | Write with `FlushFileBuffers` (write timeouts remain zero; §6.3). |

## 8. Port discovery

Native discovery via SetupAPI with
`GUID_DEVINTERFACE_COMPORT = {86E0D1E0-8089-11D0-9CE4-08003E301F73}`:

1. `SetupDiGetClassDevsW(&GUID, NULL, NULL, DIGCF_PRESENT|DIGCF_DEVICEINTERFACE)`.
2. Enumerate with `SetupDiEnumDeviceInterfaces`.
3. `SetupDiGetDeviceInterfaceDetailW` for the device path.
4. `SetupDiGetDeviceRegistryPropertyW` for `FRIENDLYNAME`, `HARDWAREID`,
   `LOCATION_INFORMATION`.
5. Parse `USB\\VID_xxxx&PID_xxxx\\...` from hardware ID strings for
   `vid` / `pid` / `serial_number`.

On x64 `SP_DEVICE_INTERFACE_DETAIL_DATA_W.cbSize` must be `8`; on x86
it must be `6`. Guard this with `sizeof(c_void_p)`.

Fallback: `HKLM\\HARDWARE\\DEVICEMAP\\SERIALCOMM` via `winreg` gives
device name but no metadata. Use only if SetupAPI enumeration fails.

No pyserial dependency. `anyserial[discovery-pyserial]` remains the
optional extra for users who explicitly want it.

## 9. Error translation

Existing `errno_to_exception` is POSIX-shaped. Windows needs a
companion `winerror_to_exception(err, *, context, path)` mapping:

| Win32 code | Exception |
|---|---|
| `ERROR_FILE_NOT_FOUND` (2) | `SerialNotFoundError` |
| `ERROR_ACCESS_DENIED` (5) | `SerialPermissionError` |
| `ERROR_SHARING_VIOLATION` (32) | `SerialInUseError` |
| `ERROR_INVALID_HANDLE` (6) | `SerialClosedError` |
| `ERROR_INVALID_PARAMETER` (87) on `SetCommState` | `SerialConfigurationError` |
| `ERROR_OPERATION_ABORTED` (995) | swallow (cancellation path only). |
| `ERROR_DEVICE_REMOVED` (1617) / `ERROR_GEN_FAILURE` on USB unplug | `SerialDisconnectedError` |
| `ERROR_NOT_READY` (21) | `SerialDisconnectedError` |

ctypes returns raw DWORDs; wrap `WinError` construction with our
translator.

## 10. Testing strategy

### 10.1 Unit tests

- `_win32.py` structs: sizeof / offset assertions per architecture.
- `dcb.py` translation: every `SerialConfig` combination round-trips
  through DCB encode/decode.
- `_runtime.py` detection: mocked asyncio / trio contexts.
- Capability snapshot invariants.

### 10.2 Integration tests

Virtual or hardware serial pair via an opt-in **self-hosted Windows
runner**:

- GitHub-hosted `windows-latest` runs hermetic Windows unit / property /
  typing coverage only. It does not install com0com or other virtual COM
  kernel drivers; modern Windows driver-signing policy makes that path
  fragile and image-dependent.
- Provision the runner once with com0com, a commercial virtual-COM
  driver, or real serial hardware. Label it `anyserial-windows-serial`.
- Set repository variable `ANYSERIAL_RUN_SELF_HOSTED_WINDOWS=true` to
  enable the jobs. Set `ANYSERIAL_WINDOWS_PAIR=COMA,COMB` if the pair is
  not the default `COM50,COM51`.
- Validate the configured pair from
  `HKLM:\HARDWARE\DEVICEMAP\SERIALCOMM` before running integration tests.
- Mark tests `@pytest.mark.windows_virtual_serial` and gate them on
  `ANYSERIAL_WINDOWS_PAIR`.
- Expect virtual-driver loopback latency, often around the millisecond
  scale — code timing assertions accordingly.

### 10.3 Runtime matrix

Every integration test runs on both Trio and asyncio (Proactor). One
test explicitly forces `SelectorEventLoop` and asserts we raise
`UnsupportedPlatformError` with the expected message.

### 10.4 Property / stress

- `hypothesis` fuzz of DCB encode/decode parity.
- 1 MB round-trip at 921600 baud across the configured serial pair (flow-control
  matrix).
- Cancellation-mid-read test: parked `receive`, cancel scope, verify
  clean teardown, no `ResourceWarning`.

### 10.5 CI wiring

Windows jobs in `ci.yml`:

- `windows-serial`: hosted `windows-latest`, Python 3.13 / 3.14,
  hermetic unit / property / typing checks only. Runs on every push / PR.
- `windows-serial-self-hosted`: opt-in self-hosted runner labelled
  `anyserial-windows-serial`, Python 3.13 / 3.14, validates
  `ANYSERIAL_WINDOWS_PAIR`, then runs
  `pytest tests/integration/test_windows_backend.py -q -v`. Gated to
  **release prep only**: triggers on `refs/tags/v*` pushes and manual
  `workflow_dispatch`. Skipped on push-to-main and PRs so the runner
  doesn't have to be online 24/7.
- `windows-smoke`: import-only fallback remains useful when driver /
  hardware integration is unavailable.

`bench.yml`:

- `bench-windows`: opt-in self-hosted runner, Python 3.13. Gated to
  `workflow_dispatch` only — re-baseline Windows benchmarks explicitly
  at release time; the nightly schedule stays on hosted Linux.

### 10.6 Future: ephemeral cloud runner (planning note)

The self-hosted runner works but carries real overhead: a persistent
Windows machine to maintain, keep patched, and keep online for release
runs. The trigger gates in §10.5 bound how often it has to be awake,
but they don't remove the "own a Windows box" problem.

**Target migration**: replace the persistent self-hosted runner with an
**ephemeral EC2 Windows instance** spun up per job via
[`machulav/ec2-github-runner`](https://github.com/machulav/ec2-github-runner)
(or the Azure equivalent). A pre-baked AMI carries the signed com0com
install, Python, uv, and the GHA runner agent configured to register +
deregister on each job.

**Cost model** (t3.medium Windows, us-east-1, on-demand):

| Item | Cost |
|---|---|
| Compute per run (~15 min incl. boot/register/teardown) | ~$0.02 |
| Egress per run (~100 MB) | ~$0.01 |
| AMI snapshot storage (~30 GB) | ~$1.50/mo |

Realistic totals at release cadence (~10 runs/mo): **~$1.80/mo** all-in.
Compare to an always-on t3.medium Windows EC2 at ~$60/mo or the
maintenance cost of a physical box on the desk.

**Migration checklist** (when we pull the trigger):

1. Launch a fresh Windows Server 2022 instance; install the
   signed com0com package (same steps currently documented in
   [`docs/windows.md`](windows.md) "Self-hosted serial CI setup").
   Verify `COM50`/`COM51` surface in `SERIALCOMM`.
2. Install Python 3.13 / 3.14 + uv + the GitHub Actions runner agent,
   configured as a service that registers with
   `--ephemeral --labels self-hosted,windows,x64,anyserial-windows-serial`
   on boot and shuts down after one job.
3. Bake the AMI (`aws ec2 create-image`). Snapshot the AMI ID in a
   repository variable (`ANYSERIAL_WINDOWS_AMI`).
4. Add an orchestration job ahead of `windows-serial-self-hosted`
   (and `bench-windows`) that calls `machulav/ec2-github-runner` in
   `mode: start` to launch the instance, runs the existing jobs against
   it unchanged (labels match), then calls `mode: stop` to terminate.
5. Store AWS credentials as a scoped IAM user
   (`ec2:RunInstances` / `ec2:TerminateInstances` / `ec2:DescribeInstances`
   only) in repository secrets.
6. Drop the persistent-runner docs path from `docs/windows.md` — keep
   it as an alternative for contributors who already have hardware on
   hand, but default the instructions to the ephemeral path.

**Why we haven't yet**: the self-hosted path is working, and the
trigger gates keep it quiet enough that the cost of the migration
(AMI bake, IAM setup, workflow plumbing) isn't paying back yet. Revisit
if (a) the runner box becomes a maintenance drag, (b) we add a second
maintainer who doesn't have Windows hardware on hand, or (c) the
release cadence picks up to the point that "is the runner alive"
becomes a recurring pre-release checklist item.

**Cost reality / what actually bills**:

- GitHub Actions does **not** charge for self-hosted runner time, so
  a job queued for hours waiting on an offline runner is free on GHA's
  side. GitHub auto-expires queued jobs after 24 hours.
- What costs money is any cloud VM you left running as the runner
  host — that bills by the hour regardless of whether a job is picked
  up. Shut the VM down when you're not actively releasing.
- The two safety nets in the workflow: (1) `timeout-minutes` (30 for
  CI, 45 for bench) caps runtime once a runner picks the job up; (2)
  a `concurrency` group with `cancel-in-progress: true` prevents
  queued runs from piling up on top of a stuck one.
- **Immediate kill switch**: set repository variable
  `ANYSERIAL_RUN_SELF_HOSTED_WINDOWS` to `false` (or unset it). Both
  `windows-serial-self-hosted` and `bench-windows` gate on it, so
  nothing will be created until you flip it back.

## 11. Benchmark targets

`benchmarks/test_windows_throughput.py` (new):

| Scenario | Target | Rationale |
|---|---|---|
| Single-port round-trip, 1 B request/reply | p99 ≤ 3× Linux p99 on same hardware | USB-serial driver overhead floor. |
| Throughput at 921600 baud, 4 KiB chunks | ≥ 90% of `pyserial-asyncio` on POSIX equivalent | Correctness more than speed. |
| 32 concurrent ports (virtual COM pairs) | No thread growth; CPU scales linearly | IOCP validation. |
| Open / close | < 50 ms per cycle | Catches leaks and driver-state regressions. |

Numbers published in `docs/performance.md` alongside Linux numbers.

## 12. Scope cuts (what M10 does **not** ship)

- RS-485 (no documented runtime Win32 API — re-evaluate later).
- Low-latency mode (no Windows equivalent — documented).
- `WaitCommEvent` integration in M10.2 initial ship; added in M10.3.
- D2XX / FTDI-direct support (explicitly out of scope forever — it's
  a separate API surface that duplicates every feature).
- `SelectorEventLoop` support (explicit error, never implemented).

## 13. Sub-milestones

All sub-milestones below shipped as part of the `v0.1.0` initial
release. Retained as a record of the delivery order.

| Step | Scope | Exit (met) |
|---|---|---|
| **M10.0** ✅ | Design review approved. | Approval recorded. |
| **M10.1** ✅ | Dispatch wiring in `stream.py` `_AsyncBackendSerialPort` + empty `_windows/backend.py` skeleton. Unit tests for dispatch path via a toy `AsyncSerialBackend` mock. | `open_serial_port` branched correctly on AnyIO backends on POSIX; Windows raised `UnsupportedPlatformError`. |
| **M10.2** ✅ | `_win32.py`, `dcb.py` (with `GetCommState` round-trip), `capabilities.py`, `_runtime.py`, `backend.py` with `open/close/receive/send/drain`, both runtime paths (zero-copy `ReadFileInto` / `readinto_overlapped`), `_HandleWrapper` for asyncio proactor registration, "wait-for-any" `COMMTIMEOUTS` policy (§6.3). Integration suite on virtual COM. | Data path worked on both Trio and asyncio. |
| **M10.3** ✅ | `WaitCommEvent`, `GetCommModemStatus`, modem-line change notification; discovery via SetupAPI; error translation full matrix. | Full Windows capability surface. |
| **M10.4** ✅ | Benchmarks published; docs (`docs/windows.md`, troubleshooting updates, capability matrix); release blocker review. | Shipped in `v0.1.0`. |

## 14. Risks and mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| `loop._proactor` API changes in a CPython release | Low | Medium | CI pins every supported CPython minor; signature changes surface immediately. Accept the risk; file a CPython PR to promote if needed. |
| `_overlapped` module changes | Very low | Medium | Same as above. Stable since 3.4. |
| Trio's `register_with_iocp` / `wait_overlapped` marked unstable | Low | Medium | API is documented as a "sketch" but has been stable for years. Pin `trio >= 0.22`. |
| USB-serial driver ignores `CancelIoEx` | Medium | Low | Trio + proactor both block on completion — buffer is still safely released. Document which drivers are known-good. |
| `GetOverlappedResult(INFINITE)` eats Ctrl-C (pyserial #770) | N/A | N/A | We never call `GetOverlappedResult` with `bWait=TRUE`. Runtimes own completion waiting. |
| Hosted-runner virtual COM driver install blocked by Windows signing policy | High | Medium | Do not install kernel drivers on GitHub-hosted runners; run COM integration / benchmarks on an opt-in self-hosted Windows runner with pre-provisioned ports. |
| FTDI VCP 16 ms latency-timer default surprises users | High | Low | Documented in `docs/windows.md`; no programmatic fix (driver-GUI setting). |
| `SP_DEVICE_INTERFACE_DETAIL_DATA_W.cbSize` x86 vs x64 bug | Low | High | Unit-test both sizeof paths; guard with `sizeof(c_void_p)`. |
| Buffer GC under overlapped op | Low | Critical | We never free buffers in cancel handlers; both runtimes await real completion. |
| "Wait-for-any" timeout mode unreliable on some drivers | Medium | Medium | Some USB-serial drivers may produce spurious zero-byte completions when idle under the `MAXDWORD/MAXDWORD/1` timeout triple. Mitigation: internal retry loop suppresses empty completions; `ReadTotalTimeoutConstant` tuneable (1–10 ms); `WaitCommEvent` readiness gate available as M10.4 contingency. |
| Proactor `_register_with_iocp` expects `.fileno()` | High | High | Raw int handles lack `.fileno()` — `AttributeError` at registration. `_HandleWrapper` shim is mandatory on the asyncio path. CI must exercise the asyncio Proactor path on real Windows (not mocked). |
| Zeroed DCB drops driver-specific reserved fields | Medium | Low | `GetCommState` round-trip (§6.2.1) preserves vendor state. Test on FTDI FT232R, Prolific PL2303, CH340G before final release. |

## 15. References

- DESIGN.md §24.5 (Windows deferral), §25 (two-Protocol split), §34 (M10).
- [Trio low-level IOCP API](https://trio.readthedocs.io/en/stable/reference-lowlevel.html)
- [`trio/_core/_io_windows.py`](https://github.com/python-trio/trio/blob/master/src/trio/_core/_io_windows.py)
- [CPython `Lib/asyncio/windows_events.py`](https://github.com/python/cpython/blob/main/Lib/asyncio/windows_events.py)
- [CPython `Modules/overlapped.c`](https://github.com/python/cpython/blob/main/Modules/overlapped.c)
- [discuss.python.org — Public proactor API](https://discuss.python.org/t/public-proactor-api/102183)
- [AnyIO 4.12.0 changelog — dropped sniffio as direct dep (#1021)](https://github.com/agronholm/anyio/blob/master/docs/versionhistory.rst)
- [pyserial `serial/win32.py`](https://github.com/pyserial/pyserial/blob/master/serial/win32.py) (reference for constants only)
- [Victor Stinner — Asyncio proactor cancellation](https://vstinner.github.io/asyncio-proactor-cancellation-from-hell.html)
- [MS Learn — `COMMTIMEOUTS` structure](https://learn.microsoft.com/en-us/windows/win32/api/winbase/ns-winbase-commtimeouts) (authoritative for the `MAXDWORD/MAXDWORD/positive` "wait-for-any" mode)
- [MS Learn — Overlapped Operations](https://learn.microsoft.com/en-us/windows/win32/devio/overlapped-operations)
- [MS Learn — `GUID_DEVINTERFACE_COMPORT`](https://learn.microsoft.com/en-us/windows-hardware/drivers/install/guid-devinterface-comport)
