"""Public test helpers.

Import :class:`MockBackend`, :class:`FaultPlan`, and :func:`serial_port_pair`
from here in test suites (inside ``anyserial`` and in downstream packages).
The ``_mock`` subpackage is private and may be restructured between releases.
"""

from __future__ import annotations

from anyserial._mock import FaultPlan, MockBackend
from anyserial.config import SerialConfig
from anyserial.stream import SerialPort


def serial_port_pair(
    *,
    config_a: SerialConfig | None = None,
    config_b: SerialConfig | None = None,
    path_a: str = "/dev/mockA",
    path_b: str = "/dev/mockB",
) -> tuple[SerialPort, SerialPort]:
    """Return two connected :class:`SerialPort` instances for tests.

    Each side is backed by a :class:`MockBackend`; bytes written to one
    are available to :meth:`SerialPort.receive` on the other. Close both
    ends in a ``try`` / ``finally`` (or ``async with``) to release the
    underlying sockets.

    Args:
        config_a: Config applied to the A side. Defaults to
            :class:`SerialConfig()`.
        config_b: Config applied to the B side. Defaults to
            :class:`SerialConfig()`.
        path_a: Path string reported by the A-side backend.
        path_b: Path string reported by the B-side backend.
    """
    mock_a, mock_b = MockBackend.pair(path_a=path_a, path_b=path_b)
    cfg_a = config_a if config_a is not None else SerialConfig()
    cfg_b = config_b if config_b is not None else SerialConfig()
    mock_a.open(path_a, cfg_a)
    mock_b.open(path_b, cfg_b)
    return SerialPort(mock_a, cfg_a), SerialPort(mock_b, cfg_b)


__all__ = ["FaultPlan", "MockBackend", "serial_port_pair"]
