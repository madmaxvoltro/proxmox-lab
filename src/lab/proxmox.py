"""Thin wrapper over qm / pvesh / pvesm / vzdump / qmrestore.

Discovery uses `pvesh ... --output-format json` where possible instead of parsing
human tables. Methods return plain dicts/lists; nothing here knows about projects."""

import json
import re
import time

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

    def node(self) -> str:
        nodes = _json(self.r.run(["pvesh", "get", "/nodes", "--output-format", "json"]))
        if not nodes:
            raise ProxmoxError("no Proxmox node found")
        return nodes[0]["node"]

    def bridges(self) -> list:
        res = self.r.run(["pvesh", "get", f"/nodes/{self.node()}/network", "--output-format", "json"])
        return sorted(n["iface"] for n in _json(res) if n.get("type") in ("bridge", "OVSBridge"))

    def storage_path(self, volid: str) -> str:
        """Filesystem path for a volume ID, e.g. 'local:snippets/x.yaml'."""
        return self.r.run(["pvesm", "path", volid]).stdout.strip()

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
        # `pvesm list` has no JSON output; the API behind pvesh does.
        argv = ["pvesh", "get", f"/nodes/{self.node()}/storage/{storage_id}/content", "--content", "backup"]
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

    def create(self, vmid: int, **opts) -> None:
        argv = ["qm", "create", str(vmid)]
        for k, v in opts.items():
            argv += [f"--{k}", str(v)]
        self.r.run(argv)

    def to_template(self, vmid: int) -> None:
        self.r.run(["qm", "template", str(vmid)])

    def agent_ready(self, vmid: int) -> bool:
        return self.r.run(["qm", "agent", str(vmid), "ping"], check=False).ok

    def wait_agent(self, vmid: int, timeout: float = 300, poll: float = 3, *,
                   sleep=time.sleep, monotonic=time.monotonic) -> None:
        deadline = monotonic() + timeout
        while not self.agent_ready(vmid):
            if monotonic() > deadline:
                raise ProxmoxError(f"guest agent did not come up within {timeout:.0f}s "
                                   "(no DHCP lease or package install failed?)")
            sleep(poll)

    def add_dir_command(self, storage_id: str, path: str, content: str, mountpoint: str | None = None) -> list:
        argv = ["pvesm", "add", "dir", storage_id, "--path", path, "--content", content]
        if mountpoint:  # Proxmox then treats the storage as offline when the disk is not mounted
            argv += ["--is_mountpoint", mountpoint]
        return argv

    def add_dir_storage(self, storage_id: str, path: str, content: str, mountpoint: str | None = None) -> None:
        """Register an existing directory as a Proxmox storage. Never creates or formats anything."""
        self.r.run(self.add_dir_command(storage_id, path, content, mountpoint))

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
        # vzdump reports a file path (or a volid, depending on storage); callers that need the
        # volume ID should diff `backups()` instead of trusting this.
        return m.group(1) if m else ""

    def restore(self, volid: str, vmid: int, storage: str) -> None:
        self.r.run(["qmrestore", volid, str(vmid), "--storage", storage])

    def free_volume(self, volid: str) -> None:
        """Delete one backup volume. Callers must only pass volumes lab created."""
        self.r.run(["pvesm", "free", volid])

    def test_archive(self, path: str) -> None:
        """zstd integrity test of a backup file; raises CommandError if corrupt."""
        self.r.run(["zstd", "-t", path])

    def disconnect_network(self, vmid: int) -> None:
        """Set link_down on net0 so a trial-restored copy can never reach (or collide on) the LAN."""
        net0 = self.config(vmid).get("net0")
        if net0 and "link_down" not in net0:
            self.set(vmid, net0=net0 + ",link_down=1")

    def guest_exec(self, vmid: int, argv, *, stdin: str | None = None, check: bool = True,
                   timeout: int | None = None):
        """Run a command in the guest via the QEMU guest agent. `stdin` is never logged.

        `timeout` is how long qm waits for the command (qm's own default is 30 s)."""
        cmd = ["qm", "guest", "exec", str(vmid)]
        if stdin is not None:
            cmd.append("--pass-stdin")
        if timeout is not None:
            cmd += ["--timeout", str(timeout)]
        cmd += ["--", *[str(a) for a in argv]]
        res = self.r.run(cmd, input=stdin, check=check, timeout=(timeout + 30) if timeout else None)
        try:
            return json.loads(res.stdout)  # {"exitcode": n, "out-data": "...", "err-data": "..."}
        except json.JSONDecodeError:
            return {"exitcode": res.returncode, "out-data": res.stdout, "err-data": res.stderr}


__all__ = ["Proxmox", "ProxmoxError", "CommandError"]
