"""Size parsing and project-name rules."""

import re

from .errors import UsageError

_SIZE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([KMGT]?)i?B?\s*$", re.IGNORECASE)
_MULT = {"": 1, "K": 1 << 10, "M": 1 << 20, "G": 1 << 30, "T": 1 << 40}

_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,30}[a-z0-9]$|^[a-z]$")
HOSTNAME_PREFIX = "lab-"


def parse_size(text: str) -> int:
    """'2G', '2048M', '32GiB' -> bytes. A bare number is bytes."""
    m = _SIZE_RE.match(str(text))
    if not m:
        raise UsageError(f"invalid size: {text!r}")
    return int(float(m.group(1)) * _MULT[m.group(2).upper()])


def to_mib(text: str) -> int:
    return parse_size(text) // (1 << 20)


def to_gib(text: str) -> int:
    return parse_size(text) // (1 << 30)


def validate_name(name: str) -> str:
    if not _NAME_RE.match(name):
        raise UsageError(
            f"invalid project name {name!r}: use lowercase letters, digits and "
            "'-', start with a letter, max 32 characters"
        )
    return name


def hostname(name: str) -> str:
    return HOSTNAME_PREFIX + validate_name(name)
