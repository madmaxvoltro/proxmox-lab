"""Command execution seam. Everything that touches the host goes through a Runner,
so dry-run and the fake Proxmox in tests are drop-in replacements."""

import logging
import subprocess
import threading
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
    ("pvesm", "status"), ("pvesm", "list"), ("pvesm", "path"),
    ("tailscale", "whois"), ("tailscale", "status"),
    ("zstd", "-t"),
    ("ip", "-4"), ("ping", "-c"),
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

    def run(self, argv, *, input: str | None = None, check: bool = True, timeout: float | None = None,
            on_line=None) -> Result:
        """`on_line(text)` is called for each line (or carriage-return update) of stdout/stderr
        while the command runs, so slow commands can report progress. Output is still returned."""
        raise NotImplementedError


def _stream(argv, input, timeout, on_line):
    """Like subprocess.run(capture_output=True, text=True) but feeds output lines to on_line live."""
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE if input is not None else None,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    bufs = {"out": [], "err": []}

    def pump(stream, name):
        for line in stream:
            bufs[name].append(line)
            try:
                on_line(line.rstrip("\n"))
            except Exception:  # a broken display must never break the command
                log.debug("on_line callback failed", exc_info=True)

    threads = [threading.Thread(target=pump, args=(proc.stdout, "out"), daemon=True),
               threading.Thread(target=pump, args=(proc.stderr, "err"), daemon=True)]
    for t in threads:
        t.start()
    try:
        if input is not None:
            try:
                proc.stdin.write(input)
            finally:
                proc.stdin.close()
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        raise
    finally:
        for t in threads:
            t.join(timeout=5)
        proc.stdout.close()
        proc.stderr.close()
    return proc.returncode, "".join(bufs["out"]), "".join(bufs["err"])


class SubprocessRunner(Runner):
    def run(self, argv, *, input=None, check=True, timeout=None, on_line=None):
        argv = [str(a) for a in argv]
        log.debug("run %s", argv)
        try:
            if on_line is None:
                proc = subprocess.run(argv, input=input, capture_output=True, text=True, timeout=timeout)
                rc, out, err = proc.returncode, proc.stdout, proc.stderr
            else:
                rc, out, err = _stream(argv, input, timeout, on_line)
        except FileNotFoundError:
            res = Result(argv, 127, "", f"command not found: {argv[0]} (is this the Proxmox host?)")
            if check:
                raise CommandError(argv, 127, res.stderr) from None
            return res
        res = Result(argv, rc, out, err)
        if check and not res.ok:
            raise CommandError(argv, res.returncode, res.stderr)
        return res


class DryRunRunner(Runner):
    """Executes read-only commands for real (discovery is accurate); records and skips the rest."""

    def __init__(self, inner: Runner):
        self.inner = inner
        self.skipped: list = []

    def run(self, argv, *, input=None, check=True, timeout=None, on_line=None):
        argv = [str(a) for a in argv]
        if is_read_only(argv):
            return self.inner.run(argv, input=input, check=check, timeout=timeout, on_line=on_line)
        self.skipped.append(argv)
        log.info("[dry-run] %s", " ".join(argv))
        return Result(argv, 0, "", "")
