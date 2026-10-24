"""Append-only audit log of MCP calls, with secrets redacted (SPEC 16). Fails closed: if the log
cannot be written the call is not executed."""

import hashlib
import json
import os
import re
import time
from pathlib import Path

from .errors import LabError

_SECRET_KEY = re.compile(r"(token|secret|passw|key|authorization|credential)", re.I)
_BULK_KEYS = {"content", "data"}
_PATTERNS = [
    (re.compile(r"tskey-[A-Za-z0-9_-]+"), "[redacted]"),
    (re.compile(r"(?i)\b(password|passwd|token|secret|api[_-]?key)(\s*[=:]\s*)\S+"), r"\1\2[redacted]"),
    (re.compile(r"(?i)(authorization:\s*bearer\s+)\S+"), r"\1[redacted]"),
]


class AuditError(LabError):
    pass


def redact(value, key: str = ""):
    if isinstance(value, dict):
        return {k: redact(v, k) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v, key) for v in value]
    if key in _BULK_KEYS and isinstance(value, str):
        return {"chars": len(value), "sha256": hashlib.sha256(value.encode()).hexdigest()[:16]}
    if _SECRET_KEY.search(key) and value not in (None, "", False, True):
        return "[redacted]"
    if isinstance(value, str):
        for pat, repl in _PATTERNS:
            value = pat.sub(repl, value)
        return value[:2000]
    return value


class AuditLog:
    def __init__(self, path: str, clock=time.time):
        self.path, self.clock = path, clock

    def write(self, **record) -> None:
        record = {"ts": round(self.clock(), 3), **{k: redact(v, k) for k, v in record.items()}}
        line = json.dumps(record, sort_keys=True, default=str) + "\n"
        try:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o640)
            try:
                os.write(fd, line.encode())
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError as e:
            raise AuditError(f"cannot write the audit log {self.path}: {e}; refusing to continue") from e
