# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# Reason: ``ctypes.WinDLL.setupapi`` attribute lookups resolve to untyped
# function pointers. See the matching rationale in ``_win32.py``.
"""SetupAPI ctypes bindings for Windows serial-port discovery.

Wraps the narrow slice of the SetupAPI surface needed to enumerate COM
ports via ``GUID_DEVINTERFACE_COMPORT``: device-interface enumeration,
interface detail, and registry-property queries. All Win32 struct
layouts match the Microsoft headers; field sizes are platform-dependent
(``cbSize`` of ``SP_DEVICE_INTERFACE_DETAIL_DATA_W`` is 8 on x64, 6 on
x86 — guarded by ``sizeof(c_void_p)``).

The loader follows the same lazy-init pattern as :mod:`._win32`: simply
importing this module on a non-Windows host does no work beyond
Python-level class definitions. :func:`load_setupapi` resolves the DLL
bindings at first call and caches them for the process lifetime.

References:
- design-windows-backend.md §8 (Port discovery).
- MS Learn: SetupDiGetClassDevsW, SetupDiEnumDeviceInterfaces,
  SetupDiGetDeviceInterfaceDetailW, SetupDiGetDeviceRegistryPropertyW,
  SetupDiDestroyDeviceInfoList.
- MS Learn: GUID_DEVINTERFACE_COMPORT =
  {86E0D1E0-8089-11D0-9CE4-08003E301F73}.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import (
    POINTER,
    Structure,
    c_uint8,
    c_uint16,
    c_uint32,
    c_void_p,
    c_wchar,
    sizeof,
)
from typing import Any

from anyserial.exceptions import UnsupportedPlatformError

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# SetupDiGetClassDevs flags.
DIGCF_PRESENT: int = 0x00000002
DIGCF_DEVICEINTERFACE: int = 0x00000010

# SetupDiGetDeviceRegistryPropertyW property keys.
SPDRP_FRIENDLYNAME: int = 0x0000000C
SPDRP_HARDWAREID: int = 0x00000001
SPDRP_LOCATION_INFORMATION: int = 0x0000000D

# Registry data types we expect back from SetupDiGetDeviceRegistryPropertyW.
REG_SZ: int = 1
REG_MULTI_SZ: int = 7

# INVALID_HANDLE_VALUE sentinel for SetupDiGetClassDevs.
INVALID_HANDLE_VALUE: int = -1

# Maximum buffer size for device-interface detail and registry properties.
# 512 wide chars is more than enough for any COM-port path or hardware ID.
_MAX_PATH_WCHARS: int = 512
_MAX_PROPERTY_BYTES: int = 2048


# ---------------------------------------------------------------------------
# GUID
# ---------------------------------------------------------------------------


class GUID(Structure):
    """Win32 GUID layout — 128 bits."""

    _fields_ = (
        ("Data1", c_uint32),
        ("Data2", c_uint16),
        ("Data3", c_uint16),
        ("Data4", c_uint8 * 8),
    )


# {86E0D1E0-8089-11D0-9CE4-08003E301F73}
GUID_DEVINTERFACE_COMPORT = GUID(
    0x86E0D1E0,
    0x8089,
    0x11D0,
    (c_uint8 * 8)(0x9C, 0xE4, 0x08, 0x00, 0x3E, 0x30, 0x1F, 0x73),
)


# ---------------------------------------------------------------------------
# Structures
# ---------------------------------------------------------------------------


# Mirror the SDK struct names verbatim — ctypes fields are consumed by
# SetupAPI functions that key off the C struct layout, and matching the
# SDK names keeps the ``# type: ignore`` surface smaller and makes the
# code diffable against MS Learn samples. Ruff's CapWords convention
# doesn't apply here.
_X64_DETAIL_CB_SIZE = 8
_X86_DETAIL_CB_SIZE = 6
_X64_VOID_P_SIZE = 8


class SP_DEVINFO_DATA(Structure):  # noqa: N801 — Win32 SDK struct name
    """``SP_DEVINFO_DATA`` — per-device information element."""

    _fields_ = (
        ("cbSize", c_uint32),
        ("ClassGuid", GUID),
        ("DevInst", c_uint32),
        ("Reserved", c_void_p),
    )


class SP_DEVICE_INTERFACE_DATA(Structure):  # noqa: N801 — Win32 SDK struct name
    """``SP_DEVICE_INTERFACE_DATA`` — per-interface element."""

    _fields_ = (
        ("cbSize", c_uint32),
        ("InterfaceClassGuid", GUID),
        ("Flags", c_uint32),
        ("Reserved", c_void_p),
    )


class SP_DEVICE_INTERFACE_DETAIL_DATA_W(Structure):  # noqa: N801 — Win32 SDK struct name
    """``SP_DEVICE_INTERFACE_DETAIL_DATA_W`` — variable-length path buffer.

    The ``cbSize`` field is **not** the total allocation size — it is the
    size of the *fixed* portion of the structure. On x64 this is 8 (due
    to alignment of the ``WCHAR[1]`` member after a ``DWORD``); on x86
    it is 6. We compute it from ``sizeof(c_void_p)`` so the same code
    works on both architectures.
    """

    _fields_ = (
        ("cbSize", c_uint32),
        ("DevicePath", c_wchar * _MAX_PATH_WCHARS),
    )


def _detail_cb_size() -> int:
    """Return the correct ``cbSize`` for ``SP_DEVICE_INTERFACE_DETAIL_DATA_W``.

    On x64: ``sizeof(DWORD) + sizeof(WCHAR)`` with alignment →
    ``4 + 2 + 2 (pad)`` = 8. On x86: ``4 + 2`` = 6 (no padding).
    The canonical formula is ``offsetof(DevicePath) + sizeof(WCHAR)``,
    which equals ``sizeof(c_void_p) + sizeof(c_wchar)`` on all Windows
    architectures Python supports.
    """
    if sizeof(c_void_p) == _X64_VOID_P_SIZE:
        return _X64_DETAIL_CB_SIZE
    return _X86_DETAIL_CB_SIZE


DETAIL_CB_SIZE: int = _detail_cb_size()


# ---------------------------------------------------------------------------
# Binding table
# ---------------------------------------------------------------------------


class SetupApiBindings:
    """Resolved setupapi function pointers.

    Populated by :func:`_bind_setupapi`. Follows the same pattern as
    :class:`anyserial._windows._win32.Kernel32Bindings`.
    """

    __slots__ = (
        "SetupDiDestroyDeviceInfoList",
        "SetupDiEnumDeviceInterfaces",
        "SetupDiGetClassDevsW",
        "SetupDiGetDeviceInterfaceDetailW",
        "SetupDiGetDeviceRegistryPropertyW",
    )

    SetupDiDestroyDeviceInfoList: Any
    SetupDiEnumDeviceInterfaces: Any
    SetupDiGetClassDevsW: Any
    SetupDiGetDeviceInterfaceDetailW: Any
    SetupDiGetDeviceRegistryPropertyW: Any


_setupapi_cache: SetupApiBindings | None = None


def load_setupapi() -> SetupApiBindings:
    """Return the lazily-initialised setupapi binding table.

    Raises :class:`UnsupportedPlatformError` when called off-Windows.
    """
    global _setupapi_cache  # noqa: PLW0603 — module-level cache by design
    if _setupapi_cache is not None:
        return _setupapi_cache
    platform = sys.platform
    if platform != "win32" or not hasattr(ctypes, "WinDLL"):
        msg = (
            "anyserial._windows._setupapi.load_setupapi called on "
            f"{platform!r}; SetupAPI bindings are Windows-only"
        )
        raise UnsupportedPlatformError(msg)
    _setupapi_cache = _bind_setupapi()
    return _setupapi_cache


def _bind_setupapi() -> SetupApiBindings:
    setupapi = ctypes.WinDLL("setupapi", use_last_error=True)  # type: ignore[attr-defined]

    bindings = SetupApiBindings()

    # SetupDiGetClassDevsW → HDEVINFO (HANDLE)
    get_devs = setupapi.SetupDiGetClassDevsW
    get_devs.argtypes = [
        POINTER(GUID),  # ClassGuid
        c_void_p,  # Enumerator (NULL)
        c_void_p,  # hwndParent (NULL)
        c_uint32,  # Flags
    ]
    get_devs.restype = c_void_p
    bindings.SetupDiGetClassDevsW = get_devs

    # SetupDiEnumDeviceInterfaces → BOOL
    enum_ifaces = setupapi.SetupDiEnumDeviceInterfaces
    enum_ifaces.argtypes = [
        c_void_p,  # DeviceInfoSet
        c_void_p,  # DeviceInfoData (NULL = all)
        POINTER(GUID),  # InterfaceClassGuid
        c_uint32,  # MemberIndex
        POINTER(SP_DEVICE_INTERFACE_DATA),  # DeviceInterfaceData
    ]
    enum_ifaces.restype = ctypes.c_bool
    bindings.SetupDiEnumDeviceInterfaces = enum_ifaces

    # SetupDiGetDeviceInterfaceDetailW → BOOL
    get_detail = setupapi.SetupDiGetDeviceInterfaceDetailW
    get_detail.argtypes = [
        c_void_p,  # DeviceInfoSet
        POINTER(SP_DEVICE_INTERFACE_DATA),  # DeviceInterfaceData
        c_void_p,  # DeviceInterfaceDetailData (or NULL for size query)
        c_uint32,  # DeviceInterfaceDetailDataSize
        POINTER(c_uint32),  # RequiredSize (out)
        POINTER(SP_DEVINFO_DATA),  # DeviceInfoData (out, optional)
    ]
    get_detail.restype = ctypes.c_bool
    bindings.SetupDiGetDeviceInterfaceDetailW = get_detail

    # SetupDiGetDeviceRegistryPropertyW → BOOL
    get_prop = setupapi.SetupDiGetDeviceRegistryPropertyW
    get_prop.argtypes = [
        c_void_p,  # DeviceInfoSet
        POINTER(SP_DEVINFO_DATA),  # DeviceInfoData
        c_uint32,  # Property
        POINTER(c_uint32),  # PropertyRegDataType (out)
        c_void_p,  # PropertyBuffer
        c_uint32,  # PropertyBufferSize
        POINTER(c_uint32),  # RequiredSize (out, optional)
    ]
    get_prop.restype = ctypes.c_bool
    bindings.SetupDiGetDeviceRegistryPropertyW = get_prop

    # SetupDiDestroyDeviceInfoList → BOOL
    destroy = setupapi.SetupDiDestroyDeviceInfoList
    destroy.argtypes = [c_void_p]
    destroy.restype = ctypes.c_bool
    bindings.SetupDiDestroyDeviceInfoList = destroy

    return bindings


__all__ = [
    "DETAIL_CB_SIZE",
    "DIGCF_DEVICEINTERFACE",
    "DIGCF_PRESENT",
    "GUID",
    "GUID_DEVINTERFACE_COMPORT",
    "INVALID_HANDLE_VALUE",
    "REG_MULTI_SZ",
    "REG_SZ",
    "SPDRP_FRIENDLYNAME",
    "SPDRP_HARDWAREID",
    "SPDRP_LOCATION_INFORMATION",
    "SP_DEVICE_INTERFACE_DATA",
    "SP_DEVICE_INTERFACE_DETAIL_DATA_W",
    "SP_DEVINFO_DATA",
    "SetupApiBindings",
    "load_setupapi",
]
