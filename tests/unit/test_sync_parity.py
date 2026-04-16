"""API-parity reflection between async and sync :class:`SerialPort`.

The sync wrapper must expose a sync counterpart for every non-dunder
public method and property on the async class, with a compatible
signature (minus ``async``, plus an optional ``timeout`` on methods that
dispatch through the portal). This file enforces the async/sync API
parity contract.

Methods and properties that are intentionally *not* mirrored are listed
in :data:`_ASYNC_ONLY` with a justification.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from anyserial import SerialPort as AsyncSerialPort
from anyserial.sync import SerialPort as SyncSerialPort

# Members that exist on the async port but deliberately aren't mirrored
# on the sync side — e.g. async-context-manager protocol hooks, which
# are replaced by the sync context-manager protocol.
_ASYNC_ONLY: frozenset[str] = frozenset({"__aenter__", "__aexit__", "aclose"})

# Async methods whose sync counterpart renames them.
_RENAMED: dict[str, str] = {
    "aclose": "close",
}

# Members that dispatch through the portal and therefore gain an optional
# ``timeout`` keyword on the sync side.
_PORTAL_METHODS: frozenset[str] = frozenset(
    {
        "receive",
        "receive_into",
        "receive_available",
        "send",
        "send_buffer",
        "send_eof",
        "configure",
        "reset_input_buffer",
        "reset_output_buffer",
        "drain",
        "drain_exact",
        "send_break",
        "modem_lines",
        "set_control_lines",
        "close",
    }
)


def _public_members(cls: type) -> dict[str, Any]:
    members: dict[str, Any] = {}
    for name in dir(cls):
        if name.startswith("_"):
            continue
        members[name] = inspect.getattr_static(cls, name)
    return members


def _expected_sync_name(async_name: str) -> str:
    return _RENAMED.get(async_name, async_name)


@pytest.mark.parametrize(
    "async_name",
    sorted(n for n in _public_members(AsyncSerialPort) if n not in _ASYNC_ONLY),
)
def test_sync_has_member_for_every_async_member(async_name: str) -> None:
    sync_name = _expected_sync_name(async_name)
    assert hasattr(SyncSerialPort, sync_name), (
        f"sync SerialPort missing {sync_name!r} (async counterpart: {async_name!r})"
    )


@pytest.mark.parametrize(
    "async_name",
    sorted(n for n in _public_members(AsyncSerialPort) if n not in _ASYNC_ONLY),
)
def test_kind_matches(async_name: str) -> None:
    """Properties stay properties; methods stay methods."""
    sync_name = _expected_sync_name(async_name)
    async_member = inspect.getattr_static(AsyncSerialPort, async_name)
    sync_member = inspect.getattr_static(SyncSerialPort, sync_name)
    if isinstance(async_member, property):
        assert isinstance(sync_member, property), (
            f"{async_name!r} is a property on async; {sync_name!r} must also be a property on sync"
        )
    else:
        # Callables on both sides; sync must be non-coroutine. Unwrap
        # classmethods / staticmethods through getattr on the class
        # itself to get the bound descriptor.
        sync_callable = getattr(SyncSerialPort, sync_name)
        assert callable(sync_callable)
        assert not inspect.iscoroutinefunction(sync_callable), (
            f"sync {sync_name!r} must not be a coroutine function"
        )


@pytest.mark.parametrize(
    "method_name",
    sorted(
        n
        for n in _public_members(AsyncSerialPort)
        if n not in _ASYNC_ONLY
        and callable(inspect.getattr_static(AsyncSerialPort, n))
        and not isinstance(inspect.getattr_static(AsyncSerialPort, n), property)
    ),
)
def test_signature_parity(method_name: str) -> None:
    """Sync signature matches async modulo ``async`` and an optional ``timeout``.

    For methods that dispatch through the portal, the sync signature
    must include a keyword-only ``timeout: float | None = None``.
    Non-portal helpers (snapshots) must have an exactly identical
    parameter list.
    """
    sync_name = _expected_sync_name(method_name)
    async_sig = inspect.signature(getattr(AsyncSerialPort, method_name))
    sync_sig = inspect.signature(getattr(SyncSerialPort, sync_name))

    async_params = list(async_sig.parameters.values())
    sync_params = list(sync_sig.parameters.values())

    if sync_name in _PORTAL_METHODS:
        # Sync must have every async param plus ``timeout`` at the end.
        # ``self`` is present on both sides.
        assert "timeout" in sync_sig.parameters, f"sync {sync_name!r} must accept `timeout=`"
        timeout_param = sync_sig.parameters["timeout"]
        assert timeout_param.kind is inspect.Parameter.KEYWORD_ONLY
        assert timeout_param.default is None
        # Non-timeout params in sync must match async param list 1:1.
        sync_params_no_timeout = [p for p in sync_params if p.name != "timeout"]
        assert [p.name for p in sync_params_no_timeout] == [p.name for p in async_params], (
            f"param names differ for {method_name!r}: "
            f"async={[p.name for p in async_params]} "
            f"sync(minus timeout)={[p.name for p in sync_params_no_timeout]}"
        )
    else:
        assert [p.name for p in sync_params] == [p.name for p in async_params]


def test_no_unexpected_extras_on_sync() -> None:
    """Every public name on the sync class corresponds to something real.

    Either (a) shares a name with an async public member, or (b) is one
    of a small set of sync-only additions (``open``, ``close``, context
    hooks).
    """
    async_names = set(_public_members(AsyncSerialPort))
    sync_only_allowed = {"open", "close"}
    renamed_targets = set(_RENAMED.values())
    for name in _public_members(SyncSerialPort):
        assert name in async_names or name in sync_only_allowed or name in renamed_targets, (
            f"unexpected public member on sync SerialPort: {name!r}"
        )
