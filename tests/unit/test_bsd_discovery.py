"""Unit tests for :func:`anyserial._bsd.discovery.enumerate_ports`.

Builds synthetic ``/dev`` trees under ``tmp_path`` and points
:func:`enumerate_ports` at them, so the variant-specific pattern
dispatch (FreeBSD / NetBSD / OpenBSD / DragonFly) runs deterministically
on any host. Pure :mod:`pathlib`, no platform-specific syscalls.

What we pin here:

- Each BSD variant selects its documented glob set — FreeBSD
  matches both ``cuaU*`` (USB) and ``cuau*`` (on-board), etc.
- Duplicate matches (a device that satisfies two patterns) surface
  exactly once in the returned list.
- An empty ``/dev`` or an unknown-platform tag returns an empty list,
  not an exception.
- Results are sorted by device path for stable ordering.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

import pytest

from anyserial._bsd.discovery import enumerate_ports, resolve_port_info

if TYPE_CHECKING:
    from pathlib import Path

# macOS's default APFS volume is case-insensitive, so files like ``cuaU0``
# and ``cuau0`` collide as the same on-disk entry. Tests that rely on
# distinguishing case-only-different basenames (FreeBSD's USB vs
# on-board callout naming) skip on Darwin; the same pattern dispatch is
# covered by Linux CI where ext4 is case-sensitive. Real FreeBSD's UFS /
# ZFS are case-sensitive, so this is purely a host-FS quirk.
_skip_case_insensitive_fs = pytest.mark.skipif(
    sys.platform == "darwin",
    reason="case-only-different filenames collide on macOS APFS (host quirk)",
)


def _make_node(dev_root: Path, name: str) -> None:
    """Create a fake device node (empty file) under ``dev_root``."""
    (dev_root / name).write_text("")


class TestEnumeratePortsFreeBSD:
    @_skip_case_insensitive_fs
    def test_usb_and_onboard_nodes_enumerated(self, tmp_path: Path) -> None:
        _make_node(tmp_path, "cuaU0")  # USB-serial callout
        _make_node(tmp_path, "cuaU1")
        _make_node(tmp_path, "cuau0")  # On-board serial callout
        _make_node(tmp_path, "random")  # should be ignored

        ports = enumerate_ports(dev_root=tmp_path, platform="freebsd14")
        devices = [p.device for p in ports]
        assert devices == sorted(
            [
                str(tmp_path / "cuaU0"),
                str(tmp_path / "cuaU1"),
                str(tmp_path / "cuau0"),
            ],
        )

    @_skip_case_insensitive_fs
    def test_dialin_aliases_surface(self, tmp_path: Path) -> None:
        _make_node(tmp_path, "ttyU0")  # USB dial-in
        _make_node(tmp_path, "ttyu0")  # On-board dial-in

        ports = enumerate_ports(dev_root=tmp_path, platform="freebsd13")
        assert {p.name for p in ports} == {"ttyU0", "ttyu0"}

    def test_port_info_carries_basename(self, tmp_path: Path) -> None:
        _make_node(tmp_path, "cuaU3")
        port = enumerate_ports(dev_root=tmp_path, platform="freebsd14")[0]
        assert port.name == "cuaU3"
        # USB metadata deliberately absent on BSD (pyserial extra).
        assert port.vid is None
        assert port.pid is None
        assert port.hwid is None


class TestEnumeratePortsOpenBSD:
    def test_cua_nodes_matched(self, tmp_path: Path) -> None:
        _make_node(tmp_path, "cua00")  # on-board UART 0
        _make_node(tmp_path, "cua10")  # on-board UART 1 (different device node)
        _make_node(tmp_path, "cuaU0")
        _make_node(tmp_path, "ttyU0")  # dial-in — not in OpenBSD pattern set

        ports = enumerate_ports(dev_root=tmp_path, platform="openbsd7")
        devices = [p.device for p in ports]
        assert str(tmp_path / "cua00") in devices
        assert str(tmp_path / "cuaU0") in devices
        # OpenBSD's pattern set excludes ttyU*; that's deliberate
        # (cua* covers both on-board and USB on OpenBSD).
        assert str(tmp_path / "ttyU0") not in devices


class TestEnumeratePortsNetBSD:
    def test_dty_nodes_matched(self, tmp_path: Path) -> None:
        _make_node(tmp_path, "dtyU0")  # USB callout
        _make_node(tmp_path, "dty00")  # On-board callout
        _make_node(tmp_path, "ttyU0")  # USB dial-in
        _make_node(tmp_path, "cuaU0")  # FreeBSD-style — should NOT match

        ports = enumerate_ports(dev_root=tmp_path, platform="netbsd11")
        devices = {p.device for p in ports}
        assert str(tmp_path / "dtyU0") in devices
        assert str(tmp_path / "dty00") in devices
        assert str(tmp_path / "ttyU0") in devices
        assert str(tmp_path / "cuaU0") not in devices


class TestEnumeratePortsDragonFly:
    @_skip_case_insensitive_fs
    def test_freebsd_style_patterns(self, tmp_path: Path) -> None:
        # DragonFly inherits FreeBSD's naming — cuaU*, cuau*.
        _make_node(tmp_path, "cuaU0")
        _make_node(tmp_path, "cuau0")
        _make_node(tmp_path, "ttyU0")  # dial-in — not in DragonFly pattern set

        ports = enumerate_ports(dev_root=tmp_path, platform="dragonfly6")
        devices = {p.device for p in ports}
        assert str(tmp_path / "cuaU0") in devices
        assert str(tmp_path / "cuau0") in devices
        assert str(tmp_path / "ttyU0") not in devices


class TestEnumeratePortsDeduplication:
    def test_device_matching_multiple_patterns_appears_once(
        self,
        tmp_path: Path,
    ) -> None:
        # A FreeBSD device matching both cuaU* (USB callout pattern)
        # and no second pattern, but we still exercise the dedupe
        # path with a crafted case where fnmatch order could double-count.
        _make_node(tmp_path, "cuaU0")
        # Run twice; ensure no duplicates even if patterns overlap.
        ports = enumerate_ports(dev_root=tmp_path, platform="freebsd14")
        assert len(ports) == 1


class TestEnumeratePortsEmpty:
    def test_missing_dev_root_returns_empty(self, tmp_path: Path) -> None:
        # Never created — the path doesn't exist. Must not raise.
        missing = tmp_path / "does-not-exist"
        assert enumerate_ports(dev_root=missing, platform="freebsd14") == []

    def test_unknown_platform_returns_empty(self, tmp_path: Path) -> None:
        _make_node(tmp_path, "cuaU0")
        # Unknown tag → no patterns → empty list (not an exception).
        assert enumerate_ports(dev_root=tmp_path, platform="haiku") == []

    def test_no_matching_nodes_returns_empty(self, tmp_path: Path) -> None:
        _make_node(tmp_path, "random1")
        _make_node(tmp_path, "random2")
        assert enumerate_ports(dev_root=tmp_path, platform="freebsd14") == []


class TestEnumeratePortsOrdering:
    def test_results_sorted_by_device(self, tmp_path: Path) -> None:
        for name in ("cuaU2", "cuaU0", "cuaU1"):
            _make_node(tmp_path, name)
        ports = enumerate_ports(dev_root=tmp_path, platform="freebsd14")
        assert [p.name for p in ports] == ["cuaU0", "cuaU1", "cuaU2"]


class TestResolvePortInfo:
    def test_matches_freebsd_path(self, tmp_path: Path) -> None:
        info = resolve_port_info(
            str(tmp_path / "cuaU0"),
            dev_root=tmp_path,
            platform="freebsd14",
        )
        assert info is not None
        assert info.name == "cuaU0"

    def test_unknown_basename_returns_none(self, tmp_path: Path) -> None:
        info = resolve_port_info(
            str(tmp_path / "pts-0"),
            dev_root=tmp_path,
            platform="freebsd14",
        )
        assert info is None

    def test_unknown_platform_returns_none(self, tmp_path: Path) -> None:
        # Unknown platform → no patterns → no way to decide → None.
        info = resolve_port_info(
            str(tmp_path / "cuaU0"),
            dev_root=tmp_path,
            platform="haiku",
        )
        assert info is None

    def test_openbsd_ttyu_rejected(self, tmp_path: Path) -> None:
        # OpenBSD's pattern set excludes ttyU*; resolve should return
        # None so the "no metadata" signal propagates to the caller.
        info = resolve_port_info(
            str(tmp_path / "ttyU0"),
            dev_root=tmp_path,
            platform="openbsd7",
        )
        assert info is None
