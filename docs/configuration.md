# Configuration

`SerialConfig` is an immutable, validated description of how to open
and operate a serial port. Construct one, hand it to
`open_serial_port(...)` or `SerialPort.configure(...)`, and
`anyserial` takes care of the termios / ioctl plumbing. Invalid
configurations fail fast at construction time so a bad value never
reaches the driver.

See [DESIGN §8.4](https://github.com/GraysonBellamy/anyserial/blob/main/DESIGN.md#84-serialconfig)
for the full rationale.

## Minimal config

```python
from anyserial import SerialConfig

config = SerialConfig(baudrate=115_200)
```

Every field has a sensible default; the minimum you ever need to
supply is the baud rate.

## Every field

| Field | Type | Default | Meaning |
|---|---|---|---|
| `baudrate` | `int` | `115_200` | Bits per second. Any positive integer; driver may reject non-standard rates at apply time — see [Custom baud](#custom-baud). |
| `byte_size` | `ByteSize` | `EIGHT` | Data bits per character. `FIVE` / `SIX` / `SEVEN` / `EIGHT`. |
| `parity` | `Parity` | `NONE` | `NONE` / `ODD` / `EVEN` / `MARK` / `SPACE`. |
| `stop_bits` | `StopBits` | `ONE` | `ONE` / `ONE_POINT_FIVE` / `TWO`. |
| `flow_control` | `FlowControl` | all-off | See [Flow control](#flow-control). |
| `exclusive` | `bool` | `False` | Acquire `flock(LOCK_EX \| LOCK_NB)` on the fd; second opener gets `PortBusyError`. |
| `hangup_on_close` | `bool` | `True` | Leave termios `HUPCL` on — driver drops DTR/RTS at close. |
| `low_latency` | `bool` | `False` | Linux `ASYNC_LOW_LATENCY` + FTDI `latency_timer=1`. See [Linux tuning](linux-tuning.md#low-latency-mode). |
| `read_chunk_size` | `int` | `65_536` | Scratch buffer size for `receive_available`. Range 64 .. 16 MiB. |
| `rs485` | `RS485Config \| None` | `None` | Linux-only kernel RS-485 via `TIOCSRS485`. See [RS-485](rs485.md). |
| `unsupported_policy` | `UnsupportedPolicy` | `RAISE` | What to do when an optional feature is unsupported. See [Unsupported-feature policy](#unsupported-feature-policy). |

All fields are keyword-only and the dataclass is frozen. Derive new
configs via `with_changes` (see [below](#immutability-and-with_changes)).

## Enums

Every user-facing enum is a `StrEnum`, so the string form is the
canonical value and works anywhere Python compares strings:

```python
from anyserial import ByteSize, Parity, StopBits

assert ByteSize.EIGHT == "8"
assert Parity.EVEN == "even"
assert StopBits.ONE_POINT_FIVE == "1.5"
```

## Flow control

`FlowControl` is three independent booleans — software XON/XOFF is
orthogonal to the hardware lines, and some stacks combine them:

```python
from anyserial import FlowControl, SerialConfig

SerialConfig(
    baudrate=115_200,
    flow_control=FlowControl(rts_cts=True),
)
```

| Field | Signals / bits |
|---|---|
| `xon_xoff` | Software flow; termios `IXON \| IXOFF`. |
| `rts_cts` | Hardware flow on RTS/CTS; termios `CRTSCTS` on Linux, `CCTS_OFLOW \| CRTS_IFLOW` on macOS. |
| `dtr_dsr` | Hardware flow on DTR/DSR. No portable termios bit — routed through `UnsupportedPolicy` on most backends. |

`FlowControl.none()` is a convenience for the all-off default.

## Custom baud

Any positive integer is accepted at config time. Whether the driver
honours it is platform- and device-specific:

| Platform | Mechanism | Custom baud |
|---|---|---|
| Linux | `TCSETS2` / `BOTHER` | ✅ — kernel will synthesize any integer the UART clock can reach |
| macOS | `IOSSIOSPEED` | ✅ — chip-dependent (FTDI / CP210x firmware cope; older PL2303 may reject) |
| BSD | `c_ispeed` / `c_ospeed` passthrough | ✅ |
| Windows | `DCB.BaudRate` integer | ✅ — driver decides (CH340 / off-brand PL2303 clones often reject non-standard rates) |

Driver-level rejection surfaces as `UnsupportedConfigurationError`.
The `SerialCapabilities.custom_baudrate` field is `SUPPORTED` on every
platform above, meaning "the platform has a mechanism". See
[Capabilities](capabilities.md) for why `SUPPORTED` still doesn't
guarantee a specific rate works.

## Unsupported-feature policy

Some fields request features that a platform may not have
(`low_latency` on macOS, `rs485` on BSD, `dtr_dsr` flow on most
drivers). Instead of silently ignoring or always raising,
`unsupported_policy` picks the behaviour:

```python
from anyserial import SerialConfig, UnsupportedPolicy

SerialConfig(low_latency=True, unsupported_policy=UnsupportedPolicy.RAISE)   # default
SerialConfig(low_latency=True, unsupported_policy=UnsupportedPolicy.WARN)    # RuntimeWarning
SerialConfig(low_latency=True, unsupported_policy=UnsupportedPolicy.IGNORE)  # silent
```

`RAISE` is the default because silent misconfiguration is the wrong
default for low-latency serial work. Pick `WARN` or `IGNORE` when you
want the same code to run across heterogeneous hardware where a
feature is nice-to-have, not required.

Policy applies only to **optional** features. Impossible values (zero
baud, read-chunk-size out of range) always raise `ConfigurationError`
regardless.

See [DESIGN §9.1](https://github.com/GraysonBellamy/anyserial/blob/main/DESIGN.md#91-unsupported-feature-policy)
for the resolution of the open design decision.

## Validation

`__post_init__` runs at construction:

- `baudrate` must be positive.
- `read_chunk_size` must be 64 .. 16 MiB (16 777 216).
- `RS485Config` delays must be non-negative.

Violations raise `ConfigurationError` (which is also a `ValueError`
so generic `except ValueError` catches it).

Rules that are wrong only for a specific driver — impossible
flow-control combinations, custom baud rates the device rejects,
`CMSPAR` on a port that doesn't expose it — are deferred to apply
time and raise `UnsupportedConfigurationError`.

## Immutability and `with_changes`

`SerialConfig` is a frozen, slots-based dataclass. To change a field,
derive a new config:

```python
current = port.config
updated = current.with_changes(baudrate=1_000_000, parity=Parity.EVEN)
await port.configure(updated)
```

`with_changes` runs the full `__post_init__` on the new instance, so
invalid combinations are caught before the backend sees them. See
[Runtime reconfiguration](runtime-reconfiguration.md) for the apply
semantics.

## Looking up the active config

```python
async with await open_serial_port("/dev/ttyUSB0", SerialConfig()) as port:
    assert port.config.baudrate == 115_200

    from anyserial import SerialStreamAttribute
    same = port.extra(SerialStreamAttribute.config)
```

`port.config` is updated only after `configure()` returns
successfully; a failed apply leaves the previous value visible.

## Defaults reference

Everything together, for copy/paste:

```python
from anyserial import (
    ByteSize,
    FlowControl,
    Parity,
    SerialConfig,
    StopBits,
    UnsupportedPolicy,
)

SerialConfig(
    baudrate=115_200,
    byte_size=ByteSize.EIGHT,
    parity=Parity.NONE,
    stop_bits=StopBits.ONE,
    flow_control=FlowControl(),         # xon_xoff=False, rts_cts=False, dtr_dsr=False
    exclusive=False,
    hangup_on_close=True,
    low_latency=False,
    read_chunk_size=65_536,
    rs485=None,
    unsupported_policy=UnsupportedPolicy.RAISE,
)
```
