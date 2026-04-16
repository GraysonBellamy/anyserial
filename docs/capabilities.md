# Capabilities

`SerialCapabilities` is the honest answer to "does this backend
support feature X?" Real serial stacks have three answers, not two —
a feature can be definitely supported, definitely not, or *maybe*,
depending on what driver or chip the kernel finds at the other end
of the fd. `anyserial` models all three with the `Capability`
tri-state instead of forcing every feature into a boolean.

See [DESIGN §9](https://github.com/GraysonBellamy/anyserial/blob/main/DESIGN.md#9-capability-model)
for the full rationale.

## The tri-state

```python
from anyserial import Capability

Capability.SUPPORTED    # mechanism exists on this platform/backend
Capability.UNSUPPORTED  # mechanism does not exist here
Capability.UNKNOWN      # mechanism reachable but driver/device may reject
```

`UNKNOWN` is the important one: it means "the platform has an ioctl
for this, but whether a specific adapter will accept a specific call
is only knowable at the moment you make the call." `TIOCSRS485` on
Linux is the canonical example — the kernel advertises it platform-
wide, but most USB-serial bridges return `ENOTTY`.

## Reading capabilities

Capabilities are reported by the backend and surfaced on the open
port:

```python
import anyio
from anyserial import open_serial_port


async def main() -> None:
    async with await open_serial_port("/dev/ttyUSB0") as port:
        caps = port.capabilities
        print(caps.platform, caps.backend)
        print("custom baud:", caps.custom_baudrate)
        print("RS-485:    ", caps.rs485)
        print("low latency:", caps.low_latency)


anyio.run(main)
```

The same snapshot is available via the typed-attribute interface for
AnyIO-polymorphic code:

```python
from anyserial import SerialStreamAttribute

caps = port.extra(SerialStreamAttribute.capabilities)
```

## Every field

`SerialCapabilities` has two identifier strings (`platform`,
`backend`) and a tri-state per feature:

| Field | What it means |
|---|---|
| `custom_baudrate` | Non-standard integer baud rates |
| `mark_space_parity` | `Parity.MARK` / `Parity.SPACE` |
| `one_point_five_stop_bits` | `StopBits.ONE_POINT_FIVE` |
| `xon_xoff` | Software flow control |
| `rts_cts` | RTS/CTS hardware flow control |
| `dtr_dsr` | DTR/DSR hardware flow control |
| `modem_lines` | `TIOCMGET` status read |
| `break_signal` | `TIOCSBRK` / `TIOCCBRK` |
| `exclusive_access` | `flock(LOCK_EX)` |
| `low_latency` | Kernel low-latency knob (Linux `ASYNC_LOW_LATENCY`) |
| `rs485` | Kernel RS-485 (`TIOCSRS485`) |
| `input_waiting` | `TIOCINQ` queue-depth read |
| `output_waiting` | `TIOCOUTQ` queue-depth read |
| `port_discovery` | Native port enumeration |

## Platform baseline

Approximate defaults per platform — individual drivers can still
return `ENOTTY` (POSIX) or `ERROR_INVALID_PARAMETER` (Windows) at
apply time:

| Capability | Linux | macOS | BSD | Windows |
|---|---|---|---|---|
| `custom_baudrate` | ✅ SUPPORTED | ✅ SUPPORTED | ✅ SUPPORTED | ✅ SUPPORTED⁵ |
| `mark_space_parity` | ✅ SUPPORTED | ❌ UNSUPPORTED¹ | 🟡 UNKNOWN² | ✅ SUPPORTED |
| `one_point_five_stop_bits` | ❌ UNSUPPORTED | ❌ UNSUPPORTED | ❌ UNSUPPORTED | ✅ SUPPORTED |
| `xon_xoff` | ✅ SUPPORTED | ✅ SUPPORTED | ✅ SUPPORTED | ✅ SUPPORTED |
| `rts_cts` | ✅ SUPPORTED | ✅ SUPPORTED | ✅ SUPPORTED | ✅ SUPPORTED |
| `dtr_dsr` | 🟡 UNKNOWN | 🟡 UNKNOWN | 🟡 UNKNOWN | ✅ SUPPORTED |
| `modem_lines` | ✅ SUPPORTED | 🟡 UNKNOWN³ | 🟡 UNKNOWN | ✅ SUPPORTED |
| `break_signal` | ✅ SUPPORTED | ✅ SUPPORTED | ✅ SUPPORTED | ✅ SUPPORTED |
| `exclusive_access` | ✅ SUPPORTED | ✅ SUPPORTED | ✅ SUPPORTED | ✅ SUPPORTED⁶ |
| `low_latency` | ✅ SUPPORTED | ❌ UNSUPPORTED | ❌ UNSUPPORTED | ❌ UNSUPPORTED⁷ |
| `rs485` | ✅ SUPPORTED | ❌ UNSUPPORTED | ❌ UNSUPPORTED | ❌ UNSUPPORTED⁸ |
| `input_waiting` | ✅ SUPPORTED | ✅ SUPPORTED | ✅ SUPPORTED | ✅ SUPPORTED |
| `output_waiting` | ✅ SUPPORTED | ✅ SUPPORTED | 🟡 UNKNOWN | ✅ SUPPORTED |
| `port_discovery` | ✅ SUPPORTED | ✅ SUPPORTED | ✅ SUPPORTED⁴ | ✅ SUPPORTED |

¹ Darwin has never defined `CMSPAR`; request routes through
  `UnsupportedPolicy`.
² FreeBSD has `CMSPAR` in newer releases; older BSDs don't.
³ Many macOS drivers return `ENOTTY` for modem-line ioctls on pseudo
  terminals and non-USB paths.
⁴ BSD native enumeration returns device path and basename only —
  use `backend="pyserial"` for USB metadata. See [BSD](bsd.md#port-discovery).
⁵ `DCB.BaudRate` is a plain integer; the USB-VCP driver decides
  whether it accepts a given rate. Off-brand CH340 / PL2303 clones
  often reject non-standard rates with `ERROR_INVALID_PARAMETER`.
⁶ Windows COM ports are **always** exclusive — `CreateFileW` is
  called with `dwShareMode=0` and there is no way to share a HANDLE.
⁷ No Win32 equivalent of `ASYNC_LOW_LATENCY`. FTDI's latency timer
  is a driver-GUI setting — see [Windows](windows.md#low-latency-mode).
⁸ FTDI VCP RS-485 is driver config, not a runtime API. Out of scope —
  see [Windows / Kernel RS-485](windows.md#kernel-rs-485).

For the authoritative "what works" matrix per platform see
[macOS](darwin.md), [BSD](bsd.md), and [Windows](windows.md).

## Gating features on a capability

Check before you try — skip the driver round-trip and the
`UnsupportedPolicy` branch entirely:

```python
from anyserial import Capability

if port.capabilities.rs485 is Capability.SUPPORTED:
    await port.configure(port.config.with_changes(rs485=RS485Config()))
else:
    # Fall back to manual RTS toggling, or just error out.
    ...
```

For `UNKNOWN`, the only way to find out is to try:

```python
from anyserial import UnsupportedConfigurationError

try:
    await port.configure(port.config.with_changes(parity=Parity.MARK))
except UnsupportedConfigurationError:
    # Driver rejected CMSPAR. Fall back or report.
    ...
```

`UnsupportedPolicy` offers the same escape without the `try` /
`except` — see [Configuration](configuration.md#unsupported-feature-policy).

## `SUPPORTED` doesn't guarantee the specific request works

`SerialCapabilities` reports what the *platform* can do, not what
the *device* can do. Linux reports `custom_baudrate = SUPPORTED`
platform-wide, but a specific CH340 clone may still return `EINVAL`
when you ask for 500 000 bps. That's why every apply-time failure
surfaces as `UnsupportedConfigurationError` even on capabilities
that read `SUPPORTED`.

Rule of thumb:

- Check capabilities to skip features your platform can't do at all.
- Always be ready to catch `UnsupportedConfigurationError` when
  actually applying the config.
- Use `unsupported_policy=WARN`/`IGNORE` on fields that are
  nice-to-have across heterogeneous hardware.

## Discovering the backend and platform

The two identifier strings let you branch on the runtime:

```python
caps = port.capabilities
if caps.backend == "linux" and caps.rs485 is Capability.SUPPORTED:
    ...
```

Backend names:

| `caps.backend` | Module |
|---|---|
| `"linux"` | `anyserial._linux.backend` |
| `"darwin"` | `anyserial._darwin.backend` |
| `"bsd"` | `anyserial._bsd.backend` |
| `"mock"` | `anyserial.testing.MockBackend` |
| `"windows"` | `anyserial._windows.backend` |

Future platforms will add their own backend names — don't hard-code
the current set in library code; prefer the per-field tri-state.
