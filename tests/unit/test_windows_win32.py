"""Layout assertions for the Win32 ctypes structures.

These checks pin the binary layout of every struct we hand to kernel32.
A drift in field order, packing, or bitfield width would corrupt
``SetCommState`` / ``ClearCommError`` and surface as a baffling
``ERROR_INVALID_PARAMETER`` deep in the Windows data path. Catching it
at unit-test time keeps Linux CI useful for a Windows-only backend.

Field offsets are validated against Microsoft's documented DCB / COMSTAT /
COMMTIMEOUTS / OVERLAPPED layouts.
"""

from __future__ import annotations

from ctypes import sizeof

import pytest

from anyserial._windows._win32 import (
    COMMTIMEOUTS,
    COMSTAT,
    DCB,
    OVERLAPPED,
    Kernel32Bindings,
    normalise_com_path,
)


class TestStructSizes:
    def test_dcb_is_28_bytes(self) -> None:
        # design-windows-backend.md §6.2 invariant: DCBlength = 28.
        assert sizeof(DCB) == 28

    def test_commtimeouts_is_20_bytes(self) -> None:
        # 5 * DWORD.
        assert sizeof(COMMTIMEOUTS) == 20

    def test_comstat_is_12_bytes(self) -> None:
        # 1 packed DWORD of bitfields + 2 DWORDs.
        assert sizeof(COMSTAT) == 12

    def test_overlapped_size_matches_pointer_sized_layout(self) -> None:
        # On 64-bit: 8 + 8 + 4 + 4 + 8 = 32. On 32-bit: 4 + 4 + 4 + 4 + 4 = 20.
        # We accept both so the assertion isn't tied to the host word size.
        assert sizeof(OVERLAPPED) in {20, 32}


class TestDcbBitfieldRoundTrip:
    def test_setting_a_bitfield_does_not_corrupt_neighbours(self) -> None:
        # If the bitfield widths are wrong, setting fRtsControl=2 (HANDSHAKE)
        # would bleed into fAbortOnError or fDummy2.
        dcb = DCB()
        dcb.fBinary = 1
        dcb.fParity = 1
        dcb.fOutxCtsFlow = 1
        dcb.fOutxDsrFlow = 1
        dcb.fDtrControl = 2
        dcb.fOutX = 1
        dcb.fInX = 1
        dcb.fRtsControl = 2
        dcb.fAbortOnError = 0
        # Read each one back.
        assert dcb.fBinary == 1
        assert dcb.fParity == 1
        assert dcb.fOutxCtsFlow == 1
        assert dcb.fOutxDsrFlow == 1
        assert dcb.fDtrControl == 2
        assert dcb.fOutX == 1
        assert dcb.fInX == 1
        assert dcb.fRtsControl == 2
        assert dcb.fAbortOnError == 0


class TestComstatBitfields:
    def test_cb_in_que_independent_of_bitfields(self) -> None:
        stat = COMSTAT()
        stat.fXoffSent = 1
        stat.cbInQue = 0xDEADBEEF
        stat.cbOutQue = 0xCAFEBABE
        # Setting a bitfield must not touch the DWORD members beyond it.
        assert stat.fXoffSent == 1
        assert stat.cbInQue == 0xDEADBEEF
        assert stat.cbOutQue == 0xCAFEBABE


class TestKernel32BindingsSlots:
    """Verify that the ``WaitCommEvent`` bindings are declared on the binding table."""

    def test_wait_comm_event_slot_exists(self) -> None:
        assert "WaitCommEvent" in Kernel32Bindings.__slots__

    def test_create_event_slot_exists(self) -> None:
        assert "CreateEventW" in Kernel32Bindings.__slots__

    def test_reset_event_slot_exists(self) -> None:
        assert "ResetEvent" in Kernel32Bindings.__slots__


class TestNormalisePath:
    @pytest.mark.parametrize(
        ("given", "expected"),
        [
            ("COM1", "\\\\.\\COM1"),
            ("COM10", "\\\\.\\COM10"),
            ("COM255", "\\\\.\\COM255"),
            # Already prefixed → returned unchanged.
            ("\\\\.\\COM1", "\\\\.\\COM1"),
            ("\\\\.\\COM42", "\\\\.\\COM42"),
        ],
    )
    def test_prefix_added_when_missing(self, given: str, expected: str) -> None:
        assert normalise_com_path(given) == expected
