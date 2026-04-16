# pyright: reportPrivateUsage=false
"""Unit tests for :class:`LinuxBackend`'s low-latency wiring.

Exercises the policy paths (RAISE / WARN / IGNORE) without touching a
real serial device by monkeypatching the ``_linux.low_latency`` helpers
the backend calls into. The save/restore lifecycle and the rollback
behaviour on partial failure are the things worth proving here — the
underlying ioctl plumbing has its own coverage in
:mod:`tests.unit.test_linux_low_latency`.

The reach into ``_fd`` / ``_enable_low_latency`` / ``_restore_low_latency``
is deliberate: this is an isolated white-box test of the policy
plumbing. The integration test file pairs it from the public surface.
"""

from __future__ import annotations

import errno
import sys
import warnings
from typing import TYPE_CHECKING

import pytest

if not sys.platform.startswith("linux"):
    pytest.skip("Linux-only", allow_module_level=True)

from anyserial._linux import backend as backend_mod
from anyserial._linux.backend import LinuxBackend
from anyserial._linux.low_latency import FtdiLatencyTimer
from anyserial._types import UnsupportedPolicy
from anyserial.exceptions import UnsupportedFeatureError

if TYPE_CHECKING:
    from pathlib import Path


_FAKE_FD = 99
_FAKE_PATH = "/dev/ttyUSB0"


def _make_backend() -> LinuxBackend:
    """Return a ``LinuxBackend`` with fake fd/path so the apply hooks can run.

    Bypasses :meth:`LinuxBackend.open` entirely: the apply path only
    reads ``self._fd`` and ``self._path``, so we can hand it any sentinel
    fd and the patched helpers will accept it without ever issuing a
    real ``ioctl``.
    """
    backend = LinuxBackend()
    backend._fd = _FAKE_FD
    backend._path = _FAKE_PATH
    return backend


@pytest.fixture
def captured_calls(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[object]]:
    """Track restore calls so tests can verify save/restore symmetry."""
    calls: dict[str, list[object]] = {"restore_serial_flags": [], "restore_ftdi": []}

    def fake_restore_flags(_fd: int, value: int) -> None:
        calls["restore_serial_flags"].append(value)

    def fake_restore_ftdi(saved: FtdiLatencyTimer) -> None:
        calls["restore_ftdi"].append(saved.original_ms)

    monkeypatch.setattr(backend_mod, "restore_serial_flags", fake_restore_flags)
    monkeypatch.setattr(backend_mod, "restore_ftdi_latency_timer", fake_restore_ftdi)
    return calls


def _patch_apply_helpers(
    monkeypatch: pytest.MonkeyPatch,
    *,
    enable_result: int | OSError = 0x100,
    ftdi_result: FtdiLatencyTimer | OSError | None = None,
) -> None:
    """Install fake apply helpers on the backend module.

    Each result may be a value (returned) or an ``OSError`` (raised),
    matching the surface the real helpers expose.
    """

    def fake_enable(_fd: int) -> int:
        if isinstance(enable_result, OSError):
            raise enable_result
        return enable_result

    def fake_tune(_path: str) -> FtdiLatencyTimer | None:
        if isinstance(ftdi_result, OSError):
            raise ftdi_result
        return ftdi_result

    monkeypatch.setattr(backend_mod, "enable_low_latency", fake_enable)
    monkeypatch.setattr(backend_mod, "tune_ftdi_latency_timer", fake_tune)


class TestApplySuccess:
    def test_saves_async_flags_and_ftdi_timer(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        timer = FtdiLatencyTimer(path=tmp_path / "latency_timer", original_ms=16)
        _patch_apply_helpers(monkeypatch, enable_result=0x100, ftdi_result=timer)
        be = _make_backend()
        be._enable_low_latency(UnsupportedPolicy.RAISE)
        assert be._saved_async_flags == 0x100
        assert be._ftdi_timer is timer

    def test_no_ftdi_state_on_non_ftdi_device(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _patch_apply_helpers(monkeypatch, enable_result=0x0, ftdi_result=None)
        be = _make_backend()
        be._enable_low_latency(UnsupportedPolicy.RAISE)
        assert be._saved_async_flags == 0x0
        assert be._ftdi_timer is None


class TestPolicyRaise:
    def test_async_low_latency_failure_raises(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _patch_apply_helpers(
            monkeypatch,
            enable_result=OSError(errno.ENOTTY, "Inappropriate ioctl for device"),
            ftdi_result=None,
        )
        be = _make_backend()
        with pytest.raises(UnsupportedFeatureError, match="ASYNC_LOW_LATENCY"):
            be._enable_low_latency(UnsupportedPolicy.RAISE)
        assert be._saved_async_flags is None

    def test_ftdi_failure_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_apply_helpers(
            monkeypatch,
            enable_result=0x100,
            ftdi_result=OSError(errno.EACCES, "Permission denied"),
        )
        be = _make_backend()
        with pytest.raises(UnsupportedFeatureError, match="latency_timer"):
            be._enable_low_latency(UnsupportedPolicy.RAISE)


class TestPolicyWarn:
    def test_async_low_latency_failure_warns_and_continues(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _patch_apply_helpers(
            monkeypatch,
            enable_result=OSError(errno.ENOTTY, "Inappropriate ioctl for device"),
            ftdi_result=None,
        )
        be = _make_backend()
        with pytest.warns(RuntimeWarning, match="ASYNC_LOW_LATENCY"):
            be._enable_low_latency(UnsupportedPolicy.WARN)
        assert be._saved_async_flags is None
        # FTDI tune still ran (returned None) — proves we didn't bail.
        assert be._ftdi_timer is None


class TestPolicyIgnore:
    def test_silently_skips_unsupported(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _patch_apply_helpers(
            monkeypatch,
            enable_result=OSError(errno.ENOTTY, "Inappropriate ioctl for device"),
            ftdi_result=None,
        )
        be = _make_backend()
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            be._enable_low_latency(UnsupportedPolicy.IGNORE)
        assert be._saved_async_flags is None


class TestRestore:
    def test_restores_async_flags_when_saved(
        self,
        monkeypatch: pytest.MonkeyPatch,
        captured_calls: dict[str, list[object]],
    ) -> None:
        _patch_apply_helpers(monkeypatch, enable_result=0x100, ftdi_result=None)
        be = _make_backend()
        be._enable_low_latency(UnsupportedPolicy.RAISE)
        be._restore_low_latency(be._fd)
        assert captured_calls["restore_serial_flags"] == [0x100]
        # Saved state cleared so a re-open starts fresh.
        assert be._saved_async_flags is None

    def test_restores_ftdi_timer_when_saved(
        self,
        monkeypatch: pytest.MonkeyPatch,
        captured_calls: dict[str, list[object]],
        tmp_path: Path,
    ) -> None:
        timer = FtdiLatencyTimer(path=tmp_path / "latency_timer", original_ms=16)
        _patch_apply_helpers(monkeypatch, enable_result=0x100, ftdi_result=timer)
        be = _make_backend()
        be._enable_low_latency(UnsupportedPolicy.RAISE)
        be._restore_low_latency(be._fd)
        assert captured_calls["restore_ftdi"] == [16]
        assert be._ftdi_timer is None

    def test_no_op_when_nothing_saved(
        self,
        captured_calls: dict[str, list[object]],
    ) -> None:
        be = _make_backend()
        be._restore_low_latency(be._fd)
        assert captured_calls["restore_serial_flags"] == []
        assert captured_calls["restore_ftdi"] == []

    def test_swallows_oserror_during_restore(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def boom(_fd: int, _value: int) -> None:
            raise OSError(errno.EIO, "device gone")

        monkeypatch.setattr(backend_mod, "restore_serial_flags", boom)
        be = _make_backend()
        be._saved_async_flags = 0x100
        # Must not propagate — close() cannot raise without leaking the fd.
        be._restore_low_latency(be._fd)
        assert be._saved_async_flags is None
