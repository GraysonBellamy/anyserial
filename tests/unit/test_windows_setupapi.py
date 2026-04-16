"""Layout assertions for the SetupAPI ctypes structures.

Pins the binary layout of every struct we hand to setupapi.dll. A drift
in field order or packing would produce garbage from
``SetupDiEnumDeviceInterfaces`` / ``SetupDiGetDeviceInterfaceDetailW``
and surface as a silent empty-enumeration rather than a loud crash.
Catching it at unit-test time keeps Linux CI useful.

The ``cbSize`` guard (§8: "On x64 it must be 8; on x86 it must be 6")
is the single most fragile value in the entire discovery path — if wrong,
*every* ``SetupDiGetDeviceInterfaceDetailW`` call silently fails.
"""

from __future__ import annotations

from ctypes import c_void_p, sizeof

from anyserial._windows._setupapi import (
    DETAIL_CB_SIZE,
    GUID,
    GUID_DEVINTERFACE_COMPORT,
    SP_DEVICE_INTERFACE_DATA,
    SP_DEVICE_INTERFACE_DETAIL_DATA_W,
    SP_DEVINFO_DATA,
)


class TestStructSizes:
    def test_guid_is_16_bytes(self) -> None:
        assert sizeof(GUID) == 16

    def test_sp_devinfo_data_size_matches_pointer_layout(self) -> None:
        # On x64: cbSize(4) + GUID(16) + DevInst(4) + Reserved(8) = 32.
        # On x86: cbSize(4) + GUID(16) + DevInst(4) + Reserved(4) = 28.
        expected = 28 if sizeof(c_void_p) == 4 else 32
        assert sizeof(SP_DEVINFO_DATA) == expected

    def test_sp_device_interface_data_size_matches_pointer_layout(self) -> None:
        # On x64: cbSize(4) + GUID(16) + Flags(4) + Reserved(8) = 32.
        # On x86: cbSize(4) + GUID(16) + Flags(4) + Reserved(4) = 28.
        expected = 28 if sizeof(c_void_p) == 4 else 32
        assert sizeof(SP_DEVICE_INTERFACE_DATA) == expected


class TestDetailCbSize:
    """The ``cbSize`` value is the single most brittle constant in the
    discovery path. If wrong, ``SetupDiGetDeviceInterfaceDetailW``
    silently returns ``FALSE`` and enumeration produces zero results.
    """

    def test_detail_cb_size_matches_architecture(self) -> None:
        if sizeof(c_void_p) == 8:
            assert DETAIL_CB_SIZE == 8, "x64 cbSize must be 8"
        else:
            assert DETAIL_CB_SIZE == 6, "x86 cbSize must be 6"

    def test_detail_struct_can_hold_a_path(self) -> None:
        # The struct embeds a 512-wchar buffer — large enough for any
        # COM-port interface path.
        detail = SP_DEVICE_INTERFACE_DETAIL_DATA_W()
        detail.cbSize = DETAIL_CB_SIZE
        detail.DevicePath = (
            r"\\?\USB#VID_0403&PID_6001#A12345"
            "#{86e0d1e0-8089-11d0-9ce4-08003e301f73}"
        )
        assert "VID_0403" in detail.DevicePath


class TestGuidDevinterfaceComport:
    """Pin the GUID bytes against the MS Learn reference:
    ``{86E0D1E0-8089-11D0-9CE4-08003E301F73}``.
    """

    def test_data1(self) -> None:
        assert GUID_DEVINTERFACE_COMPORT.Data1 == 0x86E0D1E0

    def test_data2(self) -> None:
        assert GUID_DEVINTERFACE_COMPORT.Data2 == 0x8089

    def test_data3(self) -> None:
        assert GUID_DEVINTERFACE_COMPORT.Data3 == 0x11D0

    def test_data4(self) -> None:
        expected = bytes([0x9C, 0xE4, 0x08, 0x00, 0x3E, 0x30, 0x1F, 0x73])
        assert bytes(GUID_DEVINTERFACE_COMPORT.Data4) == expected
