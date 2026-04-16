"""Tests for :mod:`anyserial._windows._runtime`.

The detection helper is called once per ``WindowsBackend.open`` so its
behaviour matters: a wrong probe sends the hot path to the wrong I/O
module. We exercise the asyncio (Proactor + Selector) and Trio branches,
plus the no-runtime case, all on Linux by directly mocking the loop /
``trio.lowlevel.current_task`` probes.
"""

from __future__ import annotations

import anyio
import anyio.lowlevel
import pytest

from anyserial._windows._runtime import detect_runtime
from anyserial.exceptions import UnsupportedAsyncBackendError, UnsupportedPlatformError

pytestmark = pytest.mark.anyio


class _ProactorLoop:
    """Stand-in for ``ProactorEventLoop`` — only the ``_proactor`` attr matters."""

    _proactor: object = object()


class _SelectorLoop:
    """Stand-in for ``SelectorEventLoop`` — no ``_proactor`` attribute."""


class TestAsyncioBranch:
    def test_proactor_loop_resolves_to_asyncio(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from anyserial._windows import _runtime  # noqa: PLC0415

        monkeypatch.setattr(_runtime, "_running_asyncio_loop", _ProactorLoop)
        monkeypatch.setattr(_runtime, "_trio_is_running", lambda: False)
        assert detect_runtime() == "asyncio"

    def test_selector_loop_raises_unsupported_platform(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from anyserial._windows import _runtime  # noqa: PLC0415

        monkeypatch.setattr(_runtime, "_running_asyncio_loop", _SelectorLoop)
        monkeypatch.setattr(_runtime, "_trio_is_running", lambda: False)
        with pytest.raises(UnsupportedPlatformError, match="ProactorEventLoop"):
            detect_runtime()


class TestTrioBranch:
    def test_trio_running_resolves_to_trio(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from anyserial._windows import _runtime  # noqa: PLC0415

        monkeypatch.setattr(_runtime, "_running_asyncio_loop", lambda: None)
        monkeypatch.setattr(_runtime, "_trio_is_running", lambda: True)
        assert detect_runtime() == "trio"


class TestNoRuntime:
    def test_neither_runtime_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from anyserial._windows import _runtime  # noqa: PLC0415

        monkeypatch.setattr(_runtime, "_running_asyncio_loop", lambda: None)
        monkeypatch.setattr(_runtime, "_trio_is_running", lambda: False)
        with pytest.raises(UnsupportedAsyncBackendError, match="asyncio or trio"):
            detect_runtime()


class TestRealRuntime:
    """Run :func:`detect_runtime` from inside a real async task.

    These don't assert the *value* (Linux asyncio is SelectorEventLoop, so
    the proactor probe legitimately raises), only that the helper picks up
    the running runtime rather than reporting "no runtime detected".
    """

    async def test_under_real_loop_detects_runtime(self) -> None:
        # Either branch is acceptable: asyncio without a Proactor raises
        # UnsupportedPlatformError; Trio returns "trio". Anything else
        # (UnsupportedAsyncBackendError) means we failed to detect a
        # running task — which would be a real bug.
        await anyio.lowlevel.checkpoint()
        try:
            flavour = detect_runtime()
        except UnsupportedPlatformError:
            # asyncio + SelectorEventLoop on Linux — expected.
            return
        assert flavour in {"asyncio", "trio"}
