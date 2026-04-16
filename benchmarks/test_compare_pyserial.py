"""Head-to-head comparison with ``pyserial-asyncio``.

Skips entirely unless ``pyserial-asyncio`` is installed (no extra is
declared for it — install ``pyserial-asyncio`` separately when running
the comparison locally). Exists so the JSON output of one ``pytest
benchmarks/`` invocation surfaces both libraries' numbers side-by-side
for the docs/performance.md report DESIGN §28.6 calls for.

The comparison covers the same single-byte receive-latency scenario as
:mod:`test_roundtrip_latency`, so a like-for-like delta is one column
subtraction in the JSON.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pytest_benchmark.fixture import BenchmarkFixture

# Skip the whole module until pyserial-asyncio is present. The submodule
# names hint at the import path the wrapper exposes — split across two
# importorskip calls because either being absent should skip cleanly.
serial = pytest.importorskip("serial")
serial_asyncio = pytest.importorskip("serial_asyncio")


_BAUD = 115_200


async def _one_byte(reader: asyncio.StreamReader, controller: int) -> None:
    os.write(controller, b"x")
    data = await reader.readexactly(1)
    assert data == b"x"


async def _setup_pyserial_asyncio(path: str) -> tuple[asyncio.StreamReader, object]:
    reader, writer = await serial_asyncio.open_serial_connection(url=path, baudrate=_BAUD)
    return reader, writer


def test_pyserial_asyncio_receive_latency_1byte_pty(
    benchmark: BenchmarkFixture,
    pty_pair: tuple[int, str],
) -> None:
    # pyserial-asyncio is asyncio-only; no point parametrizing across
    # backends. Run the loop on stock asyncio for an honest comparison.
    controller, path = pty_pair
    loop = asyncio.new_event_loop()
    try:
        reader, writer = loop.run_until_complete(_setup_pyserial_asyncio(path))
        try:
            # Warm-up.
            loop.run_until_complete(_one_byte(reader, controller))

            def _iter() -> None:
                loop.run_until_complete(_one_byte(reader, controller))

            benchmark.pedantic(_iter, rounds=200, iterations=5, warmup_rounds=2)
        finally:
            writer.close()  # type: ignore[attr-defined]
            with contextlib.suppress(Exception):
                loop.run_until_complete(writer.wait_closed())  # type: ignore[attr-defined]
    finally:
        loop.close()
