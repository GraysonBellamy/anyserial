# pyright: reportPrivateUsage=false
"""Unit tests for the Darwin ``IOSSIOSPEED`` helper.

Hermetic by design — the helper is one ``fcntl.ioctl`` call, which we
monkeypatch so the test runs on Linux CI (and any POSIX host) without
needing a real Darwin kernel. The key invariant: the ioctl request code
and the packed payload exactly match what ``<IOKit/serial/ioss.h>``
prescribes, because sending the wrong bits at a real ``/dev/cu.*`` is
a silent corruption, not a visible error.
"""

from __future__ import annotations

import fcntl
import struct

import pytest

from anyserial._darwin.baudrate import IOSSIOSPEED, set_iossiospeed


class TestConstants:
    def test_iossiospeed_matches_iokit_abi(self) -> None:
        # <IOKit/serial/ioss.h>: IOSSIOSPEED = _IOW('T', 2, speed_t).
        # Decomposed in the module docstring; asserted here as a hard
        # regression guard. pySerial and every other serial stack on
        # macOS relies on this literal.
        assert IOSSIOSPEED == 0x8008_5402

    def test_iossiospeed_decomposition(self) -> None:
        # Re-derive from the _IOW(g, n, t) macro so a future edit that
        # flips a bit gets caught by a clear arithmetic mismatch, not a
        # cryptic hex literal failure.
        ioc_in = 0x8000_0000
        ioc_parm_mask = 0x1FFF
        size = 8  # sizeof(speed_t) on 64-bit Darwin
        group = ord("T")
        number = 2
        expected = ioc_in | ((size & ioc_parm_mask) << 16) | (group << 8) | number
        assert expected == IOSSIOSPEED


class TestSetIossiospeed:
    @pytest.fixture
    def captured(self, monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, int, bytes]]:
        """Capture ``fcntl.ioctl`` invocations as ``(fd, request, payload)`` tuples."""
        calls: list[tuple[int, int, bytes]] = []

        def fake_ioctl(fd: int, request: int, payload: bytes) -> bytes:
            calls.append((fd, request, payload))
            # Real ioctl returns the (possibly modified) buffer; IOSSIOSPEED
            # is _IOW (kernel-reads-only), so echoing the payload is fine.
            return payload

        monkeypatch.setattr(fcntl, "ioctl", fake_ioctl)
        return calls

    def test_passes_correct_request_and_payload(
        self,
        captured: list[tuple[int, int, bytes]],
    ) -> None:
        set_iossiospeed(fd=7, rate=250_000)
        assert len(captured) == 1
        fd, request, payload = captured[0]
        assert fd == 7
        assert request == IOSSIOSPEED
        # 8-byte native-endian unsigned long long; macOS is little-endian
        # on both x86_64 and arm64.
        assert payload == struct.pack("@Q", 250_000)

    def test_accepts_standard_rates(
        self,
        captured: list[tuple[int, int, bytes]],
    ) -> None:
        # The helper is a pure wrapper — it doesn't reject "standard"
        # rates, even though the backend would normally route those
        # through ``tcsetattr``. Keeping the primitive narrow lets
        # tests and future callers compose it freely.
        set_iossiospeed(fd=3, rate=9600)
        (_fd, _req, payload) = captured[0]
        assert struct.unpack("@Q", payload)[0] == 9600

    def test_propagates_oserror(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def failing_ioctl(_fd: int, _request: int, _payload: bytes) -> bytes:
            raise OSError(22, "Invalid argument")  # EINVAL

        monkeypatch.setattr(fcntl, "ioctl", failing_ioctl)

        with pytest.raises(OSError) as excinfo:
            set_iossiospeed(fd=4, rate=1_234_567)
        # Backend layer routes this to UnsupportedConfigurationError via
        # errno_to_exception; the helper itself must not swallow it.
        assert excinfo.value.errno == 22
