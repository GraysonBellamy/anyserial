# Runtime reconfiguration

`SerialPort.configure()` re-applies a new `SerialConfig` to an already
open port without closing and reopening the device. It is the canonical
path for changing baud rate mid-session, switching flow control,
toggling [RS-485](rs485.md) mode, or swapping timeouts on a protocol
handshake.

See [DESIGN §8.5](https://github.com/GraysonBellamy/anyserial/blob/main/DESIGN.md#85-runtime-reconfiguration)
for the full rationale.

## Quick start

```python
import anyio
from anyserial import SerialConfig, open_serial_port


async def main() -> None:
    async with await open_serial_port("/dev/ttyUSB0", SerialConfig(baudrate=9_600)) as port:
        # Handshake at 9600…
        await port.send(b"AT+BAUD=1000000\r\n")
        _ = await port.receive(64)

        # …then switch to the negotiated speed.
        await port.configure(port.config.with_changes(baudrate=1_000_000))

        await port.send(b"fast-payload")


anyio.run(main)
```

## `with_changes`

`SerialConfig` is a frozen dataclass; derive new configs via
`with_changes`, which re-runs validation so a bad value fails fast with
`ConfigurationError` before it reaches the backend:

```python
new = port.config.with_changes(
    baudrate=115_200,
    flow_control=FlowControl(rts_cts=True),
)
await port.configure(new)
```

Every field on `SerialConfig` (including `rs485` and
`unsupported_policy`) participates in `with_changes`. Passing an
unsupported combination raises at the config layer, not inside the
driver.

## Concurrency semantics

`configure()` is serialized by an internal `anyio.Lock`. Two tasks
racing on `configure()` will observe one full apply at a time; neither
sees a torn state. The stream's public `port.config` property is only
updated after the backend call returns successfully — if the apply
raises, the previous config stays visible.

```python
# Safe: two tasks concurrently reconfiguring.
async with anyio.create_task_group() as tg:
    tg.start_soon(port.configure, config_a)
    tg.start_soon(port.configure, config_b)

# port.config is now exactly one of the two — never a mix.
```

`configure()` does **not** block in-flight reads or writes. The config
lock is independent of the send / receive resource guards, so a reader
parked in `anyio.wait_readable` stays parked while the reconfigure
runs and wakes normally when bytes arrive. This is intentional —
real-world protocols often renegotiate speed while a monitor task
continues draining.

## Failure semantics

If the backend rejects the new config (driver returns `EINVAL`, a
capability check fails, etc.), `configure()` raises a subclass of
`SerialError` and the stream's config remains at the previous value.
No half-applied state is visible to the caller.

```python
try:
    await port.configure(port.config.with_changes(baudrate=99_999_999))
except UnsupportedConfigurationError:
    # port.config is still the original, proven-good config.
    assert port.config.baudrate == 115_200
```

Termios changes apply atomically from the package's perspective on
every POSIX — the backend commits them with a single `tcsetattr`, or
the platform-specific equivalent when a custom baud rate is involved:

- **Linux** — `TCSETS2` for custom rates (one ioctl).
- **macOS** — `tcsetattr` with a placeholder baud, followed by
  `IOSSIOSPEED` for custom rates. The two-step is still apply-time
  atomic from the caller's perspective — the pair succeeds or raises
  together.
- **BSD** — `tcsetattr` with the integer rate dropped directly into
  `c_ispeed` / `c_ospeed`. One ioctl covers every case.

Partial application within a single kernel call is a driver-level
concern; `anyserial` treats each apply step as all-or-nothing.

## Cancellation

`configure()` is cancellable like every other async operation. The lock
is released on cancel and the backend call either completed or was
never started — in both cases the stream's `port.config` accurately
reflects the kernel state.

```python
with anyio.move_on_after(0.1):
    await port.configure(new_config)

# If the scope timed out, port.config is unchanged.
```

## What reconfiguration can change

Every field on [`SerialConfig`](https://github.com/GraysonBellamy/anyserial/blob/main/DESIGN.md#84-serialconfig)
is re-applicable, with two caveats:

- **`exclusive`** is honoured at open time only — the flock is acquired
  during `open_serial_port` and released on close. Changing it via
  `configure()` does nothing.
- **`hangup_on_close`** controls how the kernel handles the port when
  the last fd closes; the new value takes effect on the *next* close,
  not immediately.

Everything else — baud rate (including custom rates on every supported
platform), byte size, parity, stop bits, flow control — is
live-reconfigurable. Platform-specific restrictions on `low_latency` and
`rs485` apply to `configure()` the same way they apply to `open()`:
macOS and BSD route the request through `UnsupportedPolicy` (see
[macOS](darwin.md#low-latency-mode) and [BSD](bsd.md#low-latency-mode)),
while Linux honours both natively.
