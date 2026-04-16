"""Unit tests for :mod:`anyserial._linux.low_latency`.

Two surfaces are exercised: the ``TIOCGSERIAL``/``TIOCSSERIAL`` round-trip
(monkeypatched ``fcntl.ioctl`` so the tests run without a real serial
device) and the FTDI sysfs detection (driven by a tmp_path tree shaped
like ``/sys/class/tty``).
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

import pytest

if not sys.platform.startswith("linux"):
    pytest.skip("Linux-only", allow_module_level=True)

import array
import errno
from typing import cast

from anyserial._linux import low_latency

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


_ASYNC_LOW_LATENCY = low_latency.ASYNC_LOW_LATENCY
_FLAGS_INDEX = 4
# Sentinel non-zero values for fields the kernel actually cares about — the
# fake ioctl returns these so we can prove the round-trip preserves them.
_FAKE_IRQ = 4
_FAKE_BAUD_BASE = 115_200


class _FakeIoctl:
    """Captures every ``fcntl.ioctl`` call and emulates ``TIOCGSERIAL``.

    ``TIOCGSERIAL`` writes a recognisable pattern into the user buffer
    (irq + baud_base + the saved flags) so write-back tests can assert
    that only the flags slot changed. ``TIOCSSERIAL`` snapshots the
    payload for assertions.
    """

    def __init__(self, *, initial_flags: int) -> None:
        self.flags = initial_flags
        self.calls: list[tuple[int, int, int]] = []
        self.last_set_payload: array.array[int] | None = None

    def __call__(self, fd: int, request: int, arg: object) -> object:
        if request == low_latency.TIOCGSERIAL:
            assert isinstance(arg, array.array)
            buf = cast("array.array[int]", arg)
            for index in range(len(buf)):
                buf[index] = 0
            buf[3] = _FAKE_IRQ
            buf[7] = _FAKE_BAUD_BASE
            buf[_FLAGS_INDEX] = self.flags
            self.calls.append((fd, request, self.flags))
            return buf
        if request == low_latency.TIOCSSERIAL:
            assert isinstance(arg, array.array)
            buf = cast("array.array[int]", arg)
            self.last_set_payload = array.array("i", list(buf))
            self.flags = int(buf[_FLAGS_INDEX])
            self.calls.append((fd, request, self.flags))
            return 0
        msg = f"unexpected ioctl request {request:#x}"
        raise AssertionError(msg)


@pytest.fixture
def fake_ioctl(monkeypatch: pytest.MonkeyPatch) -> Iterator[_FakeIoctl]:
    """Install a :class:`_FakeIoctl` over ``fcntl.ioctl`` in the module."""
    fake = _FakeIoctl(initial_flags=0)
    monkeypatch.setattr("anyserial._linux.low_latency.fcntl.ioctl", fake)
    yield fake


class TestSerialFlagsRoundTrip:
    def test_read_returns_flags_field(self, fake_ioctl: _FakeIoctl) -> None:
        fake_ioctl.flags = 0x1234
        assert low_latency.read_serial_flags(fd=99) == 0x1234

    def test_write_preserves_other_fields(self, fake_ioctl: _FakeIoctl) -> None:
        fake_ioctl.flags = 0x10
        low_latency.write_serial_flags(fd=99, flags=0xABCD)
        payload = fake_ioctl.last_set_payload
        assert payload is not None
        assert payload[_FLAGS_INDEX] == 0xABCD
        # Every other slot must round-trip — irq + baud_base in particular,
        # since some drivers key off them.
        assert payload[3] == _FAKE_IRQ
        assert payload[7] == _FAKE_BAUD_BASE


class TestEnableLowLatency:
    def test_sets_async_low_latency_bit(self, fake_ioctl: _FakeIoctl) -> None:
        fake_ioctl.flags = 0x100
        original = low_latency.enable_low_latency(fd=99)
        assert original == 0x100
        assert fake_ioctl.flags == 0x100 | _ASYNC_LOW_LATENCY

    def test_idempotent_when_bit_already_set(self, fake_ioctl: _FakeIoctl) -> None:
        fake_ioctl.flags = _ASYNC_LOW_LATENCY | 0x40
        original = low_latency.enable_low_latency(fd=99)
        assert original == _ASYNC_LOW_LATENCY | 0x40
        # Idempotency means no TIOCSSERIAL was issued.
        assert all(req != low_latency.TIOCSSERIAL for _, req, _ in fake_ioctl.calls)

    def test_propagates_oserror(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(*_args: object, **_kwargs: object) -> None:
            raise OSError(errno.ENOTTY, "Inappropriate ioctl for device")

        monkeypatch.setattr("anyserial._linux.low_latency.fcntl.ioctl", boom)
        with pytest.raises(OSError, match="Inappropriate ioctl"):
            low_latency.enable_low_latency(fd=99)

    def test_restore_writes_original_value(self, fake_ioctl: _FakeIoctl) -> None:
        fake_ioctl.flags = 0x100
        low_latency.enable_low_latency(fd=99)
        low_latency.restore_serial_flags(fd=99, original=0x100)
        assert fake_ioctl.flags == 0x100


# ---------------------------------------------------------------------------
# FTDI sysfs detection
# ---------------------------------------------------------------------------


def _build_sysfs_tty(
    root: Path,
    *,
    tty_name: str,
    driver_name: str | None,
    latency_timer_value: str | None,
) -> Path:
    """Construct a tmp_path tree shaped like ``/sys/class/tty``.

    Returns the sysfs root the test should pass to
    :func:`ftdi_latency_timer_path`. ``driver_name=None`` skips the
    driver symlink entirely (mirrors a tty with no driver in sysfs).
    ``latency_timer_value=None`` skips the latency_timer file (mirrors a
    non-FTDI driver or older kernel).
    """
    sysfs_root = root / "sys" / "class" / "tty"
    tty_dir = sysfs_root / tty_name
    device_dir = tty_dir / "device"
    device_dir.mkdir(parents=True)
    if driver_name is not None:
        driver_target = root / "sys" / "bus" / "usb-serial" / "drivers" / driver_name
        driver_target.mkdir(parents=True)
        (device_dir / "driver").symlink_to(driver_target)
    if latency_timer_value is not None:
        (device_dir / "latency_timer").write_text(latency_timer_value)
    return sysfs_root


class TestFtdiLatencyTimerPath:
    def test_returns_path_for_ftdi_driver(self, tmp_path: Path) -> None:
        sysfs = _build_sysfs_tty(
            tmp_path,
            tty_name="ttyUSB0",
            driver_name="ftdi_sio",
            latency_timer_value="16\n",
        )
        path = low_latency.ftdi_latency_timer_path("/dev/ttyUSB0", sysfs_root=sysfs)
        assert path is not None
        assert path.name == "latency_timer"
        assert path.read_text().strip() == "16"

    def test_returns_none_for_non_ftdi_driver(self, tmp_path: Path) -> None:
        sysfs = _build_sysfs_tty(
            tmp_path,
            tty_name="ttyUSB0",
            driver_name="cp210x",
            latency_timer_value="16\n",
        )
        assert low_latency.ftdi_latency_timer_path("/dev/ttyUSB0", sysfs_root=sysfs) is None

    def test_returns_none_when_driver_link_missing(self, tmp_path: Path) -> None:
        sysfs = _build_sysfs_tty(
            tmp_path,
            tty_name="ttyS0",
            driver_name=None,
            latency_timer_value=None,
        )
        assert low_latency.ftdi_latency_timer_path("/dev/ttyS0", sysfs_root=sysfs) is None

    def test_returns_none_when_latency_timer_absent(self, tmp_path: Path) -> None:
        sysfs = _build_sysfs_tty(
            tmp_path,
            tty_name="ttyUSB0",
            driver_name="ftdi_sio",
            latency_timer_value=None,
        )
        assert low_latency.ftdi_latency_timer_path("/dev/ttyUSB0", sysfs_root=sysfs) is None

    def test_returns_none_when_tty_missing(self, tmp_path: Path) -> None:
        # Empty sysfs root — nothing exists for ttyUSB0 at all.
        sysfs = tmp_path / "sys" / "class" / "tty"
        sysfs.mkdir(parents=True)
        assert low_latency.ftdi_latency_timer_path("/dev/ttyUSB0", sysfs_root=sysfs) is None


class TestTuneFtdiLatencyTimer:
    def test_drops_to_one_ms_and_returns_saved(self, tmp_path: Path) -> None:
        sysfs = _build_sysfs_tty(
            tmp_path,
            tty_name="ttyUSB0",
            driver_name="ftdi_sio",
            latency_timer_value="16\n",
        )
        saved = low_latency.tune_ftdi_latency_timer("/dev/ttyUSB0", sysfs_root=sysfs)
        assert saved is not None
        assert saved.original_ms == 16
        assert saved.path.read_text().strip() == "1"

    def test_returns_none_for_non_ftdi(self, tmp_path: Path) -> None:
        sysfs = _build_sysfs_tty(
            tmp_path,
            tty_name="ttyUSB0",
            driver_name="cp210x",
            latency_timer_value="16\n",
        )
        assert low_latency.tune_ftdi_latency_timer("/dev/ttyUSB0", sysfs_root=sysfs) is None

    def test_idempotent_when_already_one(self, tmp_path: Path) -> None:
        sysfs = _build_sysfs_tty(
            tmp_path,
            tty_name="ttyUSB0",
            driver_name="ftdi_sio",
            latency_timer_value="1\n",
        )
        saved = low_latency.tune_ftdi_latency_timer("/dev/ttyUSB0", sysfs_root=sysfs)
        assert saved is not None
        assert saved.original_ms == 1
        # Restoring to the original is a no-op write; verify the value
        # is still 1 after a round-trip.
        low_latency.restore_ftdi_latency_timer(saved)
        assert saved.path.read_text().strip() == "1"

    def test_restore_writes_original(self, tmp_path: Path) -> None:
        sysfs = _build_sysfs_tty(
            tmp_path,
            tty_name="ttyUSB0",
            driver_name="ftdi_sio",
            latency_timer_value="16\n",
        )
        saved = low_latency.tune_ftdi_latency_timer("/dev/ttyUSB0", sysfs_root=sysfs)
        assert saved is not None
        low_latency.restore_ftdi_latency_timer(saved)
        assert saved.path.read_text().strip() == "16"
