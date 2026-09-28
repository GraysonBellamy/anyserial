"""Property-based tests for :func:`canonical_port_name`.

The Windows rules are pure string operations, so Hypothesis checks them on
every host: the result is its own canonical name, device prefixes and
letter case never change it, and it never carries a prefix.
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from anyserial import canonical_port_name

_PREFIXES = ("\\\\.\\", "\\\\?\\")

# Port-like names: COM ports, com0com-style names, and device-interface
# fragments, plus the characters that make up the prefixes themselves.
_NAME = st.text(
    alphabet="COMcom0123456789#&{}-_ABCDEFabcdef\\.?",
    max_size=24,
)
_PREFIX = st.sampled_from(_PREFIXES)


@given(name=_NAME)
def test_windows_result_is_its_own_canonical_name(name: str) -> None:
    canonical = canonical_port_name(name, platform="win32")
    assert canonical_port_name(canonical, platform="win32") == canonical


@given(name=_NAME, prefix=_PREFIX)
def test_windows_device_prefix_does_not_change_the_result(name: str, prefix: str) -> None:
    assert canonical_port_name(prefix + name, platform="win32") == canonical_port_name(
        name, platform="win32"
    )


@given(name=_NAME)
def test_windows_letter_case_does_not_change_the_result(name: str) -> None:
    expected = canonical_port_name(name, platform="win32")
    assert canonical_port_name(name.lower(), platform="win32") == expected
    assert canonical_port_name(name.upper(), platform="win32") == expected


@given(name=_NAME)
def test_windows_result_has_no_device_prefix(name: str) -> None:
    assert not canonical_port_name(name, platform="win32").startswith(_PREFIXES)


@given(number=st.integers(min_value=1, max_value=256), prefix=st.sampled_from(("", *_PREFIXES)))
def test_windows_com_ports_become_upper_case_com_names(number: int, prefix: str) -> None:
    assert canonical_port_name(f"{prefix}com{number}", platform="win32") == f"COM{number}"
