"""Running things inside project VMs through the QEMU guest agent (SPEC 16, Q8) as a mixin for Lab.

Everything here targets a VM that lab verified is the project's own (`_live_vm`); nothing runs on
the Proxmox host. Commands run as the project owner unless `root=True` (callers gate that)."""

import base64
import json
import re
import secrets
import shlex

from .errors import LabError, PreconditionError, UsageError

JOB_DIR = "/var/tmp/lab-jobs"
_JOB_RE = re.compile(r"^[0-9a-f]{12}$")

# Background jobs: detached from the guest-agent call, output and exit code kept in files.
_INNER = ('f=$1; if [ "$2" = root ]; then bash -lc "$4" >"$f.out" 2>"$f.err"; '
          'else runuser -l -s /bin/bash -c "$4" "$3" >"$f.out" 2>"$f.err"; fi; echo $? >"$f.rc"')
_START = ('d=' + JOB_DIR + '; mkdir -p "$d"; chmod 700 "$d"; '
          "nohup setsid bash -c '" + _INNER + "' lab-inner \"$d/$1\" \"$2\" \"$3\" \"$4\" "
          '</dev/null >/dev/null 2>&1 & echo $! >"$d/$1.pid"')
_POLL = ('f=' + JOB_DIR + '/$1; [ -e "$f.pid" ] || { echo NOJOB; exit 3; }; '
         'if [ -e "$f.rc" ]; then echo "RC $(cat "$f.rc")"; else echo RUNNING; fi; '
         'echo ---OUT; tail -c "$2" "$f.out" 2>/dev/null; echo; echo ---ERR; tail -c "$2" "$f.err" 2>/dev/null')
_CANCEL = ('f=' + JOB_DIR + '/$1; [ -e "$f.pid" ] || { echo NOJOB; exit 3; }; '
           '[ -e "$f.rc" ] && { echo DONE; exit 0; }; kill -TERM -- "-$(cat "$f.pid")" 2>/dev/null; '
           'echo 143 >"$f.rc"; echo CANCELLED')


def _cap(text, limit: int):
    text = text or ""
    if len(text.encode("utf-8", "replace")) <= limit:
        return text, False
    return text.encode("utf-8", "replace")[:limit].decode("utf-8", "ignore") + "\n[output truncated]", True


def check_path(path: str) -> str:
    if not path or "\x00" in path or len(path) > 4096:
        raise UsageError("invalid path")
    return path


class GuestOps:
    # plumbing ----------------------------------------------------------------------
    def _target(self, name: str):
        """Project row, only if its VM is verified and running."""
        row = self._mine(name)
        if row["state"] != "running":
            raise PreconditionError(f"{name} is {row['state']}; start it first")
        vm = self._live_vm(row)
        if vm is None or vm["status"] != "running":
            raise PreconditionError(f"{name} is not running in Proxmox; run `lab doctor`")
        return row

    @staticmethod
    def _argv(row, cmd: str, root: bool) -> list:
        if root:
            return ["bash", "-lc", cmd]
        return ["runuser", "-l", "-s", "/bin/bash", "-c", cmd, row["owner"]]

    def _run(self, row, cmd: str, *, root: bool, timeout: int, stdin: str | None = None) -> dict:
        return self.px.guest_exec(row["vmid"], self._argv(row, cmd, root), stdin=stdin, check=False,
                                  timeout=timeout)

    def _limit(self) -> int:
        return self.cfg.mcp.max_output_bytes

    @staticmethod
    def _timeout(value) -> int:
        if not isinstance(value, int) or not 1 <= value <= 3600:
            raise UsageError("timeout must be between 1 and 3600 seconds")
        return value

    # exec ---------------------------------------------------------------------------
    def exec(self, name: str, cmd: str, *, root: bool = False, timeout: int = 60, cwd: str | None = None) -> dict:
        if not cmd or not cmd.strip():
            raise UsageError("empty command")
        timeout = self._timeout(timeout)
        row = self._target(name)
        if cwd:
            cmd = f"cd -- {shlex.quote(check_path(cwd))} && {cmd}"
        res = self._run(row, cmd, root=root, timeout=timeout)
        out, t1 = _cap(res.get("out-data"), self._limit())
        err, t2 = _cap(res.get("err-data"), self._limit())
        timed_out = res.get("exited") == 0 and "exitcode" not in res
        return {"exit_code": res.get("exitcode"), "stdout": out, "stderr": err,
                "truncated": t1 or t2, "timed_out": timed_out}

    def exec_background(self, name: str, cmd: str, *, root: bool = False, cwd: str | None = None) -> dict:
        if not cmd or not cmd.strip():
            raise UsageError("empty command")
        row = self._target(name)
        if cwd:
            cmd = f"cd -- {shlex.quote(check_path(cwd))} && {cmd}"
        job = secrets.token_hex(6)
        res = self.px.guest_exec(row["vmid"], ["bash", "-c", _START, "lab-job", job, "root" if root else "user",
                                               row["owner"], cmd], check=False, timeout=30)
        if res.get("exitcode", 1) != 0:
            raise LabError("could not start the background job: " + (res.get("err-data") or "").strip()[-200:])
        return {"job": job}

    def _job(self, job: str) -> str:
        if not isinstance(job, str) or not _JOB_RE.match(job):
            raise UsageError("invalid job id")
        return job

    def exec_poll(self, name: str, job: str) -> dict:
        job = self._job(job)
        row = self._target(name)
        res = self.px.guest_exec(row["vmid"], ["bash", "-c", _POLL, "lab-poll", job, str(self._limit())],
                                 check=False, timeout=30)
        text = res.get("out-data") or ""
        if res.get("exitcode") == 3 or text.startswith("NOJOB"):
            raise PreconditionError(f"no such job: {job}")
        head, _, rest = text.partition("\n---OUT\n")
        out, _, err = rest.partition("\n---ERR\n")
        m = re.match(r"RC (\d+)", head)
        out, t1 = _cap(out.rstrip("\n"), self._limit())
        err, t2 = _cap(err.rstrip("\n"), self._limit())
        return {"job": job, "running": m is None, "exit_code": int(m.group(1)) if m else None,
                "stdout": out, "stderr": err, "truncated": t1 or t2}

    def exec_cancel(self, name: str, job: str) -> dict:
        job = self._job(job)
        row = self._target(name)
        res = self.px.guest_exec(row["vmid"], ["bash", "-c", _CANCEL, "lab-cancel", job], check=False, timeout=30)
        text = (res.get("out-data") or "").strip()
        if res.get("exitcode") == 3 or text == "NOJOB":
            raise PreconditionError(f"no such job: {job}")
        return {"job": job, "result": text.lower() or "unknown"}

    # files --------------------------------------------------------------------------
    def read_file(self, name: str, path: str, *, root: bool = False, max_bytes: int | None = None) -> dict:
        check_path(path)
        limit = min(max_bytes or self.cfg.mcp.max_file_bytes, self.cfg.mcp.max_file_bytes)
        row = self._target(name)
        q = shlex.quote(path)
        cmd = (f'p={q}; [ -f "$p" ] || {{ echo "not a regular file: $p" >&2; exit 2; }}; '
               f'stat -c %s -- "$p"; head -c {limit} -- "$p" | base64 -w0')
        res = self._run(row, cmd, root=root, timeout=60)
        if res.get("exitcode", 1) != 0:
            raise PreconditionError((res.get("err-data") or "cannot read file").strip()[-200:])
        size_s, _, b64 = (res.get("out-data") or "").partition("\n")
        try:
            data = base64.b64decode(b64.strip())
            size = int(size_s)
        except (ValueError, TypeError) as e:
            raise LabError("unexpected reply while reading the file") from e
        out = {"path": path, "size": size, "truncated": size > len(data)}
        try:
            out.update(encoding="utf-8", content=data.decode("utf-8"))
        except UnicodeDecodeError:
            out.update(encoding="base64", content=base64.b64encode(data).decode())
        return out

    def write_file(self, name: str, path: str, content: str, *, base64_encoded: bool = False,
                   root: bool = False) -> dict:
        check_path(path)
        try:
            data = base64.b64decode(content, validate=True) if base64_encoded else content.encode("utf-8")
        except ValueError as e:
            raise UsageError("content is not valid base64") from e
        if len(data) > self.cfg.mcp.max_file_bytes:
            raise UsageError(f"file too large for this tool (limit {self.cfg.mcp.max_file_bytes} bytes); "
                             "use `lab put` or scp over Tailscale")
        row = self._target(name)
        q = shlex.quote(path)
        cmd = f'mkdir -p -- "$(dirname -- {q})" && base64 -d > {q}.lab-tmp && mv -f -- {q}.lab-tmp {q}'
        res = self._run(row, cmd, root=root, timeout=120, stdin=base64.b64encode(data).decode())
        if res.get("exitcode", 1) != 0:
            raise PreconditionError((res.get("err-data") or "cannot write file").strip()[-200:])
        return {"path": path, "bytes": len(data)}

    # logs ---------------------------------------------------------------------------
    def logs(self, name: str, which: str = "ops", lines: int = 50) -> dict:
        if which not in ("ops", "setup") or not isinstance(lines, int) or not 1 <= lines <= 500:
            raise UsageError("which must be 'ops' or 'setup'; lines 1-500")
        if which == "ops":
            row = self._mine(name)
            rows = self.db.list_journal(row["name"], lines)
            return {"ops": [{"op": r["op"], "status": r["status"], "step": r["step"], "actor": r["actor"],
                             "started_at": r["started_at"], "detail": r["detail"]} for r in rows]}
        row = self._target(name)
        res = self._run(row, f"tail -n {lines} /var/log/cloud-init-output.log", root=True, timeout=30)
        text, truncated = _cap(res.get("out-data"), self._limit())
        return {"setup_log": text, "truncated": truncated}
