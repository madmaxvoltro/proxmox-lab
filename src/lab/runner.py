"""Command execution seam. Everything that touches the host goes through a Runner,
so dry-run and the fake Proxmox in tests are drop-in replacements."""

import logging
import subprocess
from dataclasses import dataclass

from .errors import CommandError

log = logging.getLogger("lab.runner")


@dataclass
class Result:
    argv: list
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0


# (command, subcommand) pairs that never change host state. Anything not listed is
# treated as mutating, so a new command is dry-run-safe by default.
_READ_ONLY = {
    ("qm", "list"), ("qm", "status"), ("qm", "config"), ("qm", "listsnapshot"),
    ("pvesm", "status"), ("pvesm", "list"),
    ("pvesh", "get"),
    ("pveversion", None),
}


def _key(argv):
    cmd = argv[0].rsplit("/", 1)[-1]
    if (cmd, None) in _READ_ONLY:
        return (cmd, None)
    return (cmd, argv[1] if len(argv) > 1 else None)


def is_read_only(argv) -> bool:
    return _key(argv) in _READ_ONLY


class Runner:
    """Base interface. `input` is passed on stdin and is never logged (used for secrets)."""

    def run(self, argv, *, input: str | None = None, check: bool = True, timeout: float | None = None) -> Result:
        raise NotImplementedError


class SubprocessRunner(Runner):
    def run(self, argv, *, input=None, check=True, timeout=None):
        argv = [str(a) for a in argv]
        log.debug("run %s", argv)
        proc = subprocess.run(argv, input=input, capture_output=True, text=True, timeout=timeout)
        res = Result(argv, proc.returncode, proc.stdout, proc.stderr)
        if check and not res.ok:
            raise CommandError(argv, res.returncode, res.stderr)
        return res


class DryRunRunner(Runner):
    """Executes read-only commands for real (discovery is accurate); records and skips the rest."""

    def __init__(self, inner: Runner):
        self.inner = inner
        self.skipped: list = []

    def run(self, argv, *, input=None, check=True, timeout=None):
        argv = [str(a) for a in argv]
        if is_read_only(argv):
            return self.inner.run(argv, input=input, check=check, timeout=timeout)
        self.skipped.append(argv)
        log.info("[dry-run] %s", " ".join(argv))
        return Result(argv, 0, "", "")
