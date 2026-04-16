# Linux tuning

Linux is `anyserial`'s first-class target. Most of the measurable
latency on a USB-serial round-trip is driver or kernel behaviour
rather than user-space overhead — this page collects the knobs that
move real numbers.

See [DESIGN §18](https://github.com/GraysonBellamy/anyserial/blob/main/DESIGN.md#18-low-latency-design)
for the full low-latency strategy.

## Permissions

Opening a serial device requires read/write on the node. Distributions
usually ship with the nodes owned by `root:dialout` (Debian / Ubuntu)
or `root:uucp` (Arch / Fedora):

```bash
ls -l /dev/ttyUSB0
# crw-rw---- 1 root dialout 188, 0 Apr 15 10:03 /dev/ttyUSB0
```

Add your user to the group once:

```bash
# Debian / Ubuntu
sudo usermod -aG dialout "$USER"
# Arch / Fedora
sudo usermod -aG uucp "$USER"
```

Log out and back in so the new group membership takes effect. A
permission failure from `open_serial_port` surfaces as
`PortBusyError` (an `OSError` with `errno == EACCES`); see
[Troubleshooting](troubleshooting.md#permission-denied-on-dev-ttyxxx).

## Low-latency mode

`SerialConfig(low_latency=True)` flips two knobs in tandem:

- **Kernel**: `ASYNC_LOW_LATENCY` via `TIOCSSERIAL`. Tells the tty
  layer to push bytes to userspace immediately instead of batching
  for line-discipline efficiency.
- **FTDI sysfs**: `/sys/class/tty/<name>/device/latency_timer` is
  lowered from its 16 ms default to 1 ms, so the adapter stops
  batching on its own side too.

```python
from anyserial import SerialConfig, open_serial_port

async with await open_serial_port(
    "/dev/ttyUSB0",
    SerialConfig(baudrate=115_200, low_latency=True),
) as port:
    ...
```

Both knobs are saved and restored on close — the next process to open
the device gets the kernel default, not `anyserial`'s tuning.

Restrictions:

- Writing `latency_timer` requires write access to the sysfs file.
  Usually the same group that owns `/dev/ttyUSB0` owns the sysfs
  entry; if not, a `udev` rule fixes it (see
  [below](#udev-rules)).
- Non-FTDI adapters skip the sysfs step silently — there is no
  equivalent knob for CP210x / CH340 / PL2303.
- Pseudo terminals return `ENOTTY` for `TIOCSSERIAL`. Tests that want
  to exercise `low_latency=True` on a pty need to route through
  `UnsupportedPolicy.IGNORE` or skip the check.

Rejection routes through [`unsupported_policy`](configuration.md#unsupported-feature-policy):
the default `RAISE` errors out if either knob fails; `WARN` and
`IGNORE` apply the rest of the config and keep going.

## Custom baud

Linux accepts any integer baud rate via `TCSETS2` / `BOTHER`. The
kernel divisor has to resolve cleanly on the adapter's UART clock
for the rate to actually appear on the wire, but from the
application's perspective it's a single ioctl:

```python
from anyserial import SerialConfig, open_serial_port

async with await open_serial_port(
    "/dev/ttyUSB0",
    SerialConfig(baudrate=921_600),
) as port:
    ...
```

Driver-level rejection (adapter can't synthesize the rate) surfaces
as `UnsupportedConfigurationError`. The
`SerialCapabilities.custom_baudrate` field reads `SUPPORTED` because
the *platform* has the mechanism — see
[Capabilities](capabilities.md#supported-doesnt-guarantee-the-specific-request-works)
for why "supported" isn't a guarantee per device.

## udev rules

Two scenarios where a `udev` rule helps:

**1. Stable device names.** USB-serial adapters land at whichever
free `ttyUSB*` slot is open at plug time — which changes when you
reboot with a different number of adapters attached. A `SYMLINK=`
rule anchors a stable name to the serial number:

```udev
# /etc/udev/rules.d/60-anyserial-ftdi.rules
SUBSYSTEM=="tty", ATTRS{idVendor}=="0403", ATTRS{idProduct}=="6001", \
    ATTRS{serial}=="A12345BC", SYMLINK+="anyserial-ftdi"
```

Reload and re-plug:

```bash
sudo udevadm control --reload
sudo udevadm trigger
```

Then open `/dev/anyserial-ftdi` instead of `/dev/ttyUSB0`.

**2. Group ownership of sysfs.** If `latency_timer` writes fail with
`EACCES`, widen the sysfs ownership:

```udev
# Same file; add another rule or another clause to the existing one.
SUBSYSTEM=="usb-serial", DRIVER=="ftdi_sio", \
    RUN+="/bin/sh -c 'chgrp dialout /sys%p/latency_timer; chmod g+w /sys%p/latency_timer'"
```

Check [Discovery](discovery.md#backends) for the `pyudev` fallback
that surfaces udev database attributes (`ID_PATH`, hwdb manufacturer
strings) your raw sysfs walk won't see.

## Exclusive access

`SerialConfig(exclusive=True)` acquires `flock(LOCK_EX | LOCK_NB)` on
the fd. A second opener — yours, a stale shell, `gtkterm` — gets
`PortBusyError` instead of a silently-shared port that drops bytes
between the two readers:

```python
SerialConfig(baudrate=115_200, exclusive=True)
```

Released automatically at close. Does nothing if the driver ignores
`flock`; the kernel pty does.

## FTDI `latency_timer` explained

The chip buffers incoming bytes for up to `latency_timer` ms before
shipping them upstream; the USB host-controller driver can't
round-trip faster than the chip. 16 ms is a sensible default for
line-oriented serial consoles — awful for a 2 ms Modbus request /
response cycle.

Dropping it to 1 ms removes ~15 ms from the per-exchange floor. See
[Performance](performance.md#single-byte-latency-115-200-baud-pty)
for the measured impact on pty round-trips (pty has no chip, so the
test suite exercises the `ASYNC_LOW_LATENCY` half; hardware numbers
on an FTDI adapter land when a self-hosted runner is wired up).

## Process-level knobs

Not managed by `anyserial`, but worth knowing:

- **CPU governor.** `cpupower frequency-set -g performance` removes
  ondemand / schedutil latency spikes.
- **Realtime priority.** `chrt -r 50 python my_app.py` gives the
  event loop enough priority to survive a general-purpose workload
  on the same host.
- **Scheduler pinning.** `taskset -c 1,2 python my_app.py` keeps the
  event loop off of CPU 0 where most interrupts land.

Only reach for these when you've shown the floor sits above your
budget on an otherwise-quiet machine. `uvloop` + `low_latency=True`
is typically enough.

## Seeing what the kernel thinks

```bash
# Current termios state.
stty -F /dev/ttyUSB0 -a

# ASYNC_LOW_LATENCY bit and friends (root may be required).
sudo setserial -g /dev/ttyUSB0

# FTDI latency timer, in ms.
cat /sys/class/tty/ttyUSB0/device/latency_timer

# Which driver owns the device.
readlink /sys/class/tty/ttyUSB0/device/driver
```

These are read-only diagnostics — no risk to a running application.

## See also

- [Configuration](configuration.md) — every `SerialConfig` field.
- [Performance](performance.md) — measured numbers on Linux.
- [Troubleshooting](troubleshooting.md) — permission errors, EINVAL
  on baud, stale locks.
- [RS-485](rs485.md) — kernel RS-485 on Linux via `TIOCSRS485`.
