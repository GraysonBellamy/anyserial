"""Property-based tests for :class:`PortInfo` and :func:`find_serial_port`.

Hypothesis generates arbitrary port lists and filter combinations to check
the AND-filter invariant that example-based unit tests can only spot-check.
The selector is monkeypatched per example so the tests never touch real
sysfs state.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, NamedTuple

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from anyserial import PortInfo, discovery, find_serial_port, list_serial_ports

if TYPE_CHECKING:
    from collections.abc import Callable

# Each Hypothesis example installs its own stub via monkeypatch — we need the
# fixture to NOT reset between examples (the next example overwrites it
# anyway). Hypothesis warns by default; this is the documented escape hatch.
_HYP = settings(suppress_health_check=[HealthCheck.function_scoped_fixture])

# AnyIO matrix gating; Hypothesis runs many examples per test, so we
# deliberately avoid additional parametrization to keep wall time sane.
pytestmark = pytest.mark.anyio


# ----------------------------------------------------------------------
# Strategies
# ----------------------------------------------------------------------

# 16-bit USB IDs. ``None`` is a separate branch — see _port_info_strategy.
_USB_ID = st.integers(min_value=0, max_value=0xFFFF)

# Device paths drawn from a small alphabet so equality comparisons are
# meaningful (a fully arbitrary 4 KB string doesn't add coverage).
_DEVICE_PATH = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789/",
    min_size=1,
    max_size=24,
).map(lambda s: f"/dev/{s}")

_SHORT_TEXT = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_:.",
    min_size=1,
    max_size=24,
)


@st.composite
def _port_info_strategy(draw: st.DrawFn) -> PortInfo:
    """Build a :class:`PortInfo` with each optional field independently nullable."""
    return PortInfo(
        device=draw(_DEVICE_PATH),
        name=draw(st.one_of(st.none(), _SHORT_TEXT)),
        description=draw(st.one_of(st.none(), _SHORT_TEXT)),
        hwid=draw(st.one_of(st.none(), _SHORT_TEXT)),
        vid=draw(st.one_of(st.none(), _USB_ID)),
        pid=draw(st.one_of(st.none(), _USB_ID)),
        serial_number=draw(st.one_of(st.none(), _SHORT_TEXT)),
        manufacturer=draw(st.one_of(st.none(), _SHORT_TEXT)),
        product=draw(st.one_of(st.none(), _SHORT_TEXT)),
        location=draw(st.one_of(st.none(), _SHORT_TEXT)),
        interface=draw(st.one_of(st.none(), _SHORT_TEXT)),
    )


_PORT_LIST = st.lists(_port_info_strategy(), min_size=0, max_size=8)


class _Filter(NamedTuple):
    vid: int | None
    pid: int | None
    serial_number: str | None
    device: str | None


@st.composite
def _filter_strategy(draw: st.DrawFn) -> _Filter:
    """Build a ``find_serial_port`` filter combination with each field nullable."""
    return _Filter(
        vid=draw(st.one_of(st.none(), _USB_ID)),
        pid=draw(st.one_of(st.none(), _USB_ID)),
        serial_number=draw(st.one_of(st.none(), _SHORT_TEXT)),
        device=draw(st.one_of(st.none(), _DEVICE_PATH)),
    )


def _matches(port: PortInfo, f: _Filter) -> bool:
    """Reference oracle: does ``port`` satisfy every supplied filter?"""
    return (
        (f.vid is None or port.vid == f.vid)
        and (f.pid is None or port.pid == f.pid)
        and (f.serial_number is None or port.serial_number == f.serial_number)
        and (f.device is None or port.device == f.device)
    )


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _install_stub(
    monkeypatch: pytest.MonkeyPatch, ports: list[PortInfo]
) -> Callable[[], list[PortInfo]]:
    """Replace the discovery selector with a fixed enumerator returning ``ports``."""

    def _enumerate() -> list[PortInfo]:
        return list(ports)

    monkeypatch.setattr(discovery, "_select_discovery", lambda backend="native": _enumerate)
    return _enumerate


# ----------------------------------------------------------------------
# PortInfo invariants
# ----------------------------------------------------------------------


class TestPortInfoInvariants:
    @given(port=_port_info_strategy())
    def test_equal_ports_have_equal_hashes(self, port: PortInfo) -> None:
        # Frozen+slots means by-value equality, and equal values must hash equal.
        twin = dataclasses.replace(port)
        assert port == twin
        assert hash(port) == hash(twin)

    @given(port=_port_info_strategy(), new_device=_DEVICE_PATH)
    def test_replace_changes_only_targeted_field(self, port: PortInfo, new_device: str) -> None:
        replaced = dataclasses.replace(port, device=new_device)
        assert replaced.device == new_device
        for field in dataclasses.fields(PortInfo):
            if field.name == "device":
                continue
            assert getattr(replaced, field.name) == getattr(port, field.name)

    @given(port=_port_info_strategy())
    def test_port_info_is_set_compatible(self, port: PortInfo) -> None:
        # Hashable + by-value equality means deduplication via set works.
        assert {port, port} == {port}


# ----------------------------------------------------------------------
# find_serial_port filter behaviour
# ----------------------------------------------------------------------


class TestFindSerialPortFilters:
    @_HYP
    @given(ports=_PORT_LIST, flt=_filter_strategy())
    async def test_returns_first_matching_port_or_none(
        self,
        monkeypatch: pytest.MonkeyPatch,
        ports: list[PortInfo],
        flt: _Filter,
    ) -> None:
        _install_stub(monkeypatch, ports)
        result = await find_serial_port(
            vid=flt.vid,
            pid=flt.pid,
            serial_number=flt.serial_number,
            device=flt.device,
        )
        expected = next((p for p in ports if _matches(p, flt)), None)
        assert result == expected

    @_HYP
    @given(ports=_PORT_LIST, flt=_filter_strategy())
    async def test_result_satisfies_all_filters_when_present(
        self,
        monkeypatch: pytest.MonkeyPatch,
        ports: list[PortInfo],
        flt: _Filter,
    ) -> None:
        _install_stub(monkeypatch, ports)
        result = await find_serial_port(
            vid=flt.vid,
            pid=flt.pid,
            serial_number=flt.serial_number,
            device=flt.device,
        )
        if result is not None:
            assert _matches(result, flt)

    @_HYP
    @given(ports=_PORT_LIST.filter(bool))
    async def test_no_filters_returns_first_port(
        self,
        monkeypatch: pytest.MonkeyPatch,
        ports: list[PortInfo],
    ) -> None:
        # Unrestricted query == "give me the first port the platform reports".
        _install_stub(monkeypatch, ports)
        assert await find_serial_port() == ports[0]

    @_HYP
    @given(port=_port_info_strategy(), padding=_PORT_LIST)
    async def test_lookup_by_device_round_trips(
        self,
        monkeypatch: pytest.MonkeyPatch,
        port: PortInfo,
        padding: list[PortInfo],
    ) -> None:
        # Insert ``port`` somewhere in a noisy enumeration; lookup by its
        # exact device path must come back equal (even if duplicates exist
        # in ``padding``, since first-match wins).
        ports = [*padding, port]
        _install_stub(monkeypatch, ports)
        result = await find_serial_port(device=port.device)
        assert result is not None
        assert result.device == port.device


class TestListSerialPortsContract:
    @_HYP
    @given(ports=_PORT_LIST)
    async def test_returns_caller_supplied_ports_in_order(
        self,
        monkeypatch: pytest.MonkeyPatch,
        ports: list[PortInfo],
    ) -> None:
        _install_stub(monkeypatch, ports)
        assert await list_serial_ports() == ports

    @_HYP
    @given(ports=_PORT_LIST)
    async def test_returns_fresh_list_each_call(
        self,
        monkeypatch: pytest.MonkeyPatch,
        ports: list[PortInfo],
    ) -> None:
        _install_stub(monkeypatch, ports)
        first = await list_serial_ports()
        second = await list_serial_ports()
        # Same content, distinct object identities — protects callers that
        # mutate the returned list.
        assert first == second
        assert first is not second
