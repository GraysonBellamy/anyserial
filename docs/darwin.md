# macOS (Darwin)

`anyserial` ships native support for macOS. The `DarwinBackend`
subclasses the generic `PosixBackend` and layers two Darwin-specific
behaviours on top: custom baud via `IOSSIOSPEED`, and honest rejection
of Linux-only features (`low_latency`, `rs485`).

Native port discovery walks `IOSerialBSDClient` via IOKit and reports
the same `PortInfo` shape Linux's sysfs walker produces, including the
pyserial-compatible `USB VID:PID=… SER=… LOCATION=…` `hwid` string.

See
[DESIGN §24.2](https://github.com/GraysonBellamy/anyserial/blob/main/DESIGN.md#242-darwin-_darwin)
for the full rationale.

## What works

| Feature                | Status        | Notes |
|------------------------|---------------|-------|
| Standard baud rates    | ✅            | Every `termios.B*` constant. |
| Custom baud rates      | ✅            | Via `IOSSIOSPEED` (`<IOKit/serial/ioss.h>`). |
| 5 / 6 / 7 / 8 data bits| ✅            | Shared `apply_byte_size` path. |
| Even / odd / no parity | ✅            | Shared `apply_parity` path. |
| 1 / 2 stop bits        | ✅            | Shared `apply_stop_bits` path. |
| RTS/CTS hardware flow  | ✅            | Via `CCTS_OFLOW | CRTS_IFLOW` (Darwin splits `CRTSCTS`). |
| Software flow (XON/XOFF)| ✅           | `IXON | IXOFF`. |
| Break signal           | ✅            | `TIOCSBRK` / `TIOCCBRK` via `<sys/ttycom.h>` numeric fallback. |
| Modem lines (CTS/DSR/RI/CD) | ✅¹       | Shared `TIOCMGET` path; honour depends on driver. |
| RTS / DTR control      | ✅¹           | `TIOCMBIS` / `TIOCMBIC`. |
| Exclusive access       | ✅            | `flock(LOCK_EX | LOCK_NB)`. |
| Input / output waiting | ✅            | `TIOCINQ` / `TIOCOUTQ`. |
| Buffer flush           | ✅            | `tcflush`. |
| Native discovery       | ✅            | IOKit + USB-ancestor walk. |
| Runtime reconfigure    | ✅            | Re-applies `termios` + re-runs `IOSSIOSPEED` atomically. |
| Mark / space parity    | ❌            | Darwin has never defined `CMSPAR`; raises `UnsupportedFeatureError`. |
| Low-latency mode       | ❌            | No Darwin equivalent of `ASYNC_LOW_LATENCY`. Routed through `UnsupportedPolicy`. |
| Kernel RS-485          | ❌            | No Darwin equivalent of `TIOCSRS485`. Routed through `UnsupportedPolicy`. |
| 1.5 stop bits          | ❌            | No portable termios bit. |

¹ Driver-dependent in practice — pseudo terminals in particular return
  `ENOTTY` for the modem-line ioctls. `SerialCapabilities.modem_lines`
  reads `UNKNOWN` on Darwin for exactly this reason.

## Custom baud

```python
import anyio
from anyserial import SerialConfig, open_serial_port


async def main() -> None:
    async with await open_serial_port(
        "/dev/cu.usbserial-A12345BC",
        SerialConfig(baudrate=250_000),
    ) as port:
        await port.send(b"hello")


anyio.run(main)
```

Under the hood the backend commits `termios` with a placeholder
standard baud, then overrides the hardware speed with a single
`ioctl(fd, IOSSIOSPEED, &rate)`. Whether a specific adapter honours
the rate is chip-dependent; on Apple-blessed FTDI / CP210x firmware
any rate the hardware PLL can synthesize works, while older PL2303
clones may reject non-standard rates with `EINVAL`. That `EINVAL`
surfaces as
[`UnsupportedConfigurationError`](https://github.com/GraysonBellamy/anyserial/blob/main/DESIGN.md#10-exception-hierarchy)
via the usual errno mapping.

## Low-latency mode

Darwin has no equivalent of Linux's `ASYNC_LOW_LATENCY` flag.
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

The rejection runs *before* the fd is opened so the `RAISE` policy
never leaves a transiently-open device behind. `WARN` / `IGNORE`
proceed with the rest of the config applied; the latency behaviour
reverts to whatever the adapter firmware and kernel scheduler provide.

For FTDI adapters on macOS, lowering the adapter-side latency timer is
typically done via `ftdi_sio_*` command-line tools from `libftdi` or
Apple's VCP driver panel — out of scope for `anyserial`.

## Kernel RS-485

Darwin has no equivalent of Linux's `TIOCSRS485`. Same pattern as
`low_latency`: `SerialConfig(rs485=RS485Config(...))` routes through
`UnsupportedPolicy`. See [RS-485](rs485.md) for the full contract.

If you need half-duplex RS-485 on macOS today, toggle RTS manually —
the caveats in the RS-485 guide's "Manual RTS toggling" section apply
unchanged.

## Port discovery

```python
import anyio
from anyserial import list_serial_ports


async def main() -> None:
    for port in await list_serial_ports():
        print(port.device, port.vid, port.pid, port.serial_number)


anyio.run(main)
```

The enumerator walks `IOSerialBSDClient` via IOKit, prefers the
`/dev/cu.*` callout path over the `/dev/tty.*` dial-in alias (pySerial
does the same — the callout doesn't block on carrier detect), and
climbs the IOService parent tree looking for a USB-device ancestor
with `idVendor`. When it finds one, it populates
`vid` / `pid` / `serial_number` / `manufacturer` / `product` /
`location` on the resulting `PortInfo`.

On-board serial ports (the DB9 on a Mac Pro or a Thunderbolt serial
dongle exposing a non-USB backend) enumerate cleanly with
`vid` / `pid` / etc. set to `None` — no USB ancestor found.

See [Port discovery](discovery.md) for the cross-platform API; the
Darwin-specific mechanism is documented in
[DESIGN §23.1](https://github.com/GraysonBellamy/anyserial/blob/main/DESIGN.md#231-backends).

## Device-path conventions

Apple exposes two node types per serial port:

- `/dev/cu.<name>` — **callout**; does not block on carrier detect.
  This is what `anyserial` discovers, and what application code should
  open.
- `/dev/tty.<name>` — **dial-in**; blocks on `open()` until DCD asserts.
  Mostly of historical interest; use only when your protocol explicitly
  requires the dial-in semantics.

Both nodes refer to the same hardware. Opening the `tty.*` alias still
works (the shared POSIX backend doesn't care), but
`resolve_port_info()` will match it to the same underlying `PortInfo`
record, so `port.port_info.device` may read `/dev/cu.*` even when the
open was against the `tty.*` alias.

## IOKit framework bindings

The framework bindings live in
[`anyserial._darwin._iokit`](https://github.com/GraysonBellamy/anyserial/blob/main/src/anyserial/_darwin/_iokit.py)
and are loaded lazily the first time `list_serial_ports()` runs. The
module imports cleanly on non-Darwin hosts (Linux CI), so the test
suite exercises the walk logic against an in-memory `FakeIOKitClient`
without ever loading the real frameworks.

The wrapped surface is deliberately small — just enough to drive the
enumeration walk — to minimize exposure to the kind of framework-
upgrade churn flagged in the
[risk register](https://github.com/GraysonBellamy/anyserial/blob/main/DESIGN.md#36-risk-register).
If you need a richer IOKit feature set, build it on top of
`_iokit.default_client()`; the returned client satisfies a narrow
Protocol that is safe to wrap.

## CI coverage

- **Unit tests**: every Darwin module has hermetic coverage that runs
  on Linux CI (ctypes monkeypatched, `FakeIOKitClient` injected). See
  [`tests/unit/test_darwin_*.py`](https://github.com/GraysonBellamy/anyserial/tree/main/tests/unit).
- **Integration tests**: the pty-backed `test_serial_port_pty.py` and
  `test_posix_*.py` suites run on macOS via
  [`test-integration-macos`](https://github.com/GraysonBellamy/anyserial/blob/main/.github/workflows/ci.yml)
  across Python 3.13 / 3.14 × asyncio / asyncio + uvloop / trio.
- **Hardware tests**: opt-in via `ANYSERIAL_TEST_PORT`; not yet part
  of the automated matrix.
