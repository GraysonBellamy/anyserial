"""Tests for :class:`CommEvent` and WaitCommEvent-related constants.

Pins the Win32 event-mask bit values against the MSDN documentation and
verifies the mask → :class:`CommEvent` dataclass round-trip that
:meth:`WindowsBackend.wait_modem_event` performs.
"""

from __future__ import annotations

import pytest

from anyserial._types import CommEvent
from anyserial._windows import _win32 as w


class TestEventMaskConstants:
    """Pin bit values against MSDN ``SetCommMask`` / ``WaitCommEvent``."""

    def test_ev_cts(self) -> None:
        assert w.EV_CTS == 0x0008

    def test_ev_dsr(self) -> None:
        assert w.EV_DSR == 0x0010

    def test_ev_rlsd(self) -> None:
        assert w.EV_RLSD == 0x0020

    def test_ev_break(self) -> None:
        assert w.EV_BREAK == 0x0040

    def test_ev_err(self) -> None:
        assert w.EV_ERR == 0x0080

    def test_ev_ring(self) -> None:
        assert w.EV_RING == 0x0100

    def test_ev_all_modem_is_union_of_individual_flags(self) -> None:
        expected = w.EV_CTS | w.EV_DSR | w.EV_RLSD | w.EV_BREAK | w.EV_ERR | w.EV_RING
        assert expected == w.EV_ALL_MODEM

    def test_ev_all_modem_does_not_include_ev_rxchar(self) -> None:
        # design-windows-backend.md §6.4: EV_RXCHAR is deliberately
        # excluded — we do not use comm events for data-path readiness.
        EV_RXCHAR = 0x0001  # noqa: N806 — Win32 constant convention
        assert w.EV_ALL_MODEM & EV_RXCHAR == 0

    def test_error_io_pending(self) -> None:
        assert w.ERROR_IO_PENDING == 997


class TestCommEventDataclass:
    """Verify the frozen dataclass contract."""

    def test_defaults_all_false(self) -> None:
        event = CommEvent()
        assert not event.cts_changed
        assert not event.dsr_changed
        assert not event.rlsd_changed
        assert not event.ring
        assert not event.error
        assert not event.break_received

    def test_all_true(self) -> None:
        event = CommEvent(
            cts_changed=True,
            dsr_changed=True,
            rlsd_changed=True,
            ring=True,
            error=True,
            break_received=True,
        )
        assert event.cts_changed
        assert event.dsr_changed
        assert event.rlsd_changed
        assert event.ring
        assert event.error
        assert event.break_received

    def test_frozen(self) -> None:
        event = CommEvent()
        with pytest.raises(AttributeError):
            event.cts_changed = True  # type: ignore[misc]

    def test_equality(self) -> None:
        a = CommEvent(cts_changed=True)
        b = CommEvent(cts_changed=True)
        assert a == b

    def test_inequality(self) -> None:
        a = CommEvent(cts_changed=True)
        b = CommEvent(dsr_changed=True)
        assert a != b


class TestMaskToCommEventRoundTrip:
    """Simulate the mask → CommEvent translation that
    ``WindowsBackend.wait_modem_event`` performs.
    """

    @staticmethod
    def _mask_to_event(mask: int) -> CommEvent:
        """Mirror the logic in ``backend.py:wait_modem_event``."""
        return CommEvent(
            cts_changed=bool(mask & w.EV_CTS),
            dsr_changed=bool(mask & w.EV_DSR),
            rlsd_changed=bool(mask & w.EV_RLSD),
            ring=bool(mask & w.EV_RING),
            error=bool(mask & w.EV_ERR),
            break_received=bool(mask & w.EV_BREAK),
        )

    def test_zero_mask_yields_all_false(self) -> None:
        event = self._mask_to_event(0)
        assert event == CommEvent()

    def test_cts_only(self) -> None:
        event = self._mask_to_event(w.EV_CTS)
        assert event.cts_changed is True
        assert event.dsr_changed is False

    def test_dsr_only(self) -> None:
        event = self._mask_to_event(w.EV_DSR)
        assert event.dsr_changed is True
        assert event.cts_changed is False

    def test_rlsd_only(self) -> None:
        event = self._mask_to_event(w.EV_RLSD)
        assert event.rlsd_changed is True

    def test_ring_only(self) -> None:
        event = self._mask_to_event(w.EV_RING)
        assert event.ring is True

    def test_error_only(self) -> None:
        event = self._mask_to_event(w.EV_ERR)
        assert event.error is True

    def test_break_only(self) -> None:
        event = self._mask_to_event(w.EV_BREAK)
        assert event.break_received is True

    def test_all_flags_set(self) -> None:
        event = self._mask_to_event(w.EV_ALL_MODEM)
        assert event == CommEvent(
            cts_changed=True,
            dsr_changed=True,
            rlsd_changed=True,
            ring=True,
            error=True,
            break_received=True,
        )

    @pytest.mark.parametrize(
        ("flag", "field"),
        [
            (w.EV_CTS, "cts_changed"),
            (w.EV_DSR, "dsr_changed"),
            (w.EV_RLSD, "rlsd_changed"),
            (w.EV_RING, "ring"),
            (w.EV_ERR, "error"),
            (w.EV_BREAK, "break_received"),
        ],
    )
    def test_each_flag_maps_to_exactly_one_field(self, flag: int, field: str) -> None:
        event = self._mask_to_event(flag)
        # The named field should be True.
        assert getattr(event, field) is True
        # Every other field should be False.
        for name in CommEvent.__dataclass_fields__:
            if name != field:
                assert getattr(event, name) is False, (
                    f"{name} should be False for flag 0x{flag:04X}"
                )

    def test_combined_cts_and_error(self) -> None:
        event = self._mask_to_event(w.EV_CTS | w.EV_ERR)
        assert event.cts_changed is True
        assert event.error is True
        assert event.dsr_changed is False
        assert event.rlsd_changed is False
        assert event.ring is False
        assert event.break_received is False
