# pyright: reportPrivateUsage=false
"""Unit tests for :class:`DarwinBackend`'s apply and rejection wiring.

Hermetic — every syscall the backend would make on a real Darwin fd is
monkeypatched so the suite runs on Linux CI. The key contracts to pin:

- Standard baud flows through the inherited ``PosixBackend`` pipeline;
  non-standard baud takes the ``IOSSIOSPEED`` two-step.
- ``low_latency`` and ``rs485`` route through :class:`UnsupportedPolicy`
  the same way :class:`LinuxBackend` routes driver-level rejections,
  but without an underlying ``OSError`` — Darwin simply has no mechanism
  for either feature.
- Rejections happen *before* :meth:`PosixBackend.open` under ``RAISE``
  so the orchestrator never sees a transiently-open fd.
"""

from __future__ import annotations

import os
import termios
from typing import TYPE_CHECKING, Any

import pytest

from anyserial._darwin import backend as backend_mod
from anyserial._darwin.backend import DarwinBackend
from anyserial._darwin.capabilities import darwin_capabilities
from anyserial._types import UnsupportedPolicy
from anyserial.config import RS485Config, SerialConfig
from anyserial.exceptions import UnsupportedFeatureError

if TYPE_CHECKING:
    from collections.abc import Iterator


_FAKE_FD = 77
_FAKE_PATH = "/dev/tty.usbserial-A12345"


def _baseline_termios() -> list[Any]:
    """Return a 7-list in the shape :func:`termios.tcgetattr` produces.

    cc slot must be 20 slots on POSIX; content doesn't matter — the builders
    overwrite VMIN / VTIME and leave the rest untouched.
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

    # Patch at the termios module itself — there's only one global copy,
    # shared by both ``backend_mod`` and the parent ``_posix.backend``.
    monkeypatch.setattr(termios, "tcgetattr", fake_tcgetattr)
    monkeypatch.setattr(termios, "tcsetattr", fake_tcsetattr)
    return calls


@pytest.fixture
def iossiospeed_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, int]]:
    """Capture every ``set_iossiospeed`` call as ``(fd, rate)``."""
    calls: list[tuple[int, int]] = []

    def fake_set_iossiospeed(fd: int, rate: int) -> None:
        calls.append((fd, rate))

    monkeypatch.setattr(backend_mod, "set_iossiospeed", fake_set_iossiospeed)
    return calls


@pytest.fixture
def backend() -> Iterator[DarwinBackend]:
    """Return a :class:`DarwinBackend` with a fake fd so the apply hooks run.

    Bypasses :meth:`DarwinBackend.open` entirely — the apply path reads
    only ``self._fd``, and the patched termios helpers accept any
    sentinel integer without issuing real syscalls.
    """
    b = DarwinBackend()
    b._fd = _FAKE_FD
    b._path = _FAKE_PATH
    try:
        yield b
    finally:
        # Prevent the object's finalizer from trying to close the fake fd.
        b._fd = -1


class TestCapabilities:
    def test_backend_returns_darwin_snapshot(self) -> None:
        assert DarwinBackend().capabilities == darwin_capabilities()


class TestApplyConfigStandardBaud:
    def test_standard_rate_uses_parent_pipeline(
        self,
        backend: DarwinBackend,
        tcset_calls: list[tuple[int, int, list[Any]]],
        iossiospeed_calls: list[tuple[int, int]],
    ) -> None:
        backend._apply_config_to_fd(_FAKE_FD, SerialConfig(baudrate=115200))
        # Parent path: one tcsetattr, no IOSSIOSPEED.
        assert len(tcset_calls) == 1
        assert iossiospeed_calls == []
        fd, when, attrs = tcset_calls[0]
        assert fd == _FAKE_FD
        assert when == termios.TCSANOW
        # Speed slots carry the real B115200 constant, not the placeholder.
        assert attrs[4] == termios.B115200
        assert attrs[5] == termios.B115200


class TestApplyConfigCustomBaud:
    def test_custom_rate_uses_iossiospeed(
        self,
        backend: DarwinBackend,
        tcset_calls: list[tuple[int, int, list[Any]]],
        iossiospeed_calls: list[tuple[int, int]],
    ) -> None:
        backend._apply_config_to_fd(_FAKE_FD, SerialConfig(baudrate=250_000))
        # Non-standard rate: one tcsetattr (with placeholder) + one IOSSIOSPEED.
        assert len(tcset_calls) == 1
        assert iossiospeed_calls == [(_FAKE_FD, 250_000)]
        _fd, _when, attrs = tcset_calls[0]
        # Placeholder speed lives in the termios struct so tcsetattr accepts
        # the attribute set; IOSSIOSPEED overrides the hardware afterwards.
        assert attrs[4] == termios.B9600
        assert attrs[5] == termios.B9600

    def test_iossiospeed_runs_after_tcsetattr(
        self,
        backend: DarwinBackend,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Order matters: IOSSIOSPEED must follow tcsetattr, because a later
        # tcsetattr would revert the line speed to the placeholder.
        sequence: list[str] = []

        def fake_tcgetattr(_fd: int) -> list[Any]:
            return _baseline_termios()

        def fake_tcsetattr(_fd: int, _when: int, _attrs: list[Any]) -> None:
            sequence.append("tcsetattr")

        def fake_set_iossiospeed(_fd: int, _rate: int) -> None:
            sequence.append("iossiospeed")

        monkeypatch.setattr(termios, "tcgetattr", fake_tcgetattr)
        monkeypatch.setattr(termios, "tcsetattr", fake_tcsetattr)
        monkeypatch.setattr(backend_mod, "set_iossiospeed", fake_set_iossiospeed)

        # 250_000 is non-standard on every POSIX build — neither Linux
        # (whose B* set jumps 230400 → 460800) nor Darwin exposes a
        # matching termios constant, so both CI hosts route it through
        # the IOSSIOSPEED path. A rate like 500_000 *is* standard on
        # Linux, which would silently skip this test on Linux runners.
        backend._apply_config_to_fd(_FAKE_FD, SerialConfig(baudrate=250_000))
        assert sequence == ["tcsetattr", "iossiospeed"]


class TestRejectFeatureRaisePolicy:
    def test_rs485_raises(self) -> None:
        rs485 = RS485Config(enabled=True)
        config = SerialConfig(
            baudrate=9600,
            rs485=rs485,
            unsupported_policy=UnsupportedPolicy.RAISE,
        )
        backend = DarwinBackend()
        with pytest.raises(UnsupportedFeatureError, match="RS-485"):
            backend._reject_darwin_unsupported(config)

    def test_low_latency_raises(self) -> None:
        config = SerialConfig(
            baudrate=9600,
            low_latency=True,
            unsupported_policy=UnsupportedPolicy.RAISE,
        )
        backend = DarwinBackend()
        with pytest.raises(UnsupportedFeatureError, match="low_latency"):
            backend._reject_darwin_unsupported(config)

    def test_clean_config_does_not_raise(self) -> None:
        config = SerialConfig(baudrate=9600)
        backend = DarwinBackend()
        # No rs485, no low_latency → no-op.
        backend._reject_darwin_unsupported(config)


class TestRejectFeatureWarnPolicy:
    def test_rs485_warns(self) -> None:
        rs485 = RS485Config(enabled=True)
        config = SerialConfig(
            baudrate=9600,
            rs485=rs485,
            unsupported_policy=UnsupportedPolicy.WARN,
        )
        backend = DarwinBackend()
        with pytest.warns(RuntimeWarning, match="RS-485"):
            backend._reject_darwin_unsupported(config)

    def test_low_latency_warns(self) -> None:
        config = SerialConfig(
            baudrate=9600,
            low_latency=True,
            unsupported_policy=UnsupportedPolicy.WARN,
        )
        backend = DarwinBackend()
        with pytest.warns(RuntimeWarning, match="low_latency"):
            backend._reject_darwin_unsupported(config)


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
        backend = DarwinBackend()
        backend._reject_darwin_unsupported(config)
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
        backend = DarwinBackend()
        backend._reject_darwin_unsupported(config)
        assert len(recwarn) == 0


class TestOpenRejection:
    def test_raise_policy_short_circuits_before_super_open(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # If the rejection runs before super().open, no os.open should fire
        # even against a nonexistent path. Swap os.open for a sentinel that
        # records invocation to prove the short-circuit.
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
            DarwinBackend().open("/nonexistent/pretend-tty", config)
        assert open_calls == []


class TestConfigureRejection:
    def test_raise_policy_short_circuits_before_super_configure(
        self,
        backend: DarwinBackend,
        tcset_calls: list[tuple[int, int, list[Any]]],
        iossiospeed_calls: list[tuple[int, int]],
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
        assert iossiospeed_calls == []
