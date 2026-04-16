# AnyIO backend selection

`anyserial` is built on [AnyIO](https://anyio.readthedocs.io/), so the
same code runs unchanged on `asyncio` (default), `asyncio` + `uvloop`,
or `trio`. This page covers picking a backend, running the test suite
across the full matrix, and the two or three places where the choice
actually matters in serial-I/O code.

See [DESIGN §29.1](https://github.com/GraysonBellamy/anyserial/blob/main/DESIGN.md#291-anyio-backend-support)
for the backend-support policy.

## The matrix

| Backend | Loop | Default? | When to pick it |
|---|---|---|---|
| `asyncio` (default) | CPython `selector_events` (POSIX) / `proactor_events` (Windows) | Yes | Interop with the rest of the Python async ecosystem |
| `asyncio` + `uvloop` | libuv | No | Lowest latency on POSIX; see [uvloop](uvloop.md). **POSIX only** — uvloop doesn't build on Windows. |
| `trio` | Trio scheduler | No | Strong cancellation / nursery semantics; test under `trio` at minimum |

All three are exercised in CI against every Linux / macOS integration
test. The Windows matrix is asyncio (Proactor) and trio only — see
[Windows backend](#windows-backend) below. Performance numbers live
in [Performance](performance.md).

## Running under a specific backend

AnyIO's `anyio.run` takes a `backend=` argument:

```python
import anyio

anyio.run(main)                            # asyncio (default)
anyio.run(main, backend="asyncio")
anyio.run(main, backend="trio")
```

For uvloop the trick is a `backend_options` flag:

```python
anyio.run(main, backend="asyncio", backend_options={"use_uvloop": True})
```

That flips the loop factory to uvloop for the duration of the run.
See [uvloop usage](uvloop.md) for installation and caveats.

## Inside pytest

The AnyIO pytest plugin ships inside `anyio` itself — no
`pytest-anyio` install needed. Mark an async test and parametrize
across the matrix:

```python
import pytest


@pytest.fixture(
    params=[
        pytest.param("asyncio", id="asyncio"),
        pytest.param(
            ("asyncio", {"use_uvloop": True}),
            id="uvloop",
            marks=pytest.mark.skipif(
                "sys.platform == 'win32'",
                reason="uvloop is POSIX-only",
            ),
        ),
        pytest.param("trio", id="trio"),
    ]
)
def anyio_backend(request: pytest.FixtureRequest) -> object:
    return request.param


@pytest.mark.anyio
async def test_echo() -> None:
    ...
```

The `anyio_backend` fixture is the AnyIO plugin's single convention;
every `@pytest.mark.anyio` test runs once per parametrized value.

## Where the backend choice actually matters

Most of `anyserial`'s surface is backend-agnostic —
`anyio.wait_readable`, `anyio.wait_writable`, `anyio.Lock`,
`anyio.CancelScope` — and behaves identically across the matrix. Two
places do differ:

**Cancellation**. All three backends honour `anyio.move_on_after` /
`anyio.fail_after`. Trio is stricter about checkpoint frequency and
will sometimes surface a cancellation one syscall earlier than
asyncio. `SerialPort.aclose` shields its teardown on every backend —
cancellation never leaks an open fd. See
[Cancellation](cancellation.md) for the guarantees.

**Wakeup latency**. On a busy loop, `asyncio`'s default selector can
show up to ~1 ms of tail latency that `uvloop` and `trio` trim to
the tens-of-µs range. For a UART at 115 200 baud this is invisible;
for 2 ms Modbus RTT budgets it matters. [Performance](performance.md)
publishes measured numbers per backend.

## Windows backend

Windows is the one place the backend choice is actually *constrained*.
`anyserial`'s `WindowsBackend` dispatches through each runtime's
native IOCP machinery:

- **asyncio** — must run on `WindowsProactorEventLoopPolicy` (the
  default since Python 3.8). `WindowsSelectorEventLoopPolicy` raises
  `UnsupportedPlatformError` at open time.
- **trio** — fully supported; uses `trio.lowlevel.register_with_iocp`.
- **asyncio + uvloop** — n/a; uvloop doesn't build on Windows.
- **winloop** (uvloop fork for Windows) — exposes a proactor-like
  surface but is untested; treated as unsupported.

If you've explicitly overridden the event-loop policy:

```python
# Breaks on Windows:
asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# Required for anyserial on Windows:
asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
```

The Proactor policy has been the default for six+ years, so most
Windows code doesn't touch this. If your framework (e.g. older aiohttp
versions, certain test runners) forces the selector loop, you'll need
to either unwind that or run `anyserial` in a separate process on the
proactor loop.

See [Windows / Supported runtimes](windows.md#supported-runtimes) for
the full story and [design-windows-backend.md §1](https://github.com/GraysonBellamy/anyserial/blob/main/docs/design-windows-backend.md)
for why there's no worker-thread fallback.

## Picking a default for your app

Rule of thumb:

- **Libraries**: don't pick. Your callers pick by calling `anyio.run`
  with whatever they prefer. Write code that works under all three.
- **Applications**: start with `asyncio`. Add `uvloop` when you can
  show the wakeup-latency tail in [Performance](performance.md)
  matters for your workload. Pick `trio` when you want its
  nursery-based cancellation model (or because the rest of your
  stack is already on Trio).
- **Test suites**: run against all three in CI. Regressions that
  appear on only one backend are almost always cancellation bugs,
  and those are the bugs worth catching early.

## Trio-on-asyncio and vice versa

AnyIO supports running Trio-style APIs on an asyncio loop and vice
versa via its compatibility shims. `anyserial` does **not** commit to
the cross-runtime configurations — see
[DESIGN §35 open #5](https://github.com/GraysonBellamy/anyserial/blob/main/DESIGN.md#35-open-design-decisions).
Stick to the three native configurations above.

## Sync wrapper and the backend

The sync wrapper runs its own event loop on a background thread via
`anyio.from_thread.BlockingPortalProvider`. That loop is `asyncio`
by default; call `configure_portal` before the first open to switch:

```python
from anyserial.sync import configure_portal, SerialPort

configure_portal(backend="asyncio", backend_options={"use_uvloop": True})

with SerialPort.open("/dev/ttyUSB0") as port:
    ...
```

See [Sync wrapper](sync.md#choosing-the-anyio-backend) for the full
shape.

## See also

- [uvloop usage](uvloop.md) — install, caveats, measured wins.
- [Cancellation](cancellation.md) — `CancelScope`, `aclose`
  shielding, timeouts.
- [Performance](performance.md) — measured numbers per backend.
