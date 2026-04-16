# Cancellation

`SerialPort` composes with AnyIO cancel scopes like every other
`anyio.abc.ByteStream`. Every awaitable method — `receive`, `send`,
`configure`, `drain`, `send_break`, `aclose` — is cancellable, and
cancellation never leaks an open fd or a half-applied termios state.

See [DESIGN §12.3](https://github.com/GraysonBellamy/anyserial/blob/main/DESIGN.md#123-cancellation-and-teardown)
and [§15](https://github.com/GraysonBellamy/anyserial/blob/main/DESIGN.md#15-resource-management-and-teardown)
for the full rationale.

## Timeouts

The canonical timeout is `anyio.move_on_after` (silent) or
`anyio.fail_after` (raising):

```python
import anyio

with anyio.move_on_after(0.5):
    data = await port.receive(64)
else:
    # Scope elapsed without bytes. port is still open and healthy.
    ...


with anyio.fail_after(0.5):
    data = await port.receive(64)
    # TimeoutError if 0.5 s elapses.
```

Both work under `asyncio`, `asyncio` + `uvloop`, and `trio` — AnyIO
normalizes the semantics.

## How cancellation unwinds

Parked reads unpark immediately when the scope fires. The kernel
`os.read` / `os.write` syscalls themselves are always nonblocking;
the blocking wait is `anyio.wait_readable` / `anyio.wait_writable`,
and AnyIO cancels the readiness wait without touching the fd. No
syscall is interrupted mid-flight.

```python
import anyio

async with anyio.create_task_group() as tg:
    tg.start_soon(port.receive, 1024)
    await anyio.sleep(0.1)
    tg.cancel_scope.cancel()
# The receive wakes, raises anyio.Cancelled, and the task-group exits.
```

After cancellation the port is still open. You can call `receive`
again, reconfigure, or close — the state is exactly what it was
before the cancelled call started.

## `aclose` is shielded

`SerialPort.aclose` runs its teardown inside an `anyio.CancelScope(shield=True)`.
That's deliberate: if the caller is being cancelled, the fd and
termios state still have to be reset correctly, and best-effort
restore steps (save/restore `ASYNC_LOW_LATENCY`, RS-485 struct) must
not be dropped because a parent scope fired:

```python
with anyio.move_on_after(0.001):  # absurdly short
    async with await open_serial_port("/dev/ttyUSB0") as port:
        await port.send(b"stuff")
# `async with` exit always completes aclose even though the outer
# scope cancelled almost immediately.
```

If you want aclose itself to honour a timeout, wrap it explicitly —
but this is rare and usually a sign of a bug. The teardown is short:
one `anyio.notify_closing`, one `os.close`, maybe one `tcsetattr` to
restore state.

## Timeouts on the sync wrapper

Sync code uses the `timeout=` keyword on every portal-dispatched
method; the implementation wraps the underlying coroutine in
`anyio.fail_after` inside the portal call:

```python
from anyserial.sync import SerialPort

with SerialPort.open("/dev/ttyUSB0") as port:
    try:
        data = port.receive(64, timeout=0.5)
    except TimeoutError:
        # stdlib TimeoutError — the port is still usable.
        ...
```

The timeout keyword is per-call; there is no process-wide default.
See [Sync wrapper](sync.md#per-call-timeouts).

## `configure` under cancellation

`configure()` takes an `anyio.Lock`. If the scope fires while the
lock is held, the backend call either completed or was never
started; `port.config` is updated only on successful apply, so the
visible state always matches the kernel state:

```python
with anyio.move_on_after(0.1):
    await port.configure(expensive_new_config)

# port.config is unchanged if the scope timed out before the apply.
```

See [Runtime reconfiguration](runtime-reconfiguration.md#cancellation)
for the locking and failure-semantics detail.

## `send_break` is always de-asserted

`send_break(duration)` asserts BREAK, sleeps, and de-asserts. The
de-assert is in a `finally` block, so a cancelled sleep still ends
with BREAK off:

```python
with anyio.move_on_after(0.1):
    await port.send_break(duration=0.25)
# BREAK has been de-asserted regardless of how the scope exited.
```

If the de-assert fails because the port was closed mid-sleep, the
`OSError` is swallowed — the caller sees the original cancellation,
not a spurious teardown error.

## Task groups

`SerialPort` composes cleanly with `anyio.create_task_group`. A
typical duplex pattern — a reader task draining the port while the
main task writes:

```python
import anyio


async def reader(port: SerialPort) -> None:
    while True:
        chunk = await port.receive(1024)
        handle(chunk)


async def main() -> None:
    async with await open_serial_port("/dev/ttyUSB0") as port:
        async with anyio.create_task_group() as tg:
            tg.start_soon(reader, port)
            await port.send(b"query\n")
            await anyio.sleep(1.0)
            tg.cancel_scope.cancel()
```

Concurrent `receive` calls raise `anyio.BusyResourceError` — there's
one receive guard per port. Full-duplex send+receive (one task
sending, one task receiving) is always allowed.

## `notify_closing` and parked waits

`aclose` calls `anyio.notify_closing(fd)` before closing the fd.
That wakes any task parked in `wait_readable` / `wait_writable` with
a `ClosedResourceError` — which `anyserial` translates to
`SerialClosedError` on the next method call. The fd is never closed
while a task is still waiting on it.

## Common footguns

- **Don't wrap `aclose` in a short-lived scope.** It's shielded, so
  the scope will only affect the caller, not the teardown. Usually
  fine, but confusing; rely on `async with` instead.
- **Don't catch `anyio.get_cancelled_exc_class()` and swallow it.**
  Re-raise or let it propagate; otherwise the surrounding scope
  stops functioning.
- **Don't rely on OS-level read timeouts** (`VMIN` / `VTIME`).
  `anyserial` puts termios in raw mode (`VMIN=1, VTIME=0`);
  AnyIO scopes are the timeout mechanism.

## See also

- [AnyIO backend selection](anyio-backends.md) — how cancellation
  behaves across the backend matrix.
- [Runtime reconfiguration](runtime-reconfiguration.md) — the lock,
  the resource guards, cancellation semantics.
- [Sync wrapper](sync.md) — per-call `timeout=` on blocking methods.
