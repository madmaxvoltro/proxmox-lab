"""Project lifecycle: create / list / status / start / stop / restart / delete / protect (M2).

Proxmox is the source of truth; the DB is the index. Every destructive step re-checks the
real VM (name, tag, VMID range) first, and a failed create rolls back everything it made."""

import logging
import time
from pathlib import Path

from . import access, netutil, progress, quotas
from .access import Caller
from .archive import ArchiveOps
from .cloudinit import user_data  # noqa: F401  (re-exported)
from .config import Config
from .db import DB
from .errors import AccessError, LabError, PreconditionError, UsageError
from .guest import GuestOps
from .hostio import HostIO
from .locking import lock
from .proxmox import Proxmox, ProxmoxError
from .tailscale import Tailscale
from .template import TemplateBuilder
from .units import hostname, to_gib, to_mib, validate_name

log = logging.getLogger("lab.lifecycle")
SHUTDOWN_TIMEOUT = 120


def resolve_resources(cfg: Config, size=None, cpu=None, ram=None, disk=None) -> tuple:
    """Defaults -> size preset -> explicit flags. Returns (cpu, ram_mb, disk_gb)."""
    c, r, d = cfg.vm.default_cores, cfg.vm.default_memory, to_gib(cfg.vm.default_disk)
    if size:
        if size not in cfg.presets:
            raise UsageError(f"unknown size {size!r}; available: {', '.join(sorted(cfg.presets))}")
        p = cfg.presets[size]
        c, r, d = int(p["cpu"]), to_mib(p["ram"]), to_gib(p["disk"])
    c = int(cpu) if cpu is not None else c
    r = to_mib(ram) if ram is not None else r
    d = to_gib(disk) if disk is not None else d
    if c < 1:
        raise UsageError("cpu must be at least 1")
    if r < 512:
        raise UsageError("ram must be at least 512M")
    floor = to_gib(cfg.vm.default_disk)
    if d < floor:
        raise UsageError(f"disk cannot be smaller than the template disk ({floor}G); shrinking is not supported")
    return c, r, d


class Lab(ArchiveOps, GuestOps):
    def __init__(self, cfg: Config, px: Proxmox, io: HostIO, db: DB, templates: TemplateBuilder,
                 ts: Tailscale, caller: Caller, *, lock_dir: str | None = None, sleep=time.sleep,
                 monotonic=time.monotonic, say=lambda m: None, agent_timeout: float = 300):
        self.cfg, self.px, self.io, self.db = cfg, px, io, db
        self.templates, self.ts, self.caller = templates, ts, caller
        self.lock_dir = lock_dir or str(Path(cfg.paths.state_dir) / "locks")
        self.sleep, self.monotonic, self.say, self.agent_timeout = sleep, monotonic, say, agent_timeout

    # helpers ---------------------------------------------------------------------
    def _global_lock(self):
        return lock(f"{self.lock_dir}/global.lock", what="another create/delete/archive")

    def _project_lock(self, name):
        return lock(f"{self.lock_dir}/{name}.lock", what=f"another operation on {name}")

    def _snippet_volid(self, name):
        return f"{self.cfg.storage.snippet_disk}:snippets/lab-{name}.yaml"

    def _mine(self, name):
        return access.get_project(self.db, self.caller, name)

    def _live_vm(self, row) -> dict | None:
        """The real VM behind a project, after checking it is the one lab created."""
        vm = self.px.vm(row["vmid"]) if row["vmid"] is not None else None
        if vm is None:
            return None
        lo, hi = self.cfg.vm.vmid_range
        tags = (vm.get("tags") or "").split(";")
        if vm.get("name") != hostname(row["name"]) or "lab" not in tags or not (lo <= int(vm["vmid"]) <= hi):
            raise PreconditionError(
                f"VM {vm['vmid']} does not match project {row['name']} (name/tag/range); "
                "refusing to touch it. Run `lab doctor`.")
        return vm

    def _tailnet_managed(self, row) -> bool:
        return row["tailnet_name"] is not None

    # create ----------------------------------------------------------------------
    def create(self, name: str, *, distro=None, size=None, cpu=None, ram=None, disk=None,
               clone_mode=None, owner=None, tailscale: bool = True) -> dict:
        validate_name(name)
        owner = owner or self.caller.name
        if owner != self.caller.name and not self.caller.is_admin:
            raise AccessError("only admins can create projects for other users")
        if self.db.get_user_by_name(owner) is None:
            raise UsageError(f"no such lab user: {owner}")
        cfg = self.cfg
        cfg.require_storage()
        if not cfg.storage.snippet_disk:
            raise PreconditionError("config.storage.snippet_disk unset; run `lab init`")
        distro = distro or cfg.vm.default_distro
        mode = clone_mode or cfg.vm.clone_mode
        if mode not in ("full", "linked"):
            raise UsageError("clone mode must be 'full' or 'linked'")
        c, r, d = resolve_resources(cfg, size, cpu, ram, disk)
        host = hostname(name)

        with self._global_lock():
            self._check_name_free(name, host)
            quotas.check(cfg, self.db, owner, cpu=c, ram_mb=r, disk_gb=d, is_admin=self.caller.is_admin)
            tpl = self.templates.find(distro)
            if tpl is None:
                progress.detail(self.say, f"no template for {distro} yet; building it first (several minutes)")
                self.templates.build(distro)
                tpl = self.templates.find(distro)
            vmid = self.px.nextid(*cfg.vm.vmid_range, taken=self.db.used_vmids())
            ip = netutil.allocate(cfg.network, self.db.used_ips(), self.px.r)
            jid = self.db.journal_begin(name, "create", self.caller.name)
            self.db.add_project(name=name, vmid=vmid, owner=owner, distro=distro, template_id=int(tpl["vmid"]),
                                storage_id=cfg.storage.fast_disk, clone_mode=mode, cpu=c, ram_mb=r, disk_gb=d,
                                state="creating", tailnet_name=host if tailscale else None, static_ip=ip)
            snippet = self.px.storage_path(self._snippet_volid(name))
            try:
                self._build_vm(name, owner, vmid, int(tpl["vmid"]), mode, c, r, d, snippet, jid, tailscale, ip)
            except BaseException as e:
                self._rollback_create(name, host, vmid, snippet, jid, e)
                raise
            self.db.journal_finish(jid, "ok")
        return self.status(name)

    def _check_name_free(self, name, host):
        # Same answer whether the name is taken by you, someone else, or a foreign VM:
        # users must not learn about other people's projects.
        if self.db.get_project(name) is not None or any(v.get("name") == host for v in self.px.vms()):
            raise PreconditionError(f"project name unavailable: {name}")

    def _build_vm(self, name, owner, vmid, tpl_id, mode, cpu, ram_mb, disk_gb, snippet, jid, tailscale, ip=None):
        cfg, px = self.cfg, self.px
        host = hostname(name)
        progress.plan(self.say, 5 if tailscale else 4)
        self.db.journal_step(jid, "clone")
        self.say(f"Cloning the template into VMID {vmid} ({mode} clone)")
        px.clone(tpl_id, vmid, host, full=(mode == "full"), storage=cfg.storage.fast_disk,
                 on_line=progress.line_cb(self.say))
        self.say("Configuring the VM")
        self.io.write_text(snippet, user_data(name, owner))
        px.set(vmid, cores=cpu, memory=ram_mb, tags=f"lab;owner-{owner}", **netutil.ipconfig(cfg.network, ip),
               cicustom=f"user={self._snippet_volid(name)}",
               description=f"lab-project name={name} owner={owner} created={int(time.time())}")
        progress.detail(self.say, f"{cpu} vCPU, {ram_mb} MiB RAM, {disk_gb} GiB disk" + (f", address {ip}" if ip else ""))
        if disk_gb > to_gib(cfg.vm.default_disk):
            px.resize(vmid, "scsi0", f"{disk_gb}G")
        self.db.journal_step(jid, "boot")
        self.say("Booting")
        px.start(vmid)
        px.wait_agent(vmid, self.agent_timeout, sleep=self.sleep, monotonic=self.monotonic,
                      on_wait=progress.wait_cb(self.say))
        self.say("First-boot setup")
        try:
            px.wait_cloud_init(vmid, 300, 5, sleep=self.sleep, monotonic=self.monotonic,
                               on_wait=progress.wait_cb(self.say),
                               on_detail=lambda text, key: progress.detail(self.say, text, key))
        except ProxmoxError as e:
            raise LabError(f"first-boot configuration failed: {e}") from e
        fields = {"state": "running"}
        if tailscale:
            self.db.journal_step(jid, "tailscale")
            self.say("Joining the tailnet")
            en = self.ts.enroll(vmid, host, owner)
            progress.detail(self.say, f"{en.dns_name} ({en.ip})")
            fields.update(tailnet_ip=en.ip, tailnet_name=en.dns_name)
        self.db.update_project(name, **fields)

    def _rollback_create(self, name, host, vmid, snippet, jid, error):
        """Remove everything this create made. Only touches a VM that still has the name we gave it."""
        log.warning("create of %s failed (%s); rolling back", name, error)
        try:
            vm = self.px.vm(vmid)
            if vm is not None and vm.get("name") == host and not vm.get("template"):
                if vm.get("status") == "running":
                    self.px.stop(vmid)  # brand new VM we just made: no user data to protect
                self.px.destroy(vmid)
        except LabError as e:
            log.warning("could not remove VM %s during rollback: %s", vmid, e)
        self.io.remove(snippet)
        self.db.delete_project(name)
        self.db.journal_finish(jid, "rolled_back", str(error)[:300])

    # read ------------------------------------------------------------------------
    def list(self, everything: bool = False) -> list:
        live = {int(v["vmid"]): v for v in self.px.vms()}
        out = []
        for r in access.visible_projects(self.db, self.caller, everything):
            state, vm = r["state"], live.get(r["vmid"])
            if state in ("running", "stopped"):
                state = vm["status"] if vm else "missing"
            out.append({"name": r["name"], "owner": r["owner"], "vmid": r["vmid"], "state": state,
                        "distro": r["distro"], "cpu": r["cpu"], "ram_mb": r["ram_mb"], "disk_gb": r["disk_gb"],
                        "tailnet_name": r["tailnet_name"], "tailnet_ip": r["tailnet_ip"],
                        "protected": bool(r["protected"])})
        return out

    def status(self, name: str) -> dict:
        row = self._mine(name)
        info = next((p for p in self.list(everything=True) if p["name"] == name), None) or {}
        cfg = {}
        if row["vmid"] is not None and self.px.vm(row["vmid"]):
            cfg = self.px.config(row["vmid"])
        info.update(clone_mode=row["clone_mode"], template_id=row["template_id"], storage=row["storage_id"],
                    live_config={k: cfg[k] for k in ("cores", "memory", "scsi0") if k in cfg},
                    ssh=(f"ssh {row['owner']}@{row['tailnet_name']}" if row["tailnet_name"] else None))
        return info

    # start / stop ------------------------------------------------------------------
    def start(self, name: str) -> dict:
        row = self._mine(name)
        if row["state"] == "archived":
            raise PreconditionError(f"{name} is archived; restore it first")
        with self._project_lock(name):
            vm = self._live_vm(row)
            if vm is None:
                raise PreconditionError(f"{name} has no VM in Proxmox; run `lab doctor`")
            running = vm["status"] == "running"
            if not running:
                quotas.check(self.cfg, self.db, row["owner"], cpu=row["cpu"], ram_mb=row["ram_mb"],
                             disk_gb=row["disk_gb"], exclude=name, is_admin=self.caller.is_admin)
                self.say(f"Starting {name}")
                self.px.start(row["vmid"])
            self.say("Waiting for the guest agent")
            self.px.wait_agent(row["vmid"], self.agent_timeout, sleep=self.sleep, monotonic=self.monotonic,
                               on_wait=progress.wait_cb(self.say))
            self.db.update_project(name, state="running")
            if self._tailnet_managed(row) and (not running or not row["tailnet_ip"]):
                self.say("Joining the tailnet")
                en = self.ts.enroll(row["vmid"], hostname(name), row["owner"])
                self.db.update_project(name, tailnet_ip=en.ip, tailnet_name=en.dns_name)
        return self.status(name)

    def stop(self, name: str, force: bool = False) -> dict:
        row = self._mine(name)
        with self._project_lock(name):
            vm = self._live_vm(row)
            if vm is not None and vm["status"] == "running":
                self.say(f"Stopping {name} (leaving the tailnet, graceful shutdown)")
                self._shutdown(row, force)
            self.db.update_project(name, state="stopped", tailnet_ip=None)
        return self.status(name)

    def restart(self, name: str, force: bool = False) -> dict:
        self.stop(name, force)
        return self.start(name)

    def _shutdown(self, row, force: bool) -> None:
        vmid = row["vmid"]
        if self._tailnet_managed(row) and not self.ts.logout(vmid):
            log.warning("could not confirm Tailscale logout for %s; the ephemeral device will expire on its own",
                        row["name"])
        self.px.shutdown(vmid, SHUTDOWN_TIMEOUT)
        if self.px.status(vmid) != "stopped":
            if not force:
                raise PreconditionError(f"{row['name']} did not shut down in {SHUTDOWN_TIMEOUT}s; "
                                        "not forcing it (unsaved work could be lost). Retry with --force.")
            self.px.stop(vmid)

    # delete / protect ----------------------------------------------------------------
    def delete(self, name: str, confirm: str, force: bool = False, archives: bool = False) -> None:
        """Destroy a project. Its archives are kept separate: they are only deleted with
        `archives=True`, and a project that has any refuses to be deleted without it."""
        if confirm != name:
            raise UsageError(f"type the project name to confirm deletion: expected {name!r}")
        row = self._mine(name)
        if row["protected"]:
            raise PreconditionError(f"{name} is protected; run `lab unprotect {name}` first")
        held = self.db.list_archives(name)
        if held and not archives:
            raise PreconditionError(
                f"{name} has {len(held)} archive(s); deleting it would orphan them. "
                "Pass --archives to delete the archives too.")
        if row["state"] == "archived":
            with self._global_lock():
                self._delete_archives(name, held)
                self.db.delete_project(name)
            return
        with self._global_lock():
            vm = self._live_vm(row)
            jid = self.db.journal_begin(name, "delete", self.caller.name)
            self.db.update_project(name, state="deleting")
            try:
                if vm is not None:
                    if vm["status"] == "running":
                        self._shutdown(row, force)
                    self.px.destroy(row["vmid"])
            except BaseException as e:
                self.db.update_project(name, state="stopped" if vm and vm["status"] != "running" else "running")
                self.db.journal_finish(jid, "failed", str(e)[:300])
                raise
            self.io.remove(self.px.storage_path(self._snippet_volid(name)))
            self._delete_archives(name, held)
            self.db.delete_project(name)
            self.db.journal_finish(jid, "ok")

    def set_protected(self, name: str, value: bool) -> None:
        row = self._mine(name)
        self.db.update_project(row["name"], protected=int(value))
