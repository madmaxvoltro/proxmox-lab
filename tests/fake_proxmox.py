"""In-memory stand-in for the Proxmox CLI tools, used as a lab Runner in tests.

It implements just enough of qm / pvesh / pvesm / vzdump / qmrestore to drive the
lifecycle code, with failure injection:

    fake.fail("vzdump", stderr="no space left")          # any vzdump call
    fake.fail(("qm", "start"), returncode=255)           # a specific subcommand
    fake.fail(lambda argv: argv[:2] == ["qm", "clone"])  # arbitrary predicate
"""

import json
import re

from lab.errors import CommandError
from lab.runner import Result, Runner


class FakeProxmox(Runner):
    def __init__(self, storages=None):
        self.vms = {}  # vmid -> dict(name, status, template, config{})
        self.storages = storages or {
            "nvme": {"type": "lvmthin", "content": "images,rootdir", "total": 1 << 40, "used": 0},
            "bigdisk": {"type": "dir", "content": "backup", "total": 20 << 40, "used": 0},
        }
        self.backups = {}  # volid -> dict(vmid, storage, size, corrupt)
        self.calls = []
        self.stdin_seen = []
        self._failures = []
        self._seq = 0
        self.guest_agent_ready = True

    # test helpers ----------------------------------------------------------
    def add_vm(self, vmid, name, *, status="stopped", template=False, **config):
        self.vms[int(vmid)] = {
            "name": name, "status": status, "template": template,
            "config": {"cores": "1", "memory": "2048", "scsi0": "nvme:vm-%d-disk-0,size=32G" % vmid, **config},
        }

    def fail(self, match, *, returncode=1, stderr="injected failure"):
        if isinstance(match, str):
            pred = lambda a: a[0] == match
        elif isinstance(match, tuple):
            pred = lambda a: tuple(a[: len(match)]) == match
        else:
            pred = match
        self._failures.append((pred, returncode, stderr))

    def called(self, *prefix):
        return [c for c in self.calls if tuple(c[: len(prefix)]) == prefix]

    # Runner ----------------------------------------------------------------
    def run(self, argv, *, input=None, check=True, timeout=None):
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        if input is not None:
            self.stdin_seen.append(input)
        for pred, rc, err in self._failures:
            if pred(argv):
                res = Result(argv, rc, "", err)
                break
        else:
            try:
                res = self._dispatch(argv)
            except KeyError as e:
                res = Result(argv, 2, "", f"fake: {e}")
        if check and not res.ok:
            raise CommandError(argv, res.returncode, res.stderr)
        return res

    # dispatch --------------------------------------------------------------
    def _ok(self, argv, out=""):
        return Result(argv, 0, out, "")

    def _err(self, argv, msg, rc=255):
        return Result(argv, rc, "", msg)

    def _dispatch(self, a):
        cmd = a[0]
        if cmd == "pveversion":
            return self._ok(a, "pve-manager/9.2.1/abcdef (running kernel: 6.14.0-pve)\n")
        if cmd == "pvesh":
            return self._pvesh(a)
        if cmd == "qm":
            return self._qm(a)
        if cmd == "pvesm":
            return self._pvesm(a)
        if cmd == "vzdump":
            return self._vzdump(a)
        if cmd == "qmrestore":
            return self._qmrestore(a)
        return self._err(a, f"fake: unknown command {cmd}", 127)

    def _opt(self, a, flag, default=None):
        return a[a.index(flag) + 1] if flag in a else default

    def _pvesh(self, a):
        path = a[2]
        if path == "/cluster/resources":
            kind = self._opt(a, "--type")
            if kind == "vm":
                rows = [
                    {"type": "qemu", "vmid": v, "name": d["name"], "status": d["status"],
                     "template": int(d["template"]), "node": "pve"}
                    for v, d in sorted(self.vms.items())
                ]
            else:
                rows = [
                    {"type": "storage", "storage": s, "plugintype": d["type"], "content": d["content"],
                     "maxdisk": d["total"], "disk": d["used"], "status": "available", "node": "pve"}
                    for s, d in self.storages.items()
                ]
            return self._ok(a, json.dumps(rows))
        if path == "/cluster/nextid":
            vmid = int(self._opt(a, "--vmid", 100))
            if vmid in self.vms:
                return self._err(a, f"VM {vmid} already exists")
            return self._ok(a, json.dumps(vmid))
        m = re.fullmatch(r"/storage/(\S+)", path)
        if m and m.group(1) in self.storages:
            d = self.storages[m.group(1)]
            return self._ok(a, json.dumps({"storage": m.group(1), "type": d["type"], "content": d["content"]}))
        return self._err(a, f"fake: unhandled pvesh path {path}")

    def _qm(self, a):
        sub = a[1]
        if sub == "clone":
            src, new = int(a[2]), int(a[3])
            if src not in self.vms:
                return self._err(a, f"Configuration file 'nodes/pve/qemu-server/{src}.conf' does not exist")
            if new in self.vms:
                return self._err(a, f"VM {new} already exists")
            s = self.vms[src]
            if not s["template"]:
                return self._err(a, "can't clone non-template without snapshot")
            self.vms[new] = {
                "name": self._opt(a, "--name", f"Copy-of-VM-{s['name']}"),
                "status": "stopped", "template": False,
                "config": {**s["config"], "full": self._opt(a, "--full", "0")},
            }
            return self._ok(a)
        vmid = int(a[3] if sub == "guest" else a[2])  # qm guest exec <vmid> ...
        vm = self.vms.get(vmid)
        if vm is None:
            return self._err(a, f"Configuration file 'nodes/pve/qemu-server/{vmid}.conf' does not exist")
        if sub == "status":
            return self._ok(a, f"status: {vm['status']}\n")
        if sub == "config":
            return self._ok(a, "".join(f"{k}: {v}\n" for k, v in vm["config"].items()) + f"name: {vm['name']}\n")
        if sub == "set":
            opts = a[3:]
            for k, v in zip(opts[::2], opts[1::2]):
                vm["config"][k.lstrip("-")] = v
            return self._ok(a)
        if sub == "resize":
            disk, size = a[3], a[4]
            vm["config"][disk] = re.sub(r"size=\S+", f"size={size.lstrip('+')}", vm["config"].get(disk, ""))
            return self._ok(a)
        if sub == "start":
            if vm["status"] == "running":
                return self._err(a, "VM already running")
            vm["status"] = "running"
            return self._ok(a)
        if sub in ("shutdown", "stop"):
            vm["status"] = "stopped"
            return self._ok(a)
        if sub == "destroy":
            if vm["status"] == "running":
                return self._err(a, "VM is running - destroy failed")
            del self.vms[vmid]
            return self._ok(a)
        if sub == "guest":
            if vm["status"] != "running" or not self.guest_agent_ready:
                return self._err(a, f"QEMU guest agent is not running")
            return self._ok(a, json.dumps({"exitcode": 0, "out-data": "", "err-data": ""}))
        return self._err(a, f"fake: unhandled qm {sub}")

    def _pvesm(self, a):
        if a[1] == "list":
            sid = a[2]
            vmid = self._opt(a, "--vmid")
            rows = [
                {"volid": v, "size": b["size"], "vmid": b["vmid"], "content": "backup", "format": "vma.zst"}
                for v, b in self.backups.items()
                if b["storage"] == sid and (vmid is None or b["vmid"] == int(vmid))
            ]
            return self._ok(a, json.dumps(rows))
        return self._err(a, f"fake: unhandled pvesm {a[1]}")

    def _vzdump(self, a):
        vmid = int(a[1])
        sid = self._opt(a, "--storage")
        vm = self.vms.get(vmid)
        if vm is None:
            return self._err(a, f"no such VM ('{vmid}')")
        if sid not in self.storages or "backup" not in self.storages[sid]["content"]:
            return self._err(a, f"storage '{sid}' does not support backups")
        if self._opt(a, "--mode") == "stop" and vm["status"] == "running":
            vm["status"] = "stopped"  # vzdump stop-mode shuts the guest down itself
        self._seq += 1
        volid = f"{sid}:backup/vzdump-qemu-{vmid}-{self._seq:04d}.vma.zst"
        self.backups[volid] = {"vmid": vmid, "storage": sid, "size": 1 << 30, "corrupt": False,
                               "name": vm["name"], "config": dict(vm["config"])}
        return self._ok(a, f"INFO: creating vzdump archive '{volid}'\nINFO: Finished Backup of VM {vmid}\n")

    def _qmrestore(self, a):
        volid, vmid = a[1], int(a[2])
        b = self.backups.get(volid)
        if b is None:
            return self._err(a, f"volume '{volid}' does not exist")
        if b["corrupt"]:
            return self._err(a, "restore failed: archive is corrupt")
        if vmid in self.vms:
            return self._err(a, f"unable to restore VM {vmid} - VM already exists")
        self.vms[vmid] = {"name": b["name"], "status": "stopped", "template": False, "config": dict(b["config"])}
        return self._ok(a)
