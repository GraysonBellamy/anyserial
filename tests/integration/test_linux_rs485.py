"""Integration tests for the :class:`LinuxBackend` RS-485 path.

A Linux pty does not implement ``TIOCSRS485`` — the kernel returns
``ENOTTY``. That makes the pty an excellent stand-in for "driver does
not support RS-485", so we can cover every :class:`UnsupportedPolicy`
branch end-to-end without needing a real RS-485 adapter. A separate set
of tests monkeypatches ``fcntl.ioctl`` to simulate a driver that accepts
the ioctl and asserts the exact payload the backend sends.
"""

from __future__ import annotations

import contextlib
import errno
import os
import pty
import sys
import warnings
from contextlib import contextmanager
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

if not sys.platform.startswith("linux"):
    pytest.skip("Linux-only", allow_module_level=True)

from anyserial._linux import rs485
from anyserial._linux.backend import LinuxBackend
from anyserial._types import UnsupportedPolicy
from anyserial.config import RS485Config, SerialConfig
from anyserial.exceptions import UnsupportedFeatureError


@contextmanager
def pty_path() -> Generator[tuple[int, int, str]]:
    """Yield ``(controller, follower, path)`` — follower anchors the tty alive."""
    controller, follower = pty.openpty()
    path = os.ttyname(follower)
    try:
        yield controller, follower, path
    finally:
        for fd in (controller, follower):
            with contextlib.suppress(OSError):
                os.close(fd)


# ---------------------------------------------------------------------------
# Policy branches against a real pty (kernel returns ENOTTY for ptys).
# ---------------------------------------------------------------------------


class TestRaisePolicyOnPty:
    def test_open_with_rs485_raises(self) -> None:
        with pty_path() as (_controller, _anchor, path):
            backend = LinuxBackend()
            config = SerialConfig(
                rs485=RS485Config(),
                unsupported_policy=UnsupportedPolicy.RAISE,
            )
            with pytest.raises(UnsupportedFeatureError, match="TIOCSRS485"):
                backend.open(path, config)
            # Failed open must leave the backend closed so a retry with
            # rs485=None starts from clean state.
            assert not backend.is_open

    def test_configure_with_rs485_raises(self) -> None:
        with pty_path() as (_controller, _anchor, path):
            backend = LinuxBackend()
            backend.open(path, SerialConfig())
            try:
                new_config = SerialConfig(
                    rs485=RS485Config(),
                    unsupported_policy=UnsupportedPolicy.RAISE,
                )
                with pytest.raises(UnsupportedFeatureError, match="TIOCSRS485"):
                    backend.configure(new_config)
                # configure() is not expected to close the fd on failure;
                # the user may want to try a different policy.
                assert backend.is_open
            finally:
                backend.close()


class TestWarnPolicyOnPty:
    def test_open_with_rs485_warns_and_succeeds(self) -> None:
        with pty_path() as (_controller, _anchor, path):
            backend = LinuxBackend()
            config = SerialConfig(
                rs485=RS485Config(),
                unsupported_policy=UnsupportedPolicy.WARN,
            )
            with pytest.warns(RuntimeWarning, match="TIOCSRS485"):
                backend.open(path, config)
            try:
                assert backend.is_open
            finally:
                backend.close()


class TestIgnorePolicyOnPty:
    def test_open_with_rs485_ignored_silently(self) -> None:
        with pty_path() as (_controller, _anchor, path):
            backend = LinuxBackend()
            config = SerialConfig(
                rs485=RS485Config(),
                unsupported_policy=UnsupportedPolicy.IGNORE,
            )
            with warnings.catch_warnings():
                warnings.simplefilter("error")  # any warning would fail here
                backend.open(path, config)
            try:
                assert backend.is_open
            finally:
                backend.close()


# ---------------------------------------------------------------------------
# Driver-accepts path: monkeypatch fcntl.ioctl to model a cooperative driver.
# ---------------------------------------------------------------------------


class _StubRS485Driver:
    """Pretend to be a driver that honours ``TIOCGRS485`` / ``TIOCSRS485``.

    Delegates every non-RS-485 ioctl to the original :func:`fcntl.ioctl`
    (so termios setup, exclusive locking, modem-line queries etc. still
    touch the real kernel). The RS-485 request codes are emulated in
    Python so tests can assert the exact payloads the backend sends.
    """

    def __init__(
        self,
        *,
        original_ioctl: Callable[..., object],
        initial: rs485.RS485State,
    ) -> None:
        self._original = original_ioctl
        self.state = initial
        self.set_payloads: list[bytes] = []
        self.set_states: list[rs485.RS485State] = []

    def __call__(self, fd: int, request: int, arg: object = 0) -> object:
        if request == rs485.TIOCGRS485:
            return self.state.to_bytes()
        if request == rs485.TIOCSRS485:
            assert isinstance(arg, (bytes, bytearray)), type(arg)
            payload = bytes(arg)
            self.set_payloads.append(payload)
            self.set_states.append(rs485.RS485State.from_bytes(payload))
            # Real drivers update their internal view; simulate that so
            # a follow-up TIOCGRS485 reads back what we just wrote.
            self.state = self.set_states[-1]
            return 0
        # Everything else — termios, flock, TIOCMGET, TIOCMBIS, … — is
        # still a real syscall on the pty fd.
        return self._original(fd, request, arg)


@pytest.fixture
def stub_driver(monkeypatch: pytest.MonkeyPatch) -> Generator[_StubRS485Driver]:
    """Install a :class:`_StubRS485Driver` over both modules' ``fcntl.ioctl``.

    The backend routes through :mod:`anyserial._posix.ioctl` (TIOCMGET /
    TIOCMBIS / TIOCMBIC etc.) and :mod:`anyserial._linux.rs485`
    (TIOCGRS485 / TIOCSRS485). Both import ``fcntl`` at module scope,
    so the patch has to cover both.
    """
    import fcntl  # noqa: PLC0415 — fixture-local
    from typing import cast  # noqa: PLC0415 — fixture-local

    # fcntl.ioctl has overloaded stubs that won't bind to Callable[..., object]
    # without help; the cast is a typing-only coercion.
    original: Callable[..., object] = cast("Callable[..., object]", fcntl.ioctl)
    stub = _StubRS485Driver(original_ioctl=original, initial=rs485.RS485State())
    monkeypatch.setattr("anyserial._linux.rs485.fcntl.ioctl", stub)
    yield stub


class TestDriverAccepts:
    def test_open_writes_merged_state(self, stub_driver: _StubRS485Driver) -> None:
        # Seed a driver that already advertises TERMINATE_BUS — our
        # apply must preserve it while setting the user's flags.
        stub_driver.state = rs485.RS485State(flags=rs485.SER_RS485_TERMINATE_BUS)
        with pty_path() as (_controller, _anchor, path):
            backend = LinuxBackend()
            config = SerialConfig(
                rs485=RS485Config(delay_before_send=0.002, delay_after_send=0.004),
            )
            backend.open(path, config)
            try:
                assert len(stub_driver.set_states) == 1
                written = stub_driver.set_states[0]
                assert written.flags & rs485.SER_RS485_ENABLED
                assert written.flags & rs485.SER_RS485_RTS_ON_SEND
                # TERMINATE_BUS reported by the driver must round-trip.
                assert written.flags & rs485.SER_RS485_TERMINATE_BUS
                assert written.delay_rts_before_send == 2
                assert written.delay_rts_after_send == 4
            finally:
                backend.close()

    def test_configure_applies_new_rs485(self, stub_driver: _StubRS485Driver) -> None:
        with pty_path() as (_controller, _anchor, path):
            backend = LinuxBackend()
            backend.open(path, SerialConfig())
            try:
                # No RS-485 write on open → stub has no set_states yet.
                assert stub_driver.set_states == []
                backend.configure(
                    SerialConfig(rs485=RS485Config(rts_after_send=True)),
                )
                assert len(stub_driver.set_states) == 1
                written = stub_driver.set_states[0]
                assert written.flags & rs485.SER_RS485_ENABLED
                assert written.flags & rs485.SER_RS485_RTS_AFTER_SEND
            finally:
                backend.close()

    def test_reconfigure_back_to_none_restores_saved_state(
        self,
        stub_driver: _StubRS485Driver,
    ) -> None:
        # Driver initially has TERMINATE_BUS + address mode set; after
        # enabling then disabling RS-485 the saved state must be
        # written back verbatim.
        pristine = rs485.RS485State(
            flags=rs485.SER_RS485_TERMINATE_BUS,
            addr_recv=0x42,
            addr_dest=0x7F,
        )
        stub_driver.state = pristine
        with pty_path() as (_controller, _anchor, path):
            backend = LinuxBackend()
            backend.open(path, SerialConfig(rs485=RS485Config()))
            try:
                assert len(stub_driver.set_states) == 1  # from open
                backend.configure(SerialConfig())  # rs485=None → restore
                assert len(stub_driver.set_states) == 2
                restored = stub_driver.set_states[-1]
                assert restored == pristine
            finally:
                backend.close()

    def test_close_restores_saved_state(
        self,
        stub_driver: _StubRS485Driver,
    ) -> None:
        pristine = rs485.RS485State(flags=rs485.SER_RS485_TERMINATE_BUS)
        stub_driver.state = pristine
        with pty_path() as (_controller, _anchor, path):
            backend = LinuxBackend()
            backend.open(path, SerialConfig(rs485=RS485Config()))
            assert len(stub_driver.set_states) == 1
            backend.close()
        # close() must have issued a second write restoring the saved
        # state. The pty is gone by now but the stub captured the call.
        assert len(stub_driver.set_states) == 2
        assert stub_driver.set_states[-1] == pristine


class TestWriteRejectedByDriver:
    """Driver advertises the ioctl on read but refuses ``TIOCSRS485``."""

    def test_einval_on_write_raises_under_raise_policy(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import fcntl  # noqa: PLC0415 — fixture-local

        original = fcntl.ioctl

        def picky_ioctl(fd: int, request: int, arg: object = 0) -> object:
            if request == rs485.TIOCGRS485:
                return rs485.RS485State().to_bytes()
            if request == rs485.TIOCSRS485:
                raise OSError(errno.EINVAL, "Invalid argument")
            # Fall back to the real ioctl for unrelated requests. mypy
            # cannot narrow ``object`` to the overload set so we use a
            # blanket ignore — the runtime path is fully covered by the
            # pty fixture it sees.
            return original(fd, request, arg)  # type: ignore[call-overload]

        monkeypatch.setattr("anyserial._linux.rs485.fcntl.ioctl", picky_ioctl)

        with pty_path() as (_controller, _anchor, path):
            backend = LinuxBackend()
            config = SerialConfig(
                rs485=RS485Config(),
                unsupported_policy=UnsupportedPolicy.RAISE,
            )
            with pytest.raises(UnsupportedFeatureError, match="TIOCSRS485"):
                backend.open(path, config)
            assert not backend.is_open
