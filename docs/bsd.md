# BSD (FreeBSD, NetBSD, OpenBSD, DragonFly)

`anyserial` ships a single `BsdBackend` for the BSD family. Per
[DESIGN §24.3](https://github.com/GraysonBellamy/anyserial/blob/main/DESIGN.md#243-bsd-_bsd)
and [§36](https://github.com/GraysonBellamy/anyserial/blob/main/DESIGN.md#36-risk-register),
BSD support is **best-effort** — the variants share enough of the
termios surface that one backend handles all four, but per-variant
driver differences only surface at hardware-test time, and the
backend ships without a hardware-test gate in CI.

Translation: the shared termios path is exercised by 44 unit tests on
every CI run, and a FreeBSD unit-test smoke job runs on push to main;
if you hit a driver-specific rejection on NetBSD / OpenBSD /
DragonFly we'd like to hear about it in a bug report with a hardware
reproducer.

## Variants covered

| Variant     | Dispatch predicate                | Discovery pattern set |
|-------------|-----------------------------------|-----------------------|
| FreeBSD     | `sys.platform.startswith("freebsd")` | `cuaU*`, `cuau*`, `cuad*`, `ttyU*`, `ttyu*` |
| OpenBSD     | `sys.platform.startswith("openbsd")` | `cuaU*`, `cua0*`, `cua1*` |
| NetBSD      | `sys.platform.startswith("netbsd")`  | `dtyU*`, `dty0*`, `ttyU*` |
| DragonFly   | `sys.platform.startswith("dragonfly")` | `cuaU*`, `cuau*` |

The selector routes every match to the same `BsdBackend`; variant-
specific behaviour lives in the `/dev` glob set and (should the need
arise) future branch points inside `_bsd/baudrate.py` /
`_bsd/capabilities.py`.

## What works

| Feature                | Status        | Notes |
|------------------------|---------------|-------|
| Standard baud rates    | ✅            | Every `termios.B*` constant. |
| Custom baud rates      | ✅            | Integer passthrough — BSDs store `c_ispeed` / `c_ospeed` as plain ints. |
| 5 / 6 / 7 / 8 data bits| ✅            | Shared `apply_byte_size` path. |
| Even / odd / no parity | ✅            | Shared `apply_parity` path. |
| 1 / 2 stop bits        | ✅            | Shared `apply_stop_bits` path. |
| RTS/CTS hardware flow  | ✅            | `CRTSCTS` on FreeBSD; `CCTS_OFLOW | CRTS_IFLOW` on older BSDs. |
| Software flow (XON/XOFF)| ✅           | `IXON | IXOFF`. |
| Break signal           | ✅            | `TIOCSBRK` / `TIOCCBRK` via `<sys/ttycom.h>` numeric fallback. |
| Exclusive access       | ✅            | `flock(LOCK_EX | LOCK_NB)`. |
| Input waiting          | ✅            | `TIOCINQ`. |
| Buffer flush           | ✅            | `tcflush`. |
| Native discovery       | ✅            | `/dev`-scan per variant; device path only. |
| Runtime reconfigure    | ✅            | Shared `configure()` serialization. |
| Modem lines (CTS/DSR/RI/CD) | 🟡¹      | Shared `TIOCMGET` path; not hardware-verified per variant. |
| RTS / DTR control      | 🟡¹           | `TIOCMBIS` / `TIOCMBIC`; same caveat. |
| Mark / space parity    | 🟡¹           | Depends on variant — newer FreeBSD exposes `CMSPAR`, older BSDs don't. |
| Output waiting         | 🟡¹           | `TIOCOUTQ` exists but `ICANON` interaction varies. |
| USB metadata in discovery | 🟡¹        | Not populated by native enumerator; use `backend="pyserial"` for now. |
| Low-latency mode       | ❌            | No BSD equivalent of `ASYNC_LOW_LATENCY`. Routed through `UnsupportedPolicy`. |
| Kernel RS-485          | ❌            | FreeBSD has `TIOCSRS485` but it's driver-specific; out of scope. |
| 1.5 stop bits          | ❌            | No portable termios bit. |

¹ `Capability.UNKNOWN` in the backend snapshot — the mechanism is
  reachable but hardware validation is pending. `UNKNOWN` capabilities
  still raise `UnsupportedConfigurationError` at apply time if the
  driver rejects the operation, routed through `UnsupportedPolicy` in
  the usual way.

## Custom baud

```python
import anyio
from anyserial import SerialConfig, open_serial_port


async def main() -> None:
    async with await open_serial_port(
        "/dev/cuaU0",  # FreeBSD USB-serial callout
        SerialConfig(baudrate=250_000),
    ) as port:
        await port.send(b"hello")


anyio.run(main)
```

BSD's `tcsetattr` accepts arbitrary integer rates via `c_ispeed` /
`c_ospeed` directly — no dedicated ioctl like Linux's `TCSETS2` or
Darwin's `IOSSIOSPEED`. The backend drops the integer rate straight
into the termios struct via a single `tcsetattr` call.

As with every platform, driver-level rejection surfaces as
[`UnsupportedConfigurationError`](https://github.com/GraysonBellamy/anyserial/blob/main/DESIGN.md#10-exception-hierarchy)
via the standard errno mapping.

## Port discovery

```python
import anyio
from anyserial import list_serial_ports


async def main() -> None:
    for port in await list_serial_ports():
        # device, name populated — vid/pid/serial are None on native BSD.
        print(port.device, port.name)


anyio.run(main)
```

The BSD enumerator scans `/dev` with the glob set shown above and
returns `PortInfo` records with the **device path** and **basename**
populated. USB metadata (VID / PID / serial / manufacturer / product)
is deliberately left unpopulated because each BSD variant
exposes USB metadata through a different mechanism (`usbconfig` on
FreeBSD, `drvctl` on NetBSD, `sysctl` on OpenBSD) and validating each
path requires hardware testing that's out of scope for now.

If you need VID/PID on BSD today, use the `pyserial` fallback:

```python
ports = await list_serial_ports(backend="pyserial")
```

`pyserial.tools.list_ports` wraps the per-variant tooling and is a
stable, reasonably well-maintained reference. See
[Port discovery](discovery.md#backends) for the full fallback matrix.

## Low-latency mode

No BSD equivalent of `ASYNC_LOW_LATENCY`. Same pattern as the Darwin
backend — `SerialConfig(low_latency=True)` routes through
[`UnsupportedPolicy`](https://github.com/GraysonBellamy/anyserial/blob/main/DESIGN.md#91-unsupported-feature-policy):

```python
from anyserial import SerialConfig, UnsupportedPolicy

# Default: raise UnsupportedFeatureError.
SerialConfig(low_latency=True)

# Warn and proceed without low-latency.
SerialConfig(low_latency=True, unsupported_policy=UnsupportedPolicy.WARN)
```

The rejection runs *before* the fd is opened so `RAISE` never leaves
a transient fd behind.

## Kernel RS-485

Out of scope. FreeBSD has `TIOCSRS485` support in some
drivers, but the coverage and behaviour differ enough from Linux that
validating them requires hardware for each variant. `UnsupportedPolicy`
handles the rejection; see [RS-485](rs485.md) for the full contract
and the manual-RTS-toggling fallback.

If you have RS-485 hardware on BSD and want first-class support,
please open an issue — we'll wire up the `TIOCSRS485` path from the
Linux backend against your driver and flip the capability on a
variant-by-variant basis.

## Device-path conventions

Per variant (callout nodes preferred — they don't block on carrier
detect):

- **FreeBSD / DragonFly**: `/dev/cuaU*` (USB), `/dev/cuau*` (on-board,
  modern `uart(4)` driver), `/dev/cuad*` (legacy `sio(4)` driver).
  Dial-in aliases at `/dev/ttyU*` / `/dev/ttyu*`.
- **OpenBSD**: `/dev/cua*` — the same callout-path prefix covers both
  on-board UARTs (`cua00`, `cua01`, …) and USB-serial (`cuaU0`,
  `cuaU1`, …).
- **NetBSD**: `/dev/dtyU*` for USB callout, `/dev/dty0*` for on-board
  callout, `/dev/ttyU*` for USB dial-in.

Opening a dial-in alias still works — the generic POSIX backend doesn't
care which node was opened — but discovery may not surface it depending
on variant. If in doubt, open the callout path.

## CI coverage

- **Unit tests**: every BSD module has hermetic coverage that runs on
  every CI run (synthetic `/dev` trees under `tmp_path`, `termios` +
  `os.open` monkeypatched). See
  [`tests/unit/test_bsd_*.py`](https://github.com/GraysonBellamy/anyserial/tree/main/tests/unit).
- **FreeBSD smoke**: a
  [`freebsd-smoke`](https://github.com/GraysonBellamy/anyserial/blob/main/.github/workflows/ci.yml)
  job runs the unit suite inside a FreeBSD 14 VM via
  `cross-platform-actions/action` on push to main. Best-effort
  (`continue-on-error: true`); surfaces regressions in the BSD code
  paths even without a real adapter.
- **Integration tests**: not run on BSD. The pty-backed
  `test_serial_port_pty.py` suite would work on FreeBSD, but standing
  up the full integration matrix in the VM action is deferred until
  hardware tests land.
- **Hardware tests**: not yet part of the automated matrix; opt-in
  hardware coverage is welcome via PR.

## Reporting issues

If you hit a BSD-specific rejection or a driver quirk, please include:

- `sys.platform` (e.g. `freebsd14`, `openbsd7`, `netbsd11`).
- The adapter's chipset (FTDI, CP210x, CH340, etc.) and the kernel
  driver name (`kldstat` on FreeBSD, `dmesg | grep ucom` elsewhere).
- The failing operation and the `OSError.errno` (if any).

BSD coverage grows on demand — hardware reproducers are the fastest
path from `UNKNOWN` to `SUPPORTED` in the capability snapshot.
