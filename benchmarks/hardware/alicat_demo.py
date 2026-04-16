"""Exercise anyserial against a live Alicat mass flow controller.

Device: Alicat MFC, firmware 8v17.0-R23, currently in streaming mode on
/dev/ttyUSB0 at 115200-8N1, unit id '@'. This script stops streaming,
runs a handful of commands from the Alicat Serial Primer, then restores
streaming so the device is left how we found it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import anyio

from anyserial import SerialConfig, open_serial_port

if TYPE_CHECKING:
    from anyserial.stream import SerialPort

PORT = "/dev/ttyUSB0"
BAUD = 115_200
UNIT = "A"  # unit id we assign after taking it out of streaming mode


async def drain(port: SerialPort, seconds: float = 0.2) -> bytes:
    """Soak up whatever is already buffered (e.g. stale stream frames)."""
    buf = bytearray()
    with anyio.move_on_after(seconds):
        while True:
            buf.extend(await port.receive(1024))
    return bytes(buf)


async def send_command(
    port: SerialPort,
    cmd: str,
    *,
    timeout: float = 1.0,  # noqa: ASYNC109 — internal helper; applies via move_on_after
    settle: float = 0.05,
) -> str:
    """Send `cmd\\r` and read one line of response.

    Alicat replies end in CR. We read until the reply stops arriving
    (short inter-byte gap), which tolerates frames that span multiple
    receive() calls without requiring BufferedByteStream.
    """
    await port.send(cmd.encode("ascii") + b"\r")
    buf = bytearray()
    deadline_reached = False
    with anyio.move_on_after(timeout):
        while True:
            chunk = await port.receive(256)
            buf.extend(chunk)
            # If we've seen a CR, wait briefly for trailing bytes, then return.
            if b"\r" in buf:
                with anyio.move_on_after(settle):
                    while True:
                        buf.extend(await port.receive(256))
                deadline_reached = True
                break
    _ = deadline_reached
    return buf.decode("ascii", errors="replace").strip()


async def main() -> None:
    cfg = SerialConfig(baudrate=BAUD)
    async with await open_serial_port(PORT, cfg) as port:
        print(f"=== opened {PORT} @ {BAUD} 8N1 ===\n")

        # 1. Device is in streaming mode ('@'). Stop streaming + assign unit id.
        #    Primer: `@@ new_unit_id` leaves the device in polling mode as
        #    new_unit_id.
        print(f"[stop streaming → unit id {UNIT}]")
        # Drain any in-flight frames first, then issue `@@ A`. The space is
        # required (primer p.7); without it the device stays in streaming.
        await drain(port, 0.2)
        await port.send(f"@@ {UNIT}\r".encode("ascii"))
        await anyio.sleep(0.5)
        leftover = await drain(port, 0.5)
        if leftover:
            # last line is probably the confirmation frame
            tail = leftover.decode("ascii", errors="replace").strip().splitlines()[-1]
            print(f"  confirm: {tail!r}\n")

        # 2. Firmware version.
        print("[VE — firmware version]")
        print(f"  {await send_command(port, f'{UNIT}VE')}\n")

        # 3. Manufacturing info (multi-line; increase timeout + settle).
        print("[??M* — manufacturing info]")
        info = await send_command(port, f"{UNIT}??M*", timeout=2.0, settle=0.3)
        for line in info.splitlines():
            print(f"  {line}")
        print()

        # 4. Data-frame layout (so we know what the columns mean).
        print("[??D* — data frame layout]")
        layout = await send_command(port, f"{UNIT}??D*", timeout=2.0, settle=0.3)
        for line in layout.splitlines():
            print(f"  {line}")
        print()

        # 5. Available gases. `GS` (query active gas) was added in 10v05 and
        #    is not understood by 8v17 — sending it there parses as
        #    "G (set gas) <garbage>" and silently changes the active gas.
        #    `??G*` is present in all firmware and is safe.
        print("[??G* — available gases (first 10 lines)]")
        # ~210 gases at 115200 baud; reply runs ~1s. Use a generous timeout
        # and settle so we don't chop it off mid-list.
        gases = await send_command(port, f"{UNIT}??G*", timeout=5.0, settle=0.8)
        gas_lines = gases.splitlines()
        for line in gas_lines[:10]:
            print(f"  {line}")
        print(f"  ... ({len(gas_lines)} total)\n")

        # 6. Set gas back to N2 (gas 8 in Alicat's table) in case we were run
        #    after an earlier script left the device on a different gas.
        #    Drain first — the ??G* tail or an unsolicited frame could otherwise
        #    get concatenated with our command.
        await drain(port, 0.3)
        print("[G 8 — set gas → N2]")
        print(f"  {await send_command(port, f'{UNIT}G 8')}\n")

        # 6. Three polls, ~100ms apart, to show how send/receive composes.
        print("[poll x3]")
        for _ in range(3):
            print(f"  {await send_command(port, UNIT)}")
            await anyio.sleep(0.1)
        print()

        # 7. Tare flow (safe only because we can see setpoint is 0).
        print("[V — tare flow]")
        print(f"  {await send_command(port, f'{UNIT}V')}\n")

        # 8. Restore streaming so the device is left as we found it.
        print("[restore streaming]")
        await port.send(f"{UNIT}@ @\r".encode("ascii"))
        await anyio.sleep(0.2)
        sample = await drain(port, 0.5)
        frames = sample.decode("ascii", errors="replace").strip().splitlines()
        print(f"  {len(frames)} frames received in 0.5s (rate ~{len(frames) * 2}/s)")
        if frames:
            print(f"  sample: {frames[-1]!r}")


if __name__ == "__main__":
    anyio.run(main)
