# Windows

`anyserial` ships first-class Windows support. The `WindowsBackend`
implements the `AsyncSerialBackend` Protocol
rather than the POSIX `SyncSerialBackend` — Windows COM-port HANDLEs
don't participate in fd-readiness, so the backend owns its own async
I/O via overlapped reads/writes dispatched through each runtime's
native IOCP machinery.

See
[DESIGN §24.5](https://github.com/GraysonBellamy/anyserial/blob/main/DESIGN.md#245-windows-_windows-future)
for the Protocol rationale and
[design-windows-backend.md](https://github.com/GraysonBellamy/anyserial/blob/main/docs/design-windows-backend.md)
for the full implementation design.

## Supported runtimes

| Runtime                                   | Status        | Notes |
|-------------------------------------------|---------------|-------|
| `asyncio` (`WindowsProactorEventLoopPolicy`) | ✅ Supported | Default since Python 3.8. Uses `loop._proactor` + `_overlapped.Overlapped`. |
| `trio`                                    | ✅ Supported | Uses `trio.lowlevel.register_with_iocp` + `readinto_overlapped` / `write_overlapped`. |
| `asyncio` (`WindowsSelectorEventLoopPolicy`) | ❌ Unsupported | Raises `UnsupportedPlatformError` at open time with a message pointing back at `WindowsProactorEventLoopPolicy`. |
| `winloop` (uvloop fork for Windows)       | ❌ Unsupported | Exposes a proactor-like surface but its IOCP integration has not been verified against the overlapped paths. No CI coverage. |
| `uvloop`                                  | n/a           | uvloop does not build on Windows. |

The two supported paths cover every real Windows runtime. There is
**no worker-thread fallback** — `SelectorEventLoop` on Windows fails
fast with a clear error rather than silently scaling badly. See
[design-windows-backend.md §1](https://github.com/GraysonBellamy/anyserial/blob/main/docs/design-windows-backend.md)
for the decision.

## Installation

No Windows-specific extras are required:

```bash
uv add anyserial
# or
pip install anyserial
```

Trio support is the one optional extra that matters on Windows:

```bash
uv add "anyserial[trio]"
```

`anyserial[uvloop]` is declared only for Linux and macOS —
`uv`/`pip` will skip it on Windows without an error.

## Quick start

```python
import anyio
from anyserial import SerialConfig, open_serial_port


async def main() -> None:
    async with await open_serial_port(
        r"\\.\COM3",
        SerialConfig(baudrate=115_200),
    ) as port:
        await port.send(b"AT\r\n")
        reply = await port.receive(64)
        print(reply)


anyio.run(main)
```

The port path is `COM<n>` for `n < 10` and `\\.\COM<n>` for `n >= 10`
(the `\\.\` prefix is a Win32 namespace quirk — `COM10` without it
silently opens a file called `COM10` in the current directory, which
is exactly the bug you didn't want at 3 am). Either form works for
`COM1`–`COM9`; use `\\.\COM1` unconditionally and you never have to
remember the rule.

## What works

| Feature                | Status | Notes |
|------------------------|--------|-------|
| Standard baud rates    | ✅     | `DCB.BaudRate` is a plain integer — no B-constant table. |
| Custom baud rates      | ✅     | Same mechanism; driver decides what it accepts. |
| 5 / 6 / 7 / 8 data bits| ✅     | `DCB.ByteSize`. |
| Even / odd / no parity | ✅     | `DCB.Parity`. |
| Mark / space parity    | ✅     | `MARKPARITY` / `SPACEPARITY` are first-class. |
| 1 / 1.5 / 2 stop bits  | ✅     | Includes 1.5 (Windows-only among our platforms). |
| RTS/CTS hardware flow  | ✅     | `fOutxCtsFlow=1` + `fRtsControl=RTS_CONTROL_HANDSHAKE`. |
| DTR/DSR hardware flow  | ✅     | `fOutxDsrFlow=1` + `fDtrControl=DTR_CONTROL_HANDSHAKE`. |
| Software flow (XON/XOFF)| ✅    | `fOutX=1` + `fInX=1`. |
| Break signal           | ✅     | `SetCommBreak` / `ClearCommBreak`. |
| Modem lines (CTS/DSR/RI/CD) | ✅ | `GetCommModemStatus`. |
| RTS / DTR control      | ✅     | `EscapeCommFunction`. |
| Exclusive access       | ✅     | `CreateFileW(dwShareMode=0)` — always on, no way to disable. |
| Buffer flush           | ✅     | `PurgeComm(PURGE_RX | PURGE_TX)`. |
| Input / output waiting | ✅     | `ClearCommError` → `COMSTAT.cbInQue` / `cbOutQue`. |
| `drain()` / exact drain | ✅    | Write completion + `FlushFileBuffers`. |
| Native discovery       | ✅     | SetupAPI via `GUID_DEVINTERFACE_COMPORT`; USB VID/PID/serial extracted from hardware IDs. |
| Runtime reconfigure    | ✅     | `GetCommState` → overlay → `SetCommState` round-trip. |
| Modem-line change events | ✅   | `WaitCommEvent(EV_CTS | EV_DSR | EV_RING | EV_RLSD | EV_ERR | EV_BREAK)`. |
| Low-latency mode       | ❌     | No Windows equivalent of `ASYNC_LOW_LATENCY`. FTDI's latency timer is a driver-GUI setting. Routed through `UnsupportedPolicy`. |
| Kernel RS-485          | ❌     | FTDI VCP RS-485 mode is driver config, not a runtime API. Out of scope; revisit later. |
| `SelectorEventLoop`    | ❌     | Explicit error at open time; never implemented. |

## Event-loop requirement

The backend detects the active async runtime in `open()` (one-shot, no
hot-path cost) and verifies the asyncio loop is a Proactor:

```python
# Works: default loop policy on Python 3.8+.
import asyncio
import anyio

anyio.run(main)

# Works: explicit proactor policy.
asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
anyio.run(main)

# Raises UnsupportedPlatformError:
asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
anyio.run(main)
```

The error message tells you exactly how to fix it:

```text
UnsupportedPlatformError: anyserial requires asyncio.ProactorEventLoop
on Windows. This is the default since Python 3.8. If you have overridden
the event loop policy, switch back to WindowsProactorEventLoopPolicy:
asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy()).
```

The same rule applies to `anyio.run(main, backend="asyncio")` — the
proactor is the AnyIO-default-for-Windows as well.

## Driver-specific notes

Every real Windows serial stack goes through a USB-VCP or kernel-UART
driver. The backend is tested against:

| Driver                | Notes |
|-----------------------|-------|
| com0com (virtual)     | CI default. Always-supported baud / flow / modem-line surface; ~1 ms minimum loopback latency (driver IRP turnaround). |
| FTDI (FT232R)         | Default `latency_timer=16` ms — bytes arrive in 16 ms chunks unless you lower it via the FTDI driver GUI (no programmatic API). Adjust via **Device Manager → Ports → Advanced → Latency Timer**. |
| Prolific (PL2303)     | Standard baud rates reliable; off-brand clones may reject non-standard rates with `ERROR_INVALID_PARAMETER`. Capability still reads `SUPPORTED`; the call surfaces `UnsupportedConfigurationError` at apply time. |
| Silicon Labs (CP210x) | Generally well-behaved; full-speed USB on older firmware can limit throughput above 921600 baud. |
| CH340 / CH341         | Off-brand clones vary wildly. Custom baud may round to the nearest hardware divisor; test at the rates you actually need. |

The [DCB construction strategy](https://github.com/GraysonBellamy/anyserial/blob/main/docs/design-windows-backend.md)
(§6.2.1) does a `GetCommState` round-trip before overlaying our
config, preserving any driver-specific reserved-field state that
FTDI / Prolific / CH340 firmware stores in the DCB struct. Zeroing
those fields caused subtle misbehavior during hardware testing.

## Low-latency mode

Windows has no equivalent of Linux's `ASYNC_LOW_LATENCY` ioctl.
`SerialConfig(low_latency=True)` is routed through
[`UnsupportedPolicy`](https://github.com/GraysonBellamy/anyserial/blob/main/DESIGN.md#91-unsupported-feature-policy):

```python
from anyserial import SerialConfig, UnsupportedPolicy

# Default: raise UnsupportedFeatureError.
SerialConfig(low_latency=True)

# Warn via warnings.warn(RuntimeWarning) and proceed without low-latency.
SerialConfig(low_latency=True, unsupported_policy=UnsupportedPolicy.WARN)

# Silently continue.
SerialConfig(low_latency=True, unsupported_policy=UnsupportedPolicy.IGNORE)
```

The rejection runs *before* the HANDLE is opened so the `RAISE` policy
never leaves a transiently-open device behind.

For FTDI adapters on Windows, lowering the latency timer is a driver-
GUI setting: **Device Manager → your FTDI port → Properties → Port
Settings → Advanced → Latency Timer (msec)**. The default is 16 ms;
dropping it to 1–2 ms typically erases the "bytes arrive in 16 ms
chunks" behaviour on request/response protocols.

## Kernel RS-485

Out of scope. FTDI's VCP driver has an RS-485 mode, but
it's a driver configuration (via the driver INF or vendor tooling),
not a runtime Win32 API like Linux's `TIOCSRS485`. `SerialConfig(rs485=...)`
routes through `UnsupportedPolicy`; see [RS-485](rs485.md) for the
manual-RTS-toggling fallback.

If you have a use case for first-class Windows RS-485 — especially
one backed by a hardware reproducer — please open an issue.

## Port discovery

```python
import anyio
from anyserial import list_serial_ports


async def main() -> None:
    for port in await list_serial_ports():
        print(port.device, port.vid, port.pid, port.serial_number)


anyio.run(main)
```

The native Windows enumerator walks SetupAPI with
`GUID_DEVINTERFACE_COMPORT`
(`{86E0D1E0-8089-11D0-9CE4-08003E301F73}`) and extracts VID / PID /
serial_number from the hardware-ID string when the device is USB-
attached. On-board serial ports (motherboard COM1, PCIe UART cards)
enumerate cleanly with VID/PID/serial unpopulated.

The `hwid` string is pyserial-compatible
(`USB VID:PID=0403:6001 SER=A12345BC LOCATION=…`), so code that
already consumes `list_ports.comports()` output reads the same shape
here.

If SetupAPI enumeration fails (restricted session, driver stack
issue), the backend falls back to reading
`HKLM\HARDWARE\DEVICEMAP\SERIALCOMM` via `winreg` — device path only,
no USB metadata. The fallback is automatic, but you can force it via
`backend="pyserial"`:

```python
ports = await list_serial_ports(backend="pyserial")
```

See [Port discovery](discovery.md) for the full cross-platform API.

## Device-path conventions

| Port range | Path form |
|---|---|
| `COM1`–`COM9` | Either `"COM3"` or `r"\\.\COM3"` — both work. |
| `COM10`+ | **Must** use `r"\\.\COM10"` — the bare `"COM10"` form opens a file in the current directory. |

The backend doesn't normalize these for you at open time. Use the
`\\.\` prefix unconditionally and the question goes away.

## Cancellation

The overlapped I/O path honours `anyio.CancelScope` natively — both
Trio's `register_with_iocp` and asyncio's proactor call `CancelIoEx`
on the kernel HANDLE when the awaiting task is cancelled, and both
wait for the completion packet before releasing the buffer. There is
no post-cancel use-after-free.

```python
import anyio
from anyserial import open_serial_port

async with await open_serial_port(r"\\.\COM3") as port:
    with anyio.move_on_after(0.1):
        data = await port.receive(1024)    # cancels cleanly after 100 ms
```

`aclose()` follows a shielded, idempotent sequence:

1. `SetCommMask(handle, 0)` — wakes any pending `WaitCommEvent` cleanly.
2. `PurgeComm(PURGE_RX | PURGE_TX | ABORT)` — cancels in-flight I/O.
3. `CloseHandle(handle)` — final teardown.

Calling `aclose()` twice is safe; the second call is a no-op. See
[Cancellation](cancellation.md) for the full guarantee.

## Win32 surface

The backend writes its own ctypes bindings rather than depending on
pyserial's `serial/win32.py`. Rationale: correctness
(`use_last_error=True` and proper `errcheck` hooks throughout) and
scope (only the APIs we actually use). The binding module is
[`anyserial._windows._win32`](https://github.com/GraysonBellamy/anyserial/blob/main/src/anyserial/_windows/_win32.py);
the full API surface we wrap is enumerated in
[design-windows-backend.md §6](https://github.com/GraysonBellamy/anyserial/blob/main/docs/design-windows-backend.md).

The `loop._proactor` and `_overlapped.Overlapped` usages on the
asyncio path are private CPython APIs. They've been stable since
Python 3.4; CPython
[discuss.python.org #102183](https://discuss.python.org/t/public-proactor-api/102183)
tracks promoting them to public. See
[design-windows-backend.md §4.1](https://github.com/GraysonBellamy/anyserial/blob/main/docs/design-windows-backend.md)
for why we accept the private-API dependency.

## Error translation

Win32 error codes map to the same exception hierarchy POSIX uses:

| Win32 code                      | Exception |
|---------------------------------|-----------|
| `ERROR_FILE_NOT_FOUND` (2)      | `PortNotFoundError` |
| `ERROR_ACCESS_DENIED` (5)       | `PortBusyError` |
| `ERROR_SHARING_VIOLATION` (32)  | `PortBusyError` |
| `ERROR_INVALID_HANDLE` (6)      | `SerialClosedError` |
| `ERROR_OPERATION_ABORTED` (995) | `SerialClosedError` (absorbed on cancel path) |
| `ERROR_INVALID_PARAMETER` (87) on config | `UnsupportedConfigurationError` |
| `ERROR_DEVICE_REMOVED` (1617)   | `SerialDisconnectedError` |
| `ERROR_NOT_READY` (21)          | `SerialDisconnectedError` |
| `ERROR_GEN_FAILURE`             | `SerialDisconnectedError` |

Every `SerialError` raised from the Windows path carries a
`.winerror` attribute with the Win32 code so debug logging can pick
it up without re-parsing messages.

## CI coverage

- **Unit tests**: every Windows module has hermetic coverage
  (ctypes monkeypatched, synthetic registry / SetupAPI fixtures)
  that runs on Linux CI as well as Windows. See
  [`tests/unit/test_windows_*.py`](https://github.com/GraysonBellamy/anyserial/tree/main/tests/unit).
- **Integration tests**: the
  [`windows-serial`](https://github.com/GraysonBellamy/anyserial/blob/main/.github/workflows/ci.yml)
  job installs com0com on `windows-latest` and runs
  `tests/integration/test_windows_backend.py` across Python 3.13 / 3.14
  × asyncio (ProactorEventLoop) / Trio. A `windows-smoke` import-only
  job runs alongside it as a fallback.
- **Benchmarks**: the
  [`bench-windows`](https://github.com/GraysonBellamy/anyserial/blob/main/.github/workflows/bench.yml)
  job publishes nightly com0com numbers for the four scenarios in
  [design-windows-backend.md §11](https://github.com/GraysonBellamy/anyserial/blob/main/docs/design-windows-backend.md).
  See [Performance](performance.md#windows-com0com) for the published
  numbers.
- **Hardware tests**: opt-in via `ANYSERIAL_TEST_PORT`; not yet part
  of the automated matrix — Windows hardware coverage is welcome via
  PR.

## Known limitations

- **No `low_latency` knob** — driver-level tuning only. See the
  FTDI Device Manager procedure above.
- **No kernel RS-485** — manual RTS toggling is the workaround; see
  [RS-485](rs485.md).
- **No `winloop`** — its IOCP integration hasn't been verified.
  `winloop` would be a drop-in for the proactor path in principle,
  but claiming support requires dedicated CI coverage we don't have.
- **No `SelectorEventLoop`** — explicit error. Use the proactor.
- **No D2XX / FTDI-direct** — out of scope permanently. D2XX duplicates
  every feature of the VCP driver behind a vendor-specific API; we
  stay on the kernel VCP path so the same code handles every adapter.

## Reporting issues

If you hit a Windows-specific rejection or a driver quirk, please
include:

- Python version (`python -V`) and `asyncio.get_event_loop_policy().__class__.__name__`.
- Adapter chipset and driver version (Device Manager → Properties →
  Driver tab).
- `SerialConfig` you passed, the `.winerror` code from the raised
  exception, and the full traceback.
- Whether the same action works under `pySerial` — helps separate
  driver-level bugs from library bugs.
