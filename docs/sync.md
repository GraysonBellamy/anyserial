# Sync wrapper

`anyserial` is async-first. The `anyserial.sync.SerialPort` class is a thin
blocking wrapper around [`anyserial.SerialPort`](index.md) for scripts and
test benches that do not want to run an event loop themselves.

!!! note "Async-first is the promise"
    The sync wrapper shares 100 % of its I/O code with the async port —
    every blocking call is implemented as
    `portal.call(async_method, ...)`. Performance characteristics track
    the async path (see [Performance](performance.md)); behavioural
    semantics are identical.

## Quickstart

```python
from anyserial.sync import SerialPort

with SerialPort.open("/dev/ttyUSB0", baudrate=115200) as port:
    port.send(b"ping\n")
    reply = port.receive(1024, timeout=1.0)
```

Every method that blocks on I/O accepts an optional `timeout=` keyword;
snapshot-style calls (`input_waiting`, `output_waiting`, property
accesses) do not dispatch through the portal and return immediately.

## How it works

Every sync port in the process shares one
[`anyio.from_thread.BlockingPortalProvider`][provider], which owns a
single background thread running an AnyIO event loop. The provider is
refcounted — the first `SerialPort.open(...)` spawns the event-loop
thread; the last `close()` tears it down. **Opening multiple sync ports
does not spawn multiple event-loop threads.**

Each sync method forwards to its async counterpart via
`portal.call(coroutine, *args)`; optional timeouts wrap the coroutine in
`anyio.fail_after`.

```text
       ┌──────────────────── caller thread(s) ─────────────────────┐
       │  port.send(b"x", timeout=1.0)                             │
       └────────────────┬───────────────────────┬──────────────────┘
                        │ portal.call           │ portal.call
                        ▼                       ▼
       ┌────────── portal event-loop thread (one) ─────────────────┐
       │  async_port.send(b"x")  ←─ AnyIO coroutine                │
       │  anyio.wait_writable(fd) / os.write(fd, ...)              │
       └───────────────────────────────────────────────────────────┘
```

## Choosing the AnyIO backend

The default is plain `asyncio`. To use `uvloop`, `winloop`, or Trio, call
[`anyserial.sync.configure_portal`][configure_portal] **before** opening
the first sync port:

```python
from anyserial.sync import configure_portal, SerialPort

configure_portal(backend="asyncio", backend_options={"use_uvloop": True})

with SerialPort.open("/dev/ttyUSB0") as port:
    ...
```

`configure_portal` raises `RuntimeError` if called after the portal
thread has started.

## Per-call timeouts

Timeouts are per-call keyword arguments on every blocking method.
`TimeoutError` (the stdlib one, which AnyIO's `fail_after` raises
directly) surfaces unchanged when they fire; the port stays usable for
subsequent calls.

```python
try:
    data = port.receive(64, timeout=0.5)
except TimeoutError:
    ...  # port is still open and healthy
```

Some methods take both a behavioural-timing parameter and the
portal-level timeout — for example `send_break(duration=0.25,
timeout=1.0)`: `duration` is how long the BREAK condition is asserted,
`timeout` bounds the whole call at the portal.

## Concurrency

The portal's event loop is single-threaded, so calls from multiple OS
threads dispatch through one serialization point. Full-duplex send+
receive from different threads works cleanly; concurrent `send` calls
from multiple threads are *not* guaranteed to serialize — the async
port's `ResourceGuard` can still fire (`anyio.BusyResourceError`) if two
coroutines enter the same guarded section. Coordinate writes from a
single thread if you need ordering guarantees.

## Context managers and lifecycle

`SerialPort` supports the stdlib `with` protocol and is idempotently
closeable:

```python
port = SerialPort.open("/dev/ttyUSB0")
port.close()
port.close()  # no-op
```

Leaking an open port triggers a `ResourceWarning` during garbage
collection, matching the async port's contract. Always close explicitly
or use `with`.

## `SerialConnectable`

The deferred-open data class mirrors [`anyserial.SerialConnectable`][async-connectable] but
does *not* implement `anyio.abc.ByteStreamConnectable` — that Protocol
requires an async `connect`, which is incompatible with sync call sites.
Sync code that needs AnyIO connectable polymorphism should use the async
variant.

```python
from anyserial.sync import SerialConnectable

recipe = SerialConnectable(path="/dev/ttyUSB0", config=SerialConfig(baudrate=115200))
with recipe.connect() as port:
    ...
```

## API parity

Every non-lifecycle method and property on the async `SerialPort` has a
sync counterpart with a matching signature (minus `async`, plus an
optional `timeout=` on portal-dispatched methods). The parity is
regression-tested in `tests/unit/test_sync_parity.py`.

The only intentional differences:

| Async                      | Sync                             |
|----------------------------|----------------------------------|
| `async def aclose()`       | `def close(*, timeout=None)`     |
| `async def __aenter__ / __aexit__` | `def __enter__ / __exit__` |
| N/A                        | `def open(cls, path, config=None, /, *, timeout=None, **fields)` |

## When to use which

| Situation                              | Recommendation                         |
|----------------------------------------|----------------------------------------|
| Real app with an event loop            | Async `anyserial.SerialPort`           |
| Jupyter notebook exploration           | Sync wrapper                           |
| Hardware-test bench, one-shot scripts  | Sync wrapper                           |
| Integrating into an AnyIO task group   | Async `anyserial.SerialPort`           |
| Library API that callers compose with  | Async (don't force an event loop)      |

The sync wrapper is a convenience; code that expects to grow into an
async application should start with the async API.

[provider]: https://anyio.readthedocs.io/en/stable/api.html#anyio.from_thread.BlockingPortalProvider
[configure_portal]: #choosing-the-anyio-backend
[async-connectable]: https://github.com/GraysonBellamy/anyserial/blob/main/src/anyserial/stream.py
