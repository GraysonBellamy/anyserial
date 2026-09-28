"""Public test helpers.

Import :class:`MockBackend`, :class:`FaultPlan`, :func:`serial_port_pair`
and :func:`faults_of` from here in test suites (inside ``anyserial`` and in
downstream packages). The ``_mock`` subpackage is private and may be
restructured between releases.

Fault injection drives the failure paths of a mock-backed port without
real hardware::

    host, device = serial_port_pair()
    faults_of(device).disconnected = True  # device reads EOF, writes EPIPE
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
    underlying sockets. Use :func:`faults_of` to inject faults on either
    side.

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


def faults_of(port: SerialPort) -> FaultPlan:
    """Return the live :class:`FaultPlan` of a port backed by :class:`MockBackend`.

    The plan is the backend's own, not a copy: setting a field changes how
    the port's next reads and writes fail, and a counter such as
    :attr:`FaultPlan.eagain_reads` counts down as the port uses it. Works
    for the ports :func:`serial_port_pair` returns and for any
    ``SerialPort(backend, config)`` built on a :class:`MockBackend`.

    Args:
        port: A port whose backend is a :class:`MockBackend`.

    Returns:
        The backend's fault plan.

    Raises:
        TypeError: ``port`` is backed by something other than
            :class:`MockBackend`, such as a real device.
    """
    backend = port._backend  # pyright: ignore[reportPrivateUsage]
    if not isinstance(backend, MockBackend):
        msg = (
            f"faults_of() needs a port backed by MockBackend; "
            f"{port.path!r} uses {type(backend).__name__}"
        )
        raise TypeError(msg)
    return backend.faults


__all__ = ["FaultPlan", "MockBackend", "faults_of", "serial_port_pair"]
