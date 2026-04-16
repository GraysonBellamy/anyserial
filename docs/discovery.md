# Port discovery

`anyserial` ships an async API for enumerating connected serial ports
and resolving metadata for any single device path. Discovery is always
live — there's no caching layer, so unplug events show up on the next
call. See [DESIGN §23](https://github.com/GraysonBellamy/anyserial/blob/main/DESIGN.md#23-port-discovery)
for the full rationale.

## Quick start

```python
import anyio
from anyserial import find_serial_port, list_serial_ports, open_serial_port


async def main() -> None:
    # Enumerate every port the platform exposes.
    for port in await list_serial_ports():
        print(port.device, port.vid, port.pid, port.serial_number)

    # Find a specific adapter by VID / PID.
    ftdi = await find_serial_port(vid=0x0403, pid=0x6001)
    if ftdi is None:
        raise RuntimeError("FT232R not connected")

    # Metadata is automatically attached to the open port.
    async with await open_serial_port(ftdi.device) as port:
        info = port.port_info
        assert info is not None
        print(f"opened {info.product} (S/N {info.serial_number})")


anyio.run(main)
```

## `PortInfo`

Every discovered port is a frozen, hashable `PortInfo` dataclass:

```python
@dataclass(frozen=True, slots=True, kw_only=True)
class PortInfo:
    device: str                  # always populated
    name: str | None = None
    description: str | None = None
    hwid: str | None = None      # "USB VID:PID=0403:6001 SER=A12345BC LOCATION=1-1"
    vid: int | None = None
    pid: int | None = None
    serial_number: str | None = None
    manufacturer: str | None = None
    product: str | None = None
    location: str | None = None
    interface: str | None = None
```

Only `device` is guaranteed non-`None`. USB-attached adapters typically
populate `vid` / `pid` / `serial_number` / `manufacturer` / `product`;
on-board UARTs and virtual ports usually leave them empty. Equality is
by-value, so `PortInfo` is safe in sets and as dict keys.

## Filter API

```python
match = await find_serial_port(
    vid=0x0403,
    pid=0x6001,
    serial_number="A12345BC",  # optional
    device="/dev/ttyUSB0",     # optional
)
```

Filters are AND-ed together. Any unset filter contributes no constraint.
Returns the first match in `list_serial_ports()` order, or `None` when
no port satisfies every filter.

## `port.port_info` after open

`open_serial_port(...)` resolves the path through the same discovery
backend and stashes the result on `port.port_info` (also exposed as the
`SerialStreamAttribute.port_info` typed attribute):

```python
from anyio import TypedAttributeLookupError
from anyserial import SerialStreamAttribute

async with await open_serial_port("/dev/ttyUSB0") as port:
    info = port.port_info  # PortInfo | None

    try:
        same_info = port.extra(SerialStreamAttribute.port_info)
    except TypedAttributeLookupError:
        # The path didn't map to a discoverable entry — pseudo terminal,
        # platform without a native backend yet, etc.
        same_info = None
```

The two are equivalent. The typed attribute is *omitted* (rather than
present-and-`None`) when discovery couldn't resolve metadata, matching
AnyIO's convention. Use the property for ergonomic `is None` checks; use
`extra(..., default)` when you'd rather pass a sentinel.

## Backends

`list_serial_ports` and `find_serial_port` accept a `backend=` keyword:

| Backend    | Platforms            | Source                              | Extra                            |
|------------|----------------------|-------------------------------------|----------------------------------|
| `native`   | Linux, macOS, BSD    | see [platform matrix](#platform-support) | (none — built in)                |
| `pyudev`   | Linux                | libudev via `pyudev`                | `anyserial[discovery-pyudev]`    |
| `pyserial` | Any                  | `pyserial.tools.list_ports`         | `anyserial[discovery-pyserial]`  |

```python
ports = await list_serial_ports(backend="pyserial")
```

The native backend is the default on every platform where one is
implemented. Linux and macOS produce `hwid` strings that are
byte-for-byte compatible with `pyserial`'s; BSD's native enumerator
populates only the device path + basename (see
[BSD](bsd.md#port-discovery) for the rationale). The fallbacks exist
for three cases:

- `pyudev` — udev rules can attach extra metadata (`ID_PATH`,
  database-resolved manufacturer / product strings) the raw sysfs walk
  doesn't see. Useful on distros with a curated udev hwdb.
- `pyserial` — recommended source of USB metadata on BSD
  (the native enumerator returns device path only), and a handy
  cross-check on any platform — especially when migrating from
  pySerial.
- Cross-check during migration from pySerial on any platform.

Each fallback raises `ImportError` with the exact install command if the
optional extra isn't installed.

## Platform support

| Platform | Native (`native`) | Fallback (`pyudev`) | Fallback (`pyserial`) | Status |
|----------|-------------------|---------------------|------------------------|--------|
| Linux    | ✅ sysfs                   | ✅                   | ✅                      | First-class |
| macOS    | ✅ IOKit                   | n/a                  | ✅                      | First-class |
| BSD      | ✅ `/dev` scan             | n/a                  | ✅                      | Best-effort — see [BSD](bsd.md) |
| Windows  | ✅ SetupAPI                | n/a                  | ✅                      | First-class — see [Windows](windows.md) |

### What "native" means per platform

- **Linux** — walks `/sys/class/tty`, resolves the USB ancestor for
  each entry, and populates VID / PID / serial / manufacturer /
  product / location / interface.
- **macOS** — walks `IOSerialBSDClient` via IOKit, prefers the
  `/dev/cu.*` callout path, and climbs the registry parent chain for
  USB metadata. See [macOS](darwin.md#port-discovery).
- **BSD** — scans `/dev` for the per-variant callout patterns
  (`/dev/cuaU*` on FreeBSD, `/dev/cua*` on OpenBSD, `/dev/dty*` on
  NetBSD). USB metadata is **not populated**; use
  `backend="pyserial"` if you need VID/PID.
- **Windows** — enumerates `GUID_DEVINTERFACE_COMPORT` via SetupAPI,
  extracts VID/PID/serial_number from the hardware-ID string, and
  falls back automatically to `HKLM\HARDWARE\DEVICEMAP\SERIALCOMM`
  via `winreg` (device path only) when SetupAPI enumeration fails.
  The `hwid` string is pyserial-compatible. See
  [Windows](windows.md#port-discovery).

Calling `list_serial_ports()` on a platform without a native backend
raises `UnsupportedPlatformError`. Pass an explicit
`backend="pyserial"` to get cross-platform discovery in the meantime.

## Caching

There is none. Every call performs the underlying enumeration. Callers
that need caching wrap it themselves — typical patterns are
`functools.lru_cache` on a sync wrapper for short-lived scripts, or a
manually-invalidated cache that listens for udev events for daemons.

The "always live" choice means USB unplug / replug is reflected on the
next call, which is what most users expect.
