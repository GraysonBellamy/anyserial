# pyright: reportPrivateUsage=false
"""Unit tests for :class:`BsdBackend`'s apply and rejection wiring.

Hermetic — every syscall the backend would make on a real BSD fd is
monkeypatched so the suite runs on Linux CI. Mirrors the shape of
:mod:`tests.unit.test_darwin_backend`, since both backends apply the
same "reject Linux-only features → super().open → platform-specific
custom-baud commit" pattern.

Contracts pinned here:

- Standard baud flows through the inherited ``PosixBackend`` pipeline
  (which encodes via ``B*`` constants on every POSIX).
- Non-standard baud drops the integer rate directly into
  ``c_ispeed`` / ``c_ospeed`` — no ioctl, no placeholder dance.
- ``low_latency`` and ``rs485`` route through :class:`UnsupportedPolicy`
  with synthetic messages (no OSError to wrap), matching the Darwin
  backend.
- Rejections happen *before* :meth:`PosixBackend.open` under ``RAISE``
  so the orchestrator never sees a transiently-open fd.
"""

from __future__ import annotations

import os
import termios
from typing import TYPE_CHECKING, Any

import pytest

from anyserial._bsd.backend import BsdBackend
from anyserial._bsd.capabilities import bsd_capabilities
from anyserial._types import UnsupportedPolicy
from anyserial.config import RS485Config, SerialConfig
from anyserial.exceptions import UnsupportedFeatureError

if TYPE_CHECKING:
    from collections.abc import Iterator


_FAKE_FD = 88
_FAKE_PATH = "/dev/cuaU0"


def _baseline_termios() -> list[Any]:
    """Return a 7-list in the shape :func:`termios.tcgetattr` produces.

    cc slot must be 20 slots on POSIX; content doesn't matter — the
    builders overwrite VMIN / VTIME and leave the rest untouched.
    """
    cc: list[int | bytes] = [0] * 20
    return [0, 0, termios.CS8 | termios.CREAD | termios.CLOCAL, 0, termios.B9600, termios.B9600, cc]


@pytest.fixture
def tcset_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, int, list[Any]]]:
    """Capture every ``termios.tcsetattr`` call as ``(fd, when, attrs)``."""
    calls: list[tuple[int, int, list[Any]]] = []

    def fake_tcgetattr(_fd: int) -> list[Any]:
        return _baseline_termios()

    def fake_tcsetattr(fd: int, when: int, attrs: list[Any]) -> None:
        calls.append((fd, when, attrs))

    # termios is a single global module; patching here affects both the
    # backend's module namespace and the parent PosixBackend's.
    monkeypatch.setattr(termios, "tcgetattr", fake_tcgetattr)
    monkeypatch.setattr(termios, "tcsetattr", fake_tcsetattr)
    return calls


@pytest.fixture
def backend() -> Iterator[BsdBackend]:
    """Return a :class:`BsdBackend` with a fake fd so the apply hooks run.

    Bypasses :meth:`BsdBackend.open` entirely — the apply path reads
    only ``self._fd``, and the patched termios helpers accept any
    sentinel integer without issuing real syscalls.
    """
    b = BsdBackend()
    b._fd = _FAKE_FD
    b._path = _FAKE_PATH
    try:
        yield b
    finally:
        # Prevent any lingering finalizer from trying to close the fake fd.
        b._fd = -1


class TestCapabilities:
    def test_backend_returns_bsd_snapshot(self) -> None:
        assert BsdBackend().capabilities == bsd_capabilities()


class TestApplyConfigStandardBaud:
    def test_standard_rate_uses_parent_pipeline(
        self,
        backend: BsdBackend,
        tcset_calls: list[tuple[int, int, list[Any]]],
    ) -> None:
        backend._apply_config_to_fd(_FAKE_FD, SerialConfig(baudrate=115200))
        assert len(tcset_calls) == 1
        fd, when, attrs = tcset_calls[0]
        assert fd == _FAKE_FD
        assert when == termios.TCSANOW
        # Parent path: speed slots carry the real B115200 constant.
        assert attrs[4] == termios.B115200
        assert attrs[5] == termios.B115200


class TestApplyConfigCustomBaud:
    def test_custom_rate_written_directly_to_ispeed_ospeed(
        self,
        backend: BsdBackend,
        tcset_calls: list[tuple[int, int, list[Any]]],
    ) -> None:
        # 250_000 is non-standard on every POSIX build we target — not
        # present in Linux's B* set, not present in BSD's either. The
        # backend must pass the integer directly to tcsetattr.
        backend._apply_config_to_fd(_FAKE_FD, SerialConfig(baudrate=250_000))
        assert len(tcset_calls) == 1
        _fd, _when, attrs = tcset_calls[0]
        assert attrs[4] == 250_000
        assert attrs[5] == 250_000

    def test_rate_reaches_tcsetattr_for_arbitrary_integer(
        self,
        backend: BsdBackend,
        tcset_calls: list[tuple[int, int, list[Any]]],
    ) -> None:
        # Regression guard: if the custom-baud path ever gets routed
        # through baudrate_to_speed (which raises for non-standard
        # rates), this assertion fires with a clear "no tcsetattr" fail
        # instead of a cryptic UnsupportedConfigurationError.
        backend._apply_config_to_fd(_FAKE_FD, SerialConfig(baudrate=333_333))
        _fd, _when, attrs = tcset_calls[0]
        assert attrs[4] == 333_333


class TestRejectFeatureRaisePolicy:
    def test_rs485_raises(self) -> None:
        rs485 = RS485Config(enabled=True)
        config = SerialConfig(
            baudrate=9600,
            rs485=rs485,
            unsupported_policy=UnsupportedPolicy.RAISE,
        )
        with pytest.raises(UnsupportedFeatureError, match="RS-485"):
            BsdBackend()._reject_bsd_unsupported(config)

    def test_low_latency_raises(self) -> None:
        config = SerialConfig(
            baudrate=9600,
            low_latency=True,
            unsupported_policy=UnsupportedPolicy.RAISE,
        )
        with pytest.raises(UnsupportedFeatureError, match="low_latency"):
            BsdBackend()._reject_bsd_unsupported(config)

    def test_clean_config_does_not_raise(self) -> None:
        # No rs485, no low_latency → no-op.
        BsdBackend()._reject_bsd_unsupported(SerialConfig(baudrate=9600))


class TestRejectFeatureWarnPolicy:
    def test_rs485_warns(self) -> None:
        rs485 = RS485Config(enabled=True)
        config = SerialConfig(
            baudrate=9600,
            rs485=rs485,
            unsupported_policy=UnsupportedPolicy.WARN,
        )
        with pytest.warns(RuntimeWarning, match="RS-485"):
            BsdBackend()._reject_bsd_unsupported(config)

    def test_low_latency_warns(self) -> None:
        config = SerialConfig(
            baudrate=9600,
            low_latency=True,
            unsupported_policy=UnsupportedPolicy.WARN,
        )
        with pytest.warns(RuntimeWarning, match="low_latency"):
            BsdBackend()._reject_bsd_unsupported(config)


class TestRejectFeatureIgnorePolicy:
    def test_rs485_ignored_silently(
        self,
        recwarn: pytest.WarningsRecorder,
    ) -> None:
        rs485 = RS485Config(enabled=True)
        config = SerialConfig(
            baudrate=9600,
            rs485=rs485,
            unsupported_policy=UnsupportedPolicy.IGNORE,
        )
        BsdBackend()._reject_bsd_unsupported(config)
        assert len(recwarn) == 0

    def test_low_latency_ignored_silently(
        self,
        recwarn: pytest.WarningsRecorder,
    ) -> None:
        config = SerialConfig(
            baudrate=9600,
            low_latency=True,
            unsupported_policy=UnsupportedPolicy.IGNORE,
        )
        BsdBackend()._reject_bsd_unsupported(config)
        assert len(recwarn) == 0


class TestOpenRejection:
    def test_raise_policy_short_circuits_before_super_open(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # If the rejection runs before super().open, no os.open should
        # fire even against a nonexistent path.
        open_calls: list[str] = []

        def fake_os_open(path: str, _flags: int) -> int:
            open_calls.append(path)
            return _FAKE_FD

        monkeypatch.setattr(os, "open", fake_os_open)

        rs485 = RS485Config(enabled=True)
        config = SerialConfig(
            baudrate=9600,
            rs485=rs485,
            unsupported_policy=UnsupportedPolicy.RAISE,
        )
        with pytest.raises(UnsupportedFeatureError):
            BsdBackend().open("/nonexistent/pretend-tty", config)
        assert open_calls == []


class TestConfigureRejection:
    def test_raise_policy_short_circuits_before_super_configure(
        self,
        backend: BsdBackend,
        tcset_calls: list[tuple[int, int, list[Any]]],
    ) -> None:
        config = SerialConfig(
            baudrate=9600,
            low_latency=True,
            unsupported_policy=UnsupportedPolicy.RAISE,
        )
        with pytest.raises(UnsupportedFeatureError):
            backend.configure(config)
        # super().configure never ran → tcsetattr was never called.
        assert tcset_calls == []
