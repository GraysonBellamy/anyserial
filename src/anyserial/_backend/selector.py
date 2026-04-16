"""Platform dispatch for :func:`open_serial_port`.

The selector chooses a backend factory based on ``sys.platform``. Linux,
Darwin, and BSD ship :class:`SyncSerialBackend` implementations; Windows
ships an :class:`AsyncSerialBackend`. Every other platform raises
:class:`UnsupportedPlatformError` with a message naming the platform so
the caller has a specific error to grep.

Tests bypass this module entirely by constructing
:class:`anyserial.testing.MockBackend` directly via ``MockBackend.pair()``.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from anyserial.exceptions import UnsupportedPlatformError

if TYPE_CHECKING:
    from anyserial._backend.protocol import AsyncSerialBackend, SyncSerialBackend
    from anyserial.config import SerialConfig


def select_backend(
    path: str,
    config: SerialConfig,
) -> SyncSerialBackend | AsyncSerialBackend:
    """Return an unopened backend matching the current platform.

    Args:
        path: Device path the caller intends to open. Used in error messages
            today; future platform backends can also branch on path shape
            (Windows ``COMn`` vs ``/dev/tty*``).
        config: Requested configuration. Unused today; reserved for
            platform-specific factory hooks (e.g., choosing between
            native and pySerial-backed discovery).

    Returns:
        A backend instance that satisfies :class:`SyncSerialBackend` or
        :class:`AsyncSerialBackend`. The caller (``open_serial_port``) is
        responsible for invoking ``open(path, config)`` on the result.

    Raises:
        UnsupportedPlatformError: No backend is wired up for this platform
            yet. Message includes the path so the error is easy to spot in
            logs where multiple opens fan out.
    """
    platform = sys.platform
    if platform.startswith("linux"):
        from anyserial._linux.backend import LinuxBackend  # noqa: PLC0415 — lazy by platform

        return LinuxBackend()
    if platform == "darwin":
        from anyserial._darwin.backend import DarwinBackend  # noqa: PLC0415 — lazy by platform

        return DarwinBackend()
    if "bsd" in platform or platform.startswith("dragonfly"):
        from anyserial._bsd.backend import BsdBackend  # noqa: PLC0415 — lazy by platform

        return BsdBackend()
    if platform == "win32":
        from anyserial._windows.backend import WindowsBackend  # noqa: PLC0415 — lazy by platform

        return WindowsBackend()
    msg = f"No serial backend available for platform {platform!r}; requested {path!r}"
    raise UnsupportedPlatformError(msg)


__all__ = ["select_backend"]
