# uvloop

[uvloop](https://github.com/MagicStack/uvloop) is a libuv-based drop-in
replacement for the stdlib `asyncio` event loop. On POSIX it typically
trims 20–30% off per-wakeup latency for serial I/O on top of the
stdlib selector — see [Performance](performance.md) for measured
numbers.

## Install

```bash
uv add "anyserial[uvloop]"
# or
pip install "anyserial[uvloop]"
```

The `uvloop` extra pins `uvloop>=0.22.1` and is declared
`platform_system != 'Windows'`, so it's a no-op on Windows
installs.

## Activating it

uvloop is an `asyncio` loop implementation — the AnyIO backend name
stays `"asyncio"`, you just flip the uvloop option:

```python
import anyio


async def main() -> None:
    ...


anyio.run(main, backend="asyncio", backend_options={"use_uvloop": True})
```

That's the whole story for async code.

For the sync wrapper, configure the process-wide portal once before
the first open:

```python
from anyserial.sync import configure_portal, SerialPort

configure_portal(backend="asyncio", backend_options={"use_uvloop": True})

with SerialPort.open("/dev/ttyUSB0") as port:
    ...
```

See [Sync wrapper](sync.md#choosing-the-anyio-backend) for the
configure-before-first-open constraint.

## When uvloop helps

Measured on Linux pty loops (single-byte p50 receive):

| Backend | Receive p50 | Receive max |
|---|---|---|
| asyncio + uvloop | **99 µs** | 509 µs |
| asyncio (default) | 126 µs | 961 µs |
| trio | 124 µs | 781 µs |

The median gap is ~20–30 µs; the **tail** gap is 2–3×. uvloop's
wakeup scheduler is what moves the numbers — serial-I/O throughput
itself is bound by the wire and the driver.

Use uvloop when any of these apply:

- You're below ~500 µs round-trip budget on pty or loopback.
- You're running many ports concurrently (the fan-out test in
  [Performance](performance.md) shows sub-linear scale under uvloop).
- You need predictable tail latency under an otherwise-busy loop.

Skip it when:

- You're already under budget on stock asyncio.
- Your application pulls in trio — pick trio directly; uvloop is
  asyncio-only.
- You're on Windows — uvloop doesn't build there. Use `winloop` (an
  uvloop fork) via `pip install "anyserial[winloop]"` and the same
  `use_uvloop=True` switch; Windows coverage is experimental.

## Caveats

- **uvloop sometimes regresses.** It's tuned for sockets; serial fds
  use the same `wait_readable` / `wait_writable` plumbing but the
  win isn't automatic. Benchmark your workload.
- **Debugging.** Stack traces through uvloop sometimes lose the
  `asyncio` frames you'd expect. Switch back to stock asyncio
  temporarily when chasing an obscure traceback.
- **Loop policy.** uvloop installs a custom asyncio loop policy. If
  your codebase also manipulates the policy (`asyncio.set_event_loop_policy`),
  let uvloop go first — or let AnyIO handle it end-to-end.
- **Version floor.** 0.22 or newer. Earlier releases had AnyIO 4.x
  compatibility gaps.

## Checking at runtime

```python
import asyncio

loop = asyncio.get_running_loop()
print(type(loop).__module__)  # 'uvloop.loop' when uvloop is active
```

For use inside `anyserial` code, prefer the backend-agnostic
`anyio.get_current_task` / cancel-scope APIs — don't branch on loop
type.

## See also

- [AnyIO backend selection](anyio-backends.md) — the full backend matrix.
- [Performance](performance.md) — side-by-side numbers.
- [Linux tuning](linux-tuning.md#low-latency-mode) — kernel-side
  low-latency knob that compounds with uvloop.
