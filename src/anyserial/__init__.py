"""Low-latency async serial I/O for Python, built on AnyIO.

This module re-exports the package's public surface.
"""

from __future__ import annotations

from anyserial._types import (
    ByteSize,
    BytesLike,
    Capability,
    CommEvent,
    ControlLines,
    ModemLines,
    Parity,
    StopBits,
    UnsupportedPolicy,
)
from anyserial._version import __version__
from anyserial.capabilities import SerialCapabilities, SerialStreamAttribute
from anyserial.config import FlowControl, RS485Config, SerialConfig
from anyserial.discovery import DiscoveryBackend, PortInfo, find_serial_port, list_serial_ports
from anyserial.exceptions import (
    ConfigurationError,
    PortBusyError,
    PortNotFoundError,
    SerialClosedError,
    SerialDisconnectedError,
    SerialError,
    UnsupportedAsyncBackendError,
    UnsupportedConfigurationError,
    UnsupportedFeatureError,
    UnsupportedPlatformError,
)
from anyserial.stream import SerialConnectable, SerialPort, open_serial_port

__all__ = [
    "ByteSize",
    "BytesLike",
    "Capability",
    "CommEvent",
    "ConfigurationError",
    "ControlLines",
    "DiscoveryBackend",
    "FlowControl",
    "ModemLines",
    "Parity",
    "PortBusyError",
    "PortInfo",
    "PortNotFoundError",
    "RS485Config",
    "SerialCapabilities",
    "SerialClosedError",
    "SerialConfig",
    "SerialConnectable",
    "SerialDisconnectedError",
    "SerialError",
    "SerialPort",
    "SerialStreamAttribute",
    "StopBits",
    "UnsupportedAsyncBackendError",
    "UnsupportedConfigurationError",
    "UnsupportedFeatureError",
    "UnsupportedPlatformError",
    "UnsupportedPolicy",
    "__version__",
    "find_serial_port",
    "list_serial_ports",
    "open_serial_port",
]
