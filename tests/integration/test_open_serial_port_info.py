"""Integration test for the discovery → ``port_info`` enrichment path.

Opens a real Linux pty through :func:`open_serial_port` and verifies that
the ``port_info`` typed attribute behaves correctly when discovery cannot
resolve metadata for the path. Pseudo terminals don't appear under
``/sys/class/tty`` (their ``/dev/pts/N`` paths don't map to a tty class
entry), so the lookup must come back empty and the typed-attribute lookup
must raise — proving the open-path orchestration plumbed ``port_info=None``
through correctly without breaking the rest of the typed-attribute set.
"""

from __future__ import annotations

import sys

import anyio
import pytest

if not sys.platform.startswith("linux"):
    pytest.skip("Linux backend only", allow_module_level=True)

from anyio.streams.file import FileStreamAttribute

from anyserial import SerialConfig, SerialStreamAttribute, open_serial_port

pytestmark = pytest.mark.anyio


class TestPortInfoOnOpen:
    async def test_pty_open_yields_no_port_info(self, pty_port: tuple[int, str]) -> None:
        _controller, path = pty_port
        async with await open_serial_port(path, SerialConfig(baudrate=9600)) as port:
            # /dev/pts/N has no /sys/class/tty entry → resolver returns None.
            assert port.port_info is None
            with pytest.raises(anyio.TypedAttributeLookupError):
                port.extra(SerialStreamAttribute.port_info)

    async def test_other_typed_attributes_unaffected(self, pty_port: tuple[int, str]) -> None:
        # Regression guard: omitting port_info must not drop the other keys.
        _controller, path = pty_port
        async with await open_serial_port(path, SerialConfig(baudrate=9600)) as port:
            assert port.extra(FileStreamAttribute.fileno) >= 0
            assert port.extra(SerialStreamAttribute.config).baudrate == 9600
            caps = port.extra(SerialStreamAttribute.capabilities)
            assert caps.backend == "linux"

    async def test_default_sentinel_form_works(self, pty_port: tuple[int, str]) -> None:
        # `extra(key, default)` is the ergonomic way to handle the
        # "discovery couldn't resolve metadata" case without try/except.
        _controller, path = pty_port
        async with await open_serial_port(path, SerialConfig(baudrate=9600)) as port:
            assert port.extra(SerialStreamAttribute.port_info, None) is None
