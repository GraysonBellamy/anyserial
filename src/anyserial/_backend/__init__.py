"""Backend Protocols and platform dispatch.

Public to the rest of the package, private to users. The Protocols are
re-exported here so internal modules can ``from anyserial._backend import
SyncSerialBackend`` without reaching into ``_backend.protocol``.
"""

from __future__ import annotations

from anyserial._backend.protocol import AsyncSerialBackend, SyncSerialBackend
from anyserial._backend.selector import select_backend

__all__ = [
    "AsyncSerialBackend",
    "SyncSerialBackend",
    "select_backend",
]
