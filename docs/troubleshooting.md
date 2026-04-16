# Troubleshooting

Common failure modes when opening and using a serial port, with the
fastest diagnostic and fix for each. `anyserial`'s exceptions all
multi-inherit from stdlib or AnyIO bases, so existing `except`
clauses usually still work — but the subclass name is the fastest
way to pinpoint the cause.

See [DESIGN §10](https://github.com/GraysonBellamy/anyserial/blob/main/DESIGN.md#10-exception-hierarchy)
for the full exception hierarchy.

## Permission denied on `/dev/ttyXXX`

```
PortBusyError: [Errno 13] Permission denied: '/dev/ttyUSB0'
```

The process does not have read/write on the device node.

**Diagnose:**

```bash
ls -l /dev/ttyUSB0
# crw-rw---- 1 root dialout 188, 0 ...
groups
```

**Fix:**

```bash
# Debian / Ubuntu
sudo usermod -aG dialout "$USER"
# Arch / Fedora
sudo usermod -aG uucp "$USER"
```

Log out and back in. See [Linux tuning](linux-tuning.md#permissions).

## Port is already in use

```
PortBusyError: [Errno 16] Device or resource busy: '/dev/ttyUSB0'
```

Another process holds the port open. Common culprits: `gtkterm`,
`minicom`, `screen`, a previous crash that left the process alive,
a stale ModemManager lock.

**Diagnose:**

```bash
sudo lsof /dev/ttyUSB0
sudo fuser /dev/ttyUSB0
```

**Fix:** kill the holder or wait for it to exit. If ModemManager
keeps grabbing the adapter, mask it for the port:

```bash
# One-off: disable ModemManager entirely.
sudo systemctl disable --now ModemManager

# Or: udev rule that tells ModemManager to ignore a VID/PID.
echo 'SUBSYSTEM=="tty", ATTRS{idVendor}=="0403", ENV{ID_MM_DEVICE_IGNORE}="1"' \
    | sudo tee /etc/udev/rules.d/99-no-modemmanager.rules
sudo udevadm control --reload
```

The `exclusive=True` config option uses `flock(LOCK_EX)` which
cooperates with other `flock`-aware tools but **not** with raw
`open` callers. For hard exclusivity, close before re-opening and
don't rely on `flock` as a mutex against unknown peers.

## Port does not exist

```
PortNotFoundError: [Errno 2] No such file or directory: '/dev/ttyUSB0'
```

The path is wrong, the adapter isn't plugged in, or the kernel
driver didn't attach.

**Diagnose:**

```bash
ls /dev/tty{USB,ACM}* 2>/dev/null
dmesg | tail -n 30
```

If nothing shows up, the USB bus may not see it (`lsusb`), or the
driver may not be loaded (`modprobe ftdi_sio`). Plug events land in
`dmesg`; look for `ftdi_sio` / `cp210x` / `ch341` / `pl2303`.

## EINVAL on a custom baud rate

```
UnsupportedConfigurationError: [Errno 22] Invalid argument: '/dev/ttyUSB0'
```

The driver couldn't synthesize the requested rate. Typical:

- Off-brand CH340 / PL2303 clones reject non-standard rates.
- Older FTDI firmware can synthesize arbitrary rates; some clones
  can't.
- On macOS, older PL2303 drivers reject `IOSSIOSPEED`.

**Diagnose:**

```python
# What rates does the platform claim to support?
print(port.capabilities.custom_baudrate)  # Capability.SUPPORTED still possible
```

**Fix:**

- Try a standard rate (`9600`, `19200`, `115_200`, `230_400`,
  `460_800`, `921_600`).
- Use a genuine FTDI FT232R or an 8250-based PCIe UART.
- Set `unsupported_policy=UnsupportedPolicy.WARN` or `IGNORE` to
  keep going with the standard rate the driver picked.

See [Configuration](configuration.md#custom-baud).

## Device disappeared during I/O

```
SerialDisconnectedError: [Errno 5] Input/output error: '/dev/ttyUSB0'
```

The adapter was unplugged, the kernel detached the driver, or the
device stopped responding. `SerialDisconnectedError` is a subclass
of `anyio.BrokenResourceError`, so generic AnyIO retry logic
catches it.

**Diagnose:**

```bash
dmesg | tail -n 20
ls /dev/ttyUSB* /dev/ttyACM*
```

**Fix:** re-plug, call `open_serial_port` again. There's no in-band
reconnect — reopen from scratch so every piece of kernel state
(termios, `ASYNC_LOW_LATENCY`, `struct serial_rs485`, FTDI latency
timer) is re-applied.

## `receive()` returns nothing but bytes are on the wire

Three common causes.

**1. Wrong baud / parity / stop bits.** Characters arrive framed
wrong, the kernel drops or reframes them, and userspace sees nothing
or gibberish. Verify against the peer with `stty -F /dev/ttyUSB0 -a`
and fix the `SerialConfig`.

**2. Terminal was not put in raw mode.** `anyserial` opens in raw
mode automatically. If you're sharing the fd with another process
that called `tcsetattr` with `ICANON`, lines are buffered until
newline. Don't share fds across tools.

**3. Flow control mismatch.** Peer is asserting CTS low (or sending
XOFF) and your code doesn't know. Check
`await port.modem_lines()` for CTS/DSR state; make sure
`flow_control` matches the other end.

## Bytes arrive in 16 ms chunks

You're probably on an FTDI adapter with the default
`latency_timer=16`. Open with `low_latency=True` to drop it to 1 ms:

```python
SerialConfig(baudrate=115_200, low_latency=True)
```

See [Linux tuning](linux-tuning.md#low-latency-mode) for the
detail.

## `UnsupportedFeatureError: TIOCSRS485`

Most consumer USB-serial bridges don't implement kernel RS-485.
`SerialCapabilities.rs485` reads `SUPPORTED` on Linux because the
*platform* has the ioctl, but a specific driver may return `ENOTTY`.

**Fix:**

- Use an adapter whose driver implements `TIOCSRS485` (genuine FTDI
  chips on recent kernels, most industrial PCIe UART cards).
- Or set `unsupported_policy=UnsupportedPolicy.WARN`/`IGNORE` and
  toggle RTS manually per [RS-485 / Manual RTS toggling](rs485.md#manual-rts-toggling).

## Capability reads `UNKNOWN`

That's not an error — it means "platform has the mechanism, driver
may still reject it." The request routes through
`UnsupportedPolicy` at apply time; see
[Capabilities](capabilities.md#supported-doesnt-guarantee-the-specific-request-works).

## `ResourceWarning: unclosed serial port`

A `SerialPort` was garbage-collected while still open. Usually a
missing `async with` block or a swallowed exception that skipped
`aclose`.

**Fix:**

```python
async with await open_serial_port(path) as port:
    ...
```

Every public entry point supports context-manager lifecycle. The
warning is deliberately loud — the finalizer closes synchronously
as a best effort, but cannot emit `notify_closing`, so parked tasks
are only reclaimed by AnyIO's own cleanup. See
[DESIGN §15](https://github.com/GraysonBellamy/anyserial/blob/main/DESIGN.md#15-resource-management-and-teardown).

## Discovery returns no ports (or wrong ones)

**Linux.** The native walker reads `/sys/class/tty`. On containers
with a restricted sysfs, it sees nothing. Fall back to pyudev or
pyserial:

```python
ports = await list_serial_ports(backend="pyudev")
# or
ports = await list_serial_ports(backend="pyserial")
```

**macOS.** The native enumerator prefers `/dev/cu.*`. If you need
`/dev/tty.*` (dial-in, rare), open the path directly — discovery
still resolves metadata.

**BSD.** Native discovery returns device path + basename only. Use
`backend="pyserial"` for VID/PID.

**Windows.** Native SetupAPI-based discovery populates
VID/PID/serial_number for USB adapters. Falls back automatically to
`HKLM\HARDWARE\DEVICEMAP\SERIALCOMM` via `winreg` if
SetupAPI enumeration fails (restricted session, driver stack issue);
the fallback returns device path only. Force the pyserial backend if
you want cross-check behaviour:

```python
ports = await list_serial_ports(backend="pyserial")
```

See [Discovery / Backends](discovery.md#backends).

## `anyio.BusyResourceError` on concurrent reads

Two tasks in the same process called `receive()` (or both called
`send()`) on the same port. `SerialPort` allows full-duplex
send+receive but not concurrent reads or concurrent writes.

**Fix:** one reader task, one writer task. Or serialize both sides
through an `anyio.Lock`.

See [Cancellation / Task groups](cancellation.md#task-groups).

## Timeouts don't seem to fire on `send`

`send` spends its time in `anyio.wait_writable`, which is
cancellable. If you're not seeing a timeout trip, the send is
probably already completing fast — check whether it actually blocks:

```python
with anyio.fail_after(0.001):      # absurdly short
    await port.send(b"x" * 64)
# If this doesn't raise, your send wasn't blocking.
```

Kernel output queues only fill when flow control is asserted or the
wire speed is saturated. A send into an empty queue returns
immediately.

## Windows-specific

### `UnsupportedPlatformError: anyserial requires asyncio.ProactorEventLoop`

```text
UnsupportedPlatformError: anyserial requires asyncio.ProactorEventLoop
on Windows. This is the default since Python 3.8. If you have overridden
the event loop policy, switch back to WindowsProactorEventLoopPolicy...
```

You (or something in your stack) called
`asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())`.
`anyserial`'s Windows backend dispatches through the proactor's IOCP
machinery and has no selector-loop fallback by design — see
[Windows / Supported runtimes](windows.md#supported-runtimes).

**Fix:** remove the override, or restore the proactor policy:

```python
import asyncio

asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
```

### `PortBusyError` on Windows (`ERROR_ACCESS_DENIED` / `ERROR_SHARING_VIOLATION`)

```text
PortBusyError: [WinError 5] Access is denied: 'COM3'
```

Another process holds the port open. Windows COM ports are **always**
opened with `dwShareMode=0`; there is no way to share a serial HANDLE.
There's no `lsof` equivalent built in, so:

**Diagnose** with Sysinternals `handle.exe`:

```text
handle.exe -a COM3
```

or via Process Explorer (**Find → Find Handle or DLL…**, search for
`COM3`). Resource Monitor's "Overview → CPU → Associated Handles"
panel also works if `handle.exe` isn't installed.

Common culprits: PuTTY / Tera Term / Arduino IDE / a previous Python
process that crashed mid-I/O without closing. Kill the holder or wait
for it to exit, then retry.

### `PortNotFoundError` on Windows

```text
PortNotFoundError: [WinError 2] The system cannot find the file specified: 'COM10'
```

Two common causes:

1. **The adapter isn't plugged in or the driver failed to attach.**
   Check **Device Manager → Ports (COM & LPT)**. If the adapter shows
   up with a yellow warning icon, the driver didn't load — reinstall
   the VCP driver from the chipset vendor (FTDI / Prolific /
   Silicon Labs / WCH).

2. **COM port number >= 10 without the `\\.\` prefix.** `"COM10"`
   without the Win32 namespace prefix silently opens a file in the
   current directory. Always use the prefix:

   ```python
   await open_serial_port(r"\\.\COM10")  # not "COM10"
   ```

   `COM1`–`COM9` work either way, but the prefix is safe on all
   numbers.

### Bytes arrive in 16 ms chunks (Windows / FTDI)

FTDI's VCP driver defaults its per-adapter latency timer to 16 ms.
Unlike Linux's `ASYNC_LOW_LATENCY`, **there is no programmatic API**
on Windows — the setting lives in the driver GUI.

**Fix:** Device Manager → your FTDI port → right-click **Properties**
→ **Port Settings** tab → **Advanced…** button → **Latency Timer
(msec)** → change `16` to `1` → OK → reboot the port (unplug / replug
is fine). Most request/response protocols want 1–2 ms; `anyserial`
does not override the setting.

See [Windows / Driver-specific notes](windows.md#driver-specific-notes).

### `UnsupportedConfigurationError` on Windows (custom baud)

Some USB-serial clones (CH340, off-brand PL2303) ignore non-standard
baud rates and return `ERROR_INVALID_PARAMETER` (87) on `SetCommState`.
`anyserial` surfaces that as `UnsupportedConfigurationError` with the
Win32 code stashed on the exception:

```python
try:
    port = await open_serial_port(r"\\.\COM3", SerialConfig(baudrate=500_000))
except UnsupportedConfigurationError as exc:
    print(exc.winerror)   # 87 — driver rejected the rate
```

**Fix:** pick a standard rate (9600, 19200, 115200, 230400, 460800,
921600), or use a genuine FTDI / Silicon Labs chip.

## Still stuck?

File an issue at
<https://github.com/GraysonBellamy/anyserial/issues> with:

- Platform (`sys.platform`, kernel / Windows version).
- Adapter chipset and driver name:
  - Linux: `lsusb`, `dmesg | grep ttyUSB`.
  - macOS: **About This Mac → System Report → USB**.
  - Windows: **Device Manager → port → Properties → Driver** tab.
- The `SerialConfig` you passed and the full traceback.
- On Windows, the `.winerror` code from the exception (if any).
- Whether the same action works under `pySerial` / `stty` / `minicom`
  (POSIX) or `pySerial` / PuTTY (Windows) — helps separate driver
  bugs from library bugs.
