"""Thin wrapper over qm / pvesh / pvesm / vzdump / qmrestore.

Discovery uses `pvesh ... --output-format json` where possible instead of parsing
human tables. Methods return plain dicts/lists; nothing here knows about projects."""

import json
import re

from .errors import CommandError, LabError, PreconditionError
from .runner import Runner


class ProxmoxError(LabError):
    pass


def _json(res):
    try:
        return json.loads(res.stdout or "null")
    except json.JSONDecodeError as e:
        raise ProxmoxError(f"unparseable output from {' '.join(res.argv)}: {e}") from e


class Proxmox:
    def __init__(self, runner: Runner):
        self.r = runner

    # discovery (read-only) -------------------------------------------------
    def version(self) -> str:
        out = self.r.run(["pveversion"]).stdout.strip()
        m = re.search(r"pve-manager/([\d.]+)", out)
        return m.group(1) if m else out

    def vms(self) -> list:
        """All VMs and templates on the cluster."""
        res = self.r.run(["pvesh", "get", "/cluster/resources", "--type", "vm", "--output-format", "json"])
        return [v for v in _json(res) if v.get("type") == "qemu"]

    def vm(self, vmid: int) -> dict | None:
        return next((v for v in self.vms() if int(v["vmid"]) == int(vmid)), None)

    def storages(self) -> list:
        res = self.r.run(["pvesh", "get", "/cluster/resources", "--type", "storage", "--output-format", "json"])
        return _json(res)

    def storage_config(self, storage_id: str) -> dict:
        res = self.r.run(["pvesh", "get", f"/storage/{storage_id}", "--output-format", "json"])
        return _json(res)

    def nextid(self, lo: int, hi: int, taken: set = frozenset()) -> int:
        """First free VMID in [lo, hi]: free in Proxmox and not already indexed by lab."""
        used = {int(v["vmid"]) for v in self.vms()} | set(taken)
        for vmid in range(lo, hi + 1):
            if vmid in used:
                continue
            res = self.r.run(["pvesh", "get", "/cluster/nextid", "--vmid", str(vmid)], check=False)
            if res.ok:  # Proxmox also validates against other cluster objects
                return vmid
        raise PreconditionError(f"no free VMID in range {lo}-{hi}")

    def status(self, vmid: int) -> str:
        out = self.r.run(["qm", "status", str(vmid)]).stdout
        m = re.search(r"status:\s*(\S+)", out)
        if not m:
            raise ProxmoxError(f"cannot parse status of {vmid}: {out!r}")
        return m.group(1)

    def config(self, vmid: int) -> dict:
        out = self.r.run(["qm", "config", str(vmid)]).stdout
        cfg = {}
        for line in out.splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                cfg[k.strip()] = v.strip()
        return cfg

    def backups(self, storage_id: str, vmid: int | None = None) -> list:
        argv = ["pvesm", "list", storage_id, "--content", "backup"]
        if vmid is not None:
            argv += ["--vmid", str(vmid)]
        argv += ["--output-format", "json"]
        return _json(self.r.run(argv))

    # mutating --------------------------------------------------------------
    def clone(self, template: int, vmid: int, name: str, *, full: bool, storage: str | None = None) -> None:
        argv = ["qm", "clone", str(template), str(vmid), "--name", name]
        if full:
            argv += ["--full", "1"]
            if storage:
                argv += ["--storage", storage]
        self.r.run(argv)

    def set(self, vmid: int, **opts) -> None:
        """qm set with key=value options; `delete` takes a list of keys to remove."""
        argv = ["qm", "set", str(vmid)]
        for k, v in opts.items():
            argv += [f"--{k}", str(v)]
        self.r.run(argv)

    def resize(self, vmid: int, disk: str, size: str) -> None:
        self.r.run(["qm", "resize", str(vmid), disk, size])

    def start(self, vmid: int) -> None:
        self.r.run(["qm", "start", str(vmid)])

    def shutdown(self, vmid: int, timeout: int = 120) -> None:
        """Graceful only. Never escalates to a forced stop; the caller decides."""
        self.r.run(["qm", "shutdown", str(vmid), "--timeout", str(timeout)])

    def stop(self, vmid: int) -> None:
        self.r.run(["qm", "stop", str(vmid)])

    def destroy(self, vmid: int) -> None:
        self.r.run(["qm", "destroy", str(vmid), "--purge", "1", "--destroy-unreferenced-disks", "1"])

    def vzdump(self, vmid: int, storage: str, *, mode: str = "stop", compress: str = "zstd") -> str:
        """Back up one VM; returns the archive volume ID parsed from vzdump's output."""
        res = self.r.run(["vzdump", str(vmid), "--storage", storage, "--mode", mode, "--compress", compress])
        text = res.stdout + res.stderr
        m = re.search(r"creating vzdump archive '([^']+)'", text) or re.search(r"archive file: (\S+)", text)
        if not m:
            raise ProxmoxError("vzdump succeeded but no archive path was reported")
        return m.group(1)

    def restore(self, volid: str, vmid: int, storage: str) -> None:
        self.r.run(["qmrestore", volid, str(vmid), "--storage", storage])

    def guest_exec(self, vmid: int, argv, *, stdin: str | None = None, check: bool = True):
        """Run a command in the guest via the QEMU guest agent. `stdin` is never logged."""
        cmd = ["qm", "guest", "exec", str(vmid)]
        if stdin is not None:
            cmd.append("--pass-stdin")
        cmd += ["--", *[str(a) for a in argv]]
        res = self.r.run(cmd, input=stdin, check=check)
        try:
            return json.loads(res.stdout)  # {"exitcode": n, "out-data": "...", "err-data": "..."}
        except json.JSONDecodeError:
            return {"exitcode": res.returncode, "out-data": res.stdout, "err-data": res.stderr}


__all__ = ["Proxmox", "ProxmoxError", "CommandError"]
