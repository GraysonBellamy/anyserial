# pyright: reportPrivateUsage=false
"""Unit tests for the shared POSIX ioctl module.

Only the platform-dispatch logic (request-code selection) is exercised here.
Every real-kernel ioctl lives in :mod:`tests.integration.test_posix_ioctl`,
which requires a pty and therefore only runs on the platforms whose kernels
accept the request codes this module hands back.

The key contract we pin down: ``TIOCSBRK`` / ``TIOCCBRK`` resolve through
platform-appropriate numeric fallbacks whenever Python's ``termios`` module
omits the names — and it does on every POSIX target. Getting the numeric
values wrong silently sends a garbage ioctl to the kernel, so the explicit
hex assertions are the regression guard.
"""

from __future__ import annotations

import pytest

import anyserial._posix.ioctl as ioctl_mod
from anyserial._posix.ioctl import (
    _BSD_TIOCCBRK,
    _BSD_TIOCSBRK,
    _LINUX_TIOCCBRK,
    _LINUX_TIOCSBRK,
    _break_request,
)


class TestBreakRequestConstants:
    """Pin the hardcoded kernel-ABI numeric values against surprise changes."""

    def test_linux_values_match_asm_ioctls_h(self) -> None:
        # <asm/ioctls.h>: TIOCSBRK = 0x5427, TIOCCBRK = 0x5428.
        assert _LINUX_TIOCSBRK == 0x5427
        assert _LINUX_TIOCCBRK == 0x5428

    def test_bsd_family_values_match_ttycom_h(self) -> None:
        # <sys/ttycom.h>: TIOCSBRK = _IO('t', 123), TIOCCBRK = _IO('t', 122).
        # _IO(g, n) = IOC_VOID (0x20000000) | (g << 8) | n.
        assert _BSD_TIOCSBRK == 0x2000_747B
        assert _BSD_TIOCCBRK == 0x2000_747A


class TestBreakRequestDispatch:
    """Verify ``_break_request`` routes to the right number on each platform."""

    @pytest.fixture
    def _force_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Make sure the getattr(termios, ...) short-circuit never fires in
        # these tests even if a future CPython build starts exporting the
        # name. We want to assert the fallback branch's output, not what
        # stdlib happens to ship today.
        monkeypatch.setattr(ioctl_mod, "termios", _TermiosWithoutBreakNames())

    def test_linux_returns_linux_values(
        self,
        monkeypatch: pytest.MonkeyPatch,
        _force_fallback: None,
    ) -> None:
        monkeypatch.setattr(ioctl_mod, "_IS_LINUX", True)
        monkeypatch.setattr(ioctl_mod, "_IS_BSD_FAMILY", False)
        assert _break_request(on=True) == _LINUX_TIOCSBRK
        assert _break_request(on=False) == _LINUX_TIOCCBRK

    def test_bsd_family_returns_bsd_values(
        self,
        monkeypatch: pytest.MonkeyPatch,
        _force_fallback: None,
    ) -> None:
        # Darwin, FreeBSD, NetBSD, OpenBSD, DragonFly — one shared ttycom.h.
        monkeypatch.setattr(ioctl_mod, "_IS_LINUX", False)
        monkeypatch.setattr(ioctl_mod, "_IS_BSD_FAMILY", True)
        assert _break_request(on=True) == _BSD_TIOCSBRK
        assert _break_request(on=False) == _BSD_TIOCCBRK

    def test_unknown_platform_returns_none(
        self,
        monkeypatch: pytest.MonkeyPatch,
        _force_fallback: None,
    ) -> None:
        # Unknown POSIX / exotic platform: signal "no fallback available"
        # so set_break raises UnsupportedFeatureError via _require().
        monkeypatch.setattr(ioctl_mod, "_IS_LINUX", False)
        monkeypatch.setattr(ioctl_mod, "_IS_BSD_FAMILY", False)
        assert _break_request(on=True) is None
        assert _break_request(on=False) is None


class _TermiosWithoutBreakNames:
    """Stand-in for :mod:`termios` that never exposes TIOCSBRK/TIOCCBRK.

    Everything else a caller might look up (``VMIN``, ``TIOCINQ``, …) is
    not needed here — ``_break_request`` only consults the two break names.
    ``getattr`` with a default takes the fallback branch when the attribute
    is missing on an instance of this class.
    """
