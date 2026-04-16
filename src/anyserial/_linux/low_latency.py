"""Linux low-latency mode: ``ASYNC_LOW_LATENCY`` + FTDI sysfs latency_timer.

Two independent knobs share this module because they're always tuned
together: ``ASYNC_LOW_LATENCY`` tells the kernel's tty layer to push
incoming bytes to userspace as soon as they arrive (skipping the small
batching window that exists for line-discipline efficiency), and the
FTDI ``latency_timer`` does the same on the USB-adapter side (default
16 ms; we drop it to 1 ms). Without both, USB-FTDI ports keep their
worst-case ~16 ms read latency even with the kernel knob enabled.

Both paths are best-effort. Many tty drivers (pty, virtual ports, some
USB adapters) reject :data:`TIOCSSERIAL` with ``ENOTTY`` or ``EINVAL``,
and the FTDI sysfs file only exists when the ``ftdi_sio`` kernel module
owns the device. Callers route ``OSError`` through
:class:`UnsupportedPolicy` to decide whether the rejection raises,
warns, or is silently ignored.

The save/restore discipline is non-negotiable per DESIGN §18.1: leaving
the device with our settings after close would surprise the next process
to open it. :class:`LinuxBackend` records originals at open time and
puts them back on close, even under cancellation, via the shielded
teardown in :meth:`SerialPort.aclose`.
"""

from __future__ import annotations

import array
import fcntl
from dataclasses import dataclass
from pathlib import Path
from typing import Final

# Stable kernel ABI from <asm-generic/ioctls.h>. Python's stdlib termios
# does not expose either constant.
TIOCGSERIAL: Final[int] = 0x541E
TIOCSSERIAL: Final[int] = 0x541F

# From <linux/tty_flags.h>. The other bits of struct serial_struct.flags
# (e.g. ASYNC_SPD_HI, ASYNC_SAK) round-trip untouched — we only OR our
# bit in and rely on the read-modify-write pattern to leave them alone.
ASYNC_LOW_LATENCY: Final[int] = 0x2000

# struct serial_struct's `flags` field has lived at offset 16 (= int
# index 4) since the very first kernel revision. The struct itself has
# grown over the years; we treat the read buffer as an opaque blob and
# only touch this slot.
_FLAGS_INDEX: Final[int] = 4

# Generous int-array slot count. The kernel reads/writes exactly
# sizeof(struct serial_struct) bytes from the user buffer regardless of
# its declared size; modern kernels top out around 18 ints on 64-bit
# (72 bytes), so 32 ints (128 bytes) leaves comfortable headroom for
# any future field additions.
_BUF_INTS: Final[int] = 32


def _new_buffer() -> array.array[int]:
    """Allocate the int-array buffer used for ``TIOCGSERIAL``/``TIOCSSERIAL``."""
    return array.array("i", [0] * _BUF_INTS)


def read_serial_flags(fd: int) -> int:
    """Return ``struct serial_struct.flags`` for ``fd`` via ``TIOCGSERIAL``.

    Lets :class:`OSError` (typically ``ENOTTY`` on drivers that don't
    implement the ioctl) escape unchanged; the orchestrator decides
    whether to honour :class:`UnsupportedPolicy`.
    """
    buf = _new_buffer()
    fcntl.ioctl(fd, TIOCGSERIAL, buf)
    return int(buf[_FLAGS_INDEX])


def write_serial_flags(fd: int, flags: int) -> None:
    """Write ``flags`` into ``struct serial_struct`` via ``TIOCSSERIAL``.

    Reads the current struct first and overwrites only the flags slot so
    every other field (irq, baud_base, close_delay, …) round-trips
    byte-for-byte. Zeroing them out has been observed to break drivers
    that key off the IRQ field for kernel bookkeeping.
    """
    buf = _new_buffer()
    fcntl.ioctl(fd, TIOCGSERIAL, buf)
    buf[_FLAGS_INDEX] = flags
    fcntl.ioctl(fd, TIOCSSERIAL, buf)


def enable_low_latency(fd: int) -> int:
    """Set ``ASYNC_LOW_LATENCY`` on ``fd`` and return the original flags.

    Returning the originals lets the caller restore them on close so the
    next process opening the device inherits the kernel's default tty
    batching, not ours. Idempotent: if the bit is already set, no
    ``TIOCSSERIAL`` is issued.
    """
    original = read_serial_flags(fd)
    if original & ASYNC_LOW_LATENCY:
        return original
    write_serial_flags(fd, original | ASYNC_LOW_LATENCY)
    return original


def restore_serial_flags(fd: int, original: int) -> None:
    """Write ``original`` back into ``struct serial_struct.flags``."""
    write_serial_flags(fd, original)


# ---------------------------------------------------------------------------
# FTDI latency-timer (sysfs)
# ---------------------------------------------------------------------------

_SYS_CLASS_TTY: Final[Path] = Path("/sys/class/tty")
_FTDI_DRIVER_NAME: Final[str] = "ftdi_sio"
_FTDI_LATENCY_TIMER_TARGET_MS: Final[int] = 1


@dataclass(frozen=True, slots=True, kw_only=True)
class FtdiLatencyTimer:
    """Saved FTDI latency-timer state for restore-on-close.

    Attributes:
        path: Sysfs file the value was read from / will be written to.
        original_ms: Value read at open time, in milliseconds.
    """

    path: Path
    original_ms: int


def _tty_name(device_path: str) -> str:
    """Return the basename of ``/dev/ttyXXX`` — the sysfs entry name."""
    return Path(device_path).name


def ftdi_latency_timer_path(
    device_path: str,
    *,
    sysfs_root: Path = _SYS_CLASS_TTY,
) -> Path | None:
    """Return the sysfs ``latency_timer`` path for ``device_path``.

    Returns ``None`` when the tty does not exist in sysfs, the driver
    isn't ``ftdi_sio``, or the ``latency_timer`` file is absent (older
    kernels, virtual ports, non-FTDI USB adapters). ``sysfs_root`` is
    parametrised purely so unit tests can point at a tmp_path tree.
    """
    name = _tty_name(device_path)
    driver_link = sysfs_root / name / "device" / "driver"
    try:
        driver_target = driver_link.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if driver_target.name != _FTDI_DRIVER_NAME:
        return None
    timer_path = sysfs_root / name / "device" / "latency_timer"
    if not timer_path.exists():
        return None
    return timer_path


def read_latency_timer(path: Path) -> int:
    """Read the FTDI ``latency_timer`` value (milliseconds)."""
    return int(path.read_text().strip())


def write_latency_timer(path: Path, value_ms: int) -> None:
    """Write ``value_ms`` to the FTDI ``latency_timer``.

    Writing the sysfs file requires either CAP_DAC_OVERRIDE or membership
    in the group that owns it (commonly ``dialout``); permission failures
    surface as ``OSError(EACCES)`` for the caller's policy layer to map.
    """
    path.write_text(f"{value_ms}\n")


def tune_ftdi_latency_timer(
    device_path: str,
    *,
    sysfs_root: Path = _SYS_CLASS_TTY,
) -> FtdiLatencyTimer | None:
    """Drop the FTDI ``latency_timer`` to 1 ms and return the saved original.

    Returns ``None`` when the device is not an FTDI port — that is the
    expected outcome on every other USB-serial adapter and on motherboard
    UARTs, and is not an error. ``OSError`` from the read/write itself
    propagates so the caller can apply :class:`UnsupportedPolicy`.
    """
    path = ftdi_latency_timer_path(device_path, sysfs_root=sysfs_root)
    if path is None:
        return None
    original = read_latency_timer(path)
    saved = FtdiLatencyTimer(path=path, original_ms=original)
    if original != _FTDI_LATENCY_TIMER_TARGET_MS:
        write_latency_timer(path, _FTDI_LATENCY_TIMER_TARGET_MS)
    return saved


def restore_ftdi_latency_timer(saved: FtdiLatencyTimer) -> None:
    """Restore the FTDI ``latency_timer`` to its pre-open value."""
    write_latency_timer(saved.path, saved.original_ms)


__all__ = [
    "ASYNC_LOW_LATENCY",
    "TIOCGSERIAL",
    "TIOCSSERIAL",
    "FtdiLatencyTimer",
    "enable_low_latency",
    "ftdi_latency_timer_path",
    "read_latency_timer",
    "read_serial_flags",
    "restore_ftdi_latency_timer",
    "restore_serial_flags",
    "tune_ftdi_latency_timer",
    "write_latency_timer",
    "write_serial_flags",
]
