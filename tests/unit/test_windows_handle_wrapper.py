"""Regression tests for :class:`HandleWrapper` weakref support.

CPython >= 3.12 changed ``IocpProactor._registered`` from a plain ``set``
to a ``weakref.WeakSet`` (Lib/asyncio/windows_events.py).
``_register_with_iocp(obj)`` therefore calls ``weakref.ref(obj)`` under
the hood. ``HandleWrapper`` is slotted, so without ``__weakref__`` in
``__slots__`` that ref construction raises ``TypeError`` and every
``open_serial_port`` call on the asyncio runtime path fails at
registration. These tests pin that behaviour so the slot can never be
silently dropped again.

``HandleWrapper`` itself is plain Python (no Windows-only imports at
module load), so the weakref-support test runs on every platform — that
gives us regression coverage on Linux CI even when no Windows runner is
available. The end-to-end proactor test genuinely needs
``ProactorEventLoop`` and is gated to Windows.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
import weakref
from typing import Any, cast

import pytest

from anyserial._windows._asyncio_io import HandleWrapper


def test_handle_wrapper_supports_weakref() -> None:
    """``weakref.ref(HandleWrapper(...))`` must succeed and behave normally."""
    wrapper = HandleWrapper(0)
    ref = weakref.ref(wrapper)
    assert ref() is wrapper
    del wrapper
    assert ref() is None


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="ProactorEventLoop is Windows-only",
)
def test_handle_wrapper_round_trips_through_iocp_registration() -> None:
    """End-to-end: the same private API ``_asyncio_io.register`` exercises.

    ``asyncio.run`` lands on ``ProactorEventLoop`` on Windows (the default
    since 3.8), so this reaches the real ``_register_with_iocp`` codepath.
    The proactor first does ``self._registered.add(obj)`` (a ``WeakSet``
    on CPython >= 3.12) and *then* calls
    ``_overlapped.CreateIoCompletionPort(obj.fileno(), ...)``. With a
    NULL handle that kernel call fails with ``OSError [WinError 6]`` —
    that is fine and not what we are testing. The regression we pin
    here is that the wrapper makes it past the ``WeakSet.add`` step
    without ``TypeError: cannot create weak reference``.
    """

    async def go() -> None:
        loop = asyncio.get_running_loop()
        # Cast to ``Any`` so both type checkers treat ``_proactor`` and
        # the proactor's underscore-prefixed members as known. The mypy
        # platform pin (Linux) makes ``loop._proactor`` undefined; pyright
        # would otherwise flag every member access as ``Unknown``.
        proactor = cast("Any", loop._proactor)  # type: ignore[attr-defined]
        wrapper = HandleWrapper(0)
        # Expected post-WeakSet kernel failure with a NULL handle:
        # CreateIoCompletionPort raises OSError [WinError 6]. If the
        # WeakSet rejected the wrapper instead, suppress would not catch
        # the resulting TypeError and the test would fail.
        with contextlib.suppress(OSError):
            proactor._register_with_iocp(wrapper)
        assert wrapper in proactor._registered

    asyncio.run(go())
