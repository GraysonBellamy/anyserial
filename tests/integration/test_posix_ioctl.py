"""Integration tests for the shared POSIX ioctl helpers.

These tests open a real pseudoterminal via :func:`pty.openpty` and exercise
every helper against a live kernel fd. Pty behaviour varies: some ioctls that
make sense on a real tty return ``ENOTTY`` on a pty (notably ``TIOCMGET`` on
Linux). The tests assert the actual observable behaviour, and negative cases
verify that helpers let :class:`OSError` propagate for the backend layer to
map with :func:`errno_to_exception`.
"""

from __future__ import annotations

import contextlib
import errno
import os
import sys
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

if sys.platform == "win32":
    pytest.skip("POSIX-only", allow_module_level=True)

import pty

from anyserial._posix.ioctl import (
    get_control_lines,
    get_modem_lines,
    input_waiting,
    output_waiting,
    reset_input_buffer,
    reset_output_buffer,
    set_break,
    set_control_lines,
)

_IS_LINUX = sys.platform.startswith("linux")
_IS_DARWIN = sys.platform == "darwin"
_HAS_BREAK_FALLBACK = _IS_LINUX or _IS_DARWIN or "bsd" in sys.platform

# ``TIOCINQ`` (a.k.a. ``FIONREAD``) on a pty *master* is a Linux-ism: Linux
# returns the count of bytes the master can read, but Darwin's pty driver
# tracks the input/output queues separately and returns 0 on the master
# regardless of what the slave wrote. The ioctl still works correctly on
# real serial fds and on pty *slaves* — it's specifically the master-side
# query that's a no-op on Darwin. Tests that depend on master-side counts
# skip on Darwin; the same code paths are covered on Linux CI.
_TIOCINQ_PTY_MASTER_BROKEN = _IS_DARWIN
_skip_pty_master_inq = pytest.mark.skipif(
    _TIOCINQ_PTY_MASTER_BROKEN,
    reason="TIOCINQ on pty master returns 0 on Darwin (kernel pty quirk, not a bug)",
)


@contextmanager
def pty_pair() -> Iterator[tuple[int, int]]:
    """Yield ``(controller_fd, follower_fd)`` and guarantee they are closed."""
    controller, follower = pty.openpty()
    try:
        yield controller, follower
    finally:
        for fd in (controller, follower):
            with contextlib.suppress(OSError):
                os.close(fd)


class TestInputWaiting:
    def test_zero_when_idle(self) -> None:
        with pty_pair() as (controller, _follower):
            assert input_waiting(controller) == 0

    @_skip_pty_master_inq
    def test_reflects_pending_bytes(self) -> None:
        with pty_pair() as (controller, follower):
            # Avoid \n in the payload — the default pty line discipline
            # translates \n to \r\n on the way out, which would inflate
            # the byte count and make this test assertion flaky.
            payload = b"pingpong"
            os.write(follower, payload)
            # Kernel posting to the controller side takes a moment; poll
            # briefly so the test isn't timing-dependent.
            for _ in range(50):
                if input_waiting(controller) == len(payload):
                    break
                time.sleep(0.001)
            assert input_waiting(controller) == len(payload)


class TestOutputWaiting:
    def test_returns_zero_on_pty(self) -> None:
        # Ptys don't really queue output, so TIOCOUTQ is always 0 — but the
        # call must still succeed so SerialPort.drain can poll on any tty.
        with pty_pair() as (controller, _follower):
            assert output_waiting(controller) == 0


class TestModemAndControlLines:
    def test_get_modem_lines_propagates_enotty_on_pty(self) -> None:
        # Ptys don't implement modem-status ioctls; the kernel returns ENOTTY.
        # The helper must surface the raw OSError; the backend maps it via
        # errno_to_exception(..., context="ioctl") into UnsupportedFeatureError.
        with pty_pair() as (_controller, follower), pytest.raises(OSError) as excinfo:
            get_modem_lines(follower)
        assert excinfo.value.errno in {errno.ENOTTY, errno.EINVAL}

    def test_get_control_lines_propagates_enotty_on_pty(self) -> None:
        with pty_pair() as (_controller, follower), pytest.raises(OSError) as excinfo:
            get_control_lines(follower)
        assert excinfo.value.errno in {errno.ENOTTY, errno.EINVAL}

    def test_set_control_lines_noop_when_all_none(self) -> None:
        # No args set → no syscall → no ENOTTY — the helper returns cleanly.
        with pty_pair() as (_controller, follower):
            set_control_lines(follower)  # should not raise

    def test_set_control_lines_propagates_enotty_on_pty(self) -> None:
        with pty_pair() as (_controller, follower), pytest.raises(OSError) as excinfo:
            set_control_lines(follower, rts=True)
        assert excinfo.value.errno in {errno.ENOTTY, errno.EINVAL}


class TestSetBreak:
    @pytest.mark.skipif(
        not _HAS_BREAK_FALLBACK,
        reason="TIOCSBRK/TIOCCBRK fallback numbers are defined for Linux / Darwin / BSD only",
    )
    def test_assert_and_deassert(self) -> None:
        with pty_pair() as (_controller, follower):
            # Exercises the per-platform fallback path: Python's termios
            # module omits TIOCSBRK / TIOCCBRK on every POSIX we target, so
            # the helper routes through the hardcoded kernel-ABI numbers
            # (``<asm/ioctls.h>`` on Linux, ``<sys/ttycom.h>`` on Darwin /
            # the BSDs) and the ioctls succeed on a real pty fd.
            set_break(follower, on=True)
            set_break(follower, on=False)


class TestResetBuffers:
    @_skip_pty_master_inq
    def test_reset_input_buffer_discards_pending_bytes(self) -> None:
        with pty_pair() as (controller, follower):
            os.write(follower, b"stale")
            for _ in range(50):
                if input_waiting(controller) > 0:
                    break
                time.sleep(0.001)
            assert input_waiting(controller) > 0
            reset_input_buffer(controller)
            assert input_waiting(controller) == 0

    def test_reset_output_buffer_succeeds_on_pty(self) -> None:
        with pty_pair() as (controller, _follower):
            # Nothing to flush, but the call must still succeed.
            reset_output_buffer(controller)
