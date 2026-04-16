"""Unit tests for the BSD integer-passthrough baud helper.

The helper is a trivial identity function, but it carries a contract
(":mod:`anyserial._bsd.baudrate` hands the rate straight to termios")
that the Linux and Darwin equivalents do not, so a dedicated test pins
the behaviour against a future refactor that forgets it.
"""

from __future__ import annotations

import pytest

from anyserial._bsd.baudrate import passthrough_rate


class TestPassthroughRate:
    @pytest.mark.parametrize(
        "rate",
        [
            9600,
            115200,
            230_400,
            250_000,  # non-standard on every POSIX we target
            1_000_000,
            1,  # degenerate but still something the kernel can reject cleanly
        ],
    )
    def test_identity(self, rate: int) -> None:
        # The helper exists so the backend's custom-baud path reads
        # symmetrically with Linux's mark_bother / Darwin's IOSSIOSPEED.
        # A silent bug that coerced rates would mean every non-standard
        # baud on BSD talks at the wrong speed.
        assert passthrough_rate(rate) == rate
