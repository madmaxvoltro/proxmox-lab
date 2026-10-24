"""Archive and restore (SPEC sections 10 and 11) as a mixin for Lab.

Safety shape of `archive`:
  1. shut the VM down (graceful only), back it up to the archive disk,
  2. verify: backup listed, plausible size, zstd integrity, trial restore with the network
     disconnected, boot test of the restored copy,
  3. only then commit: record the archive, THEN destroy the original VM.
Any failure before the commit deletes the new backup and any temporary copy, restarts the
original exactly as it was, and reports. Every step is journaled with enough state in the
journal to recover after a crash: before the commit point recovery rolls back, after it
recovery rolls forward."""

import json
import logging

from . import netutil, quotas
from .cloudinit import user_data
from .errors import CommandError, LabError, PreconditionError, VerificationError
from .units import hostname

log = logging.getLogger("lab.archive")
GIB = 1 << 30
SPACE_MARGIN = 1.2
COMMITTED = ("commit", "destroyed")


def verify_name(name: str) -> str:
    return "lab-verify-" + name


class ArchiveOps:
    verify_timeout = 300
    min_backup_bytes = 1 << 20

    # small helpers ----------------------------------------------------------------
    def _persist(self, jid: int, step: str, st: dict) -> None:
        self.db.journal_step(jid, step, json.dumps(st))

    def _archive_disk(self) -> str:
        return self.cfg.storage.archive_disk

    def _check_archive_space(self, row) -> None:
        s = next((s for s in self.px.storages() if s.get("storage") == self._archive_disk()), None)
        if s is None:
            raise PreconditionError(f"archive disk {self._archive_disk()!r} not found in Proxmox")
        free = int(s.get("maxdisk") or 0) - int(s.get("disk") or 0)
        need = int(row["disk_gb"] * GIB * SPACE_MARGIN)
        if free < need:
            raise PreconditionError(
                f"archive disk has {free // GIB} GiB free; need about {need // GIB} GiB for {row['name']}")

    def _busy(self, row) -> None:
        if row["state"] in ("creating", "deleting", "archiving", "restoring"):
            raise PreconditionError(
                f"{row['name']} is {row['state']}; if that operation was interrupted run `lab recover {row['name']}`")

    # archive ------------------------------------------------------------------------
    def archive(self, name: str, *, keep_active: bool = False, boot_test: bool = True) -> dict:
        row = self._mine(name)
        self._busy(row)
        if row["state"] == "archived":
            raise PreconditionError(f"{name} is already archived")
        if row["protected"]:
            raise PreconditionError(f"{name} is protected; run `lab unprotect {name}` first")
        self.cfg.require_storage()
        with self._global_lock():
            vm = self._live_vm(row)
            if vm is None:
                raise PreconditionError(f"{name} has no VM in Proxmox; run `lab doctor`")
            self._check_archive_space(row)
            st = {"was_running": vm["status"] == "running", "volid": None, "tmp_vmid": None,
                  "before": [], "keep_active": keep_active}
            jid = self.db.journal_begin(name, "archive", self.caller.name)
            self.db.update_project(name, state="archiving")
            step = "shutdown"
            try:
                self._persist(jid, step, st)
                if st["was_running"]:
                    self.say(f"stopping {name}")
                    self._shutdown(row, force=False)
                st["before"] = [b["volid"] for b in self.px.backups(self._archive_disk(), row["vmid"])]
                step = "backup"
                self._persist(jid, step, st)
                self.say(f"backing up {name} to {self._archive_disk()}")
                self.px.vzdump(row["vmid"], self._archive_disk())
                st["volid"] = self._new_backup(row, st)
                step = "verify"
                self._persist(jid, step, st)
                st["size"], st["sha256"] = self._verify(row, st, jid, boot_test)
            except BaseException as e:
                err = self._abort_archive(row, st, jid, e, backup_stage=(step != "shutdown"))
                if err is e:
                    raise
                raise err from e

            self._persist(jid, "commit", st)  # point of no return: from here recovery rolls forward
            self._commit_archive(row, st)
            if keep_active:
                self.say("verified; keeping the active VM as requested")
                self._finish_keep_active(row, st, jid)
                return self.status(name)
            self._finish_archive(row, jid, st)
        return self.status(name)

    def _new_backup(self, row, st: dict) -> str:
        """The volume vzdump just created, found by diffing the storage listing (vzdump itself
        reports a file path, while everything else here uses storage volume IDs)."""
        new = [b["volid"] for b in self.px.backups(self._archive_disk(), row["vmid"])
               if b["volid"] not in st["before"]]
        if len(new) != 1:
            raise VerificationError(f"expected exactly one new backup after vzdump, found {len(new)}")
        return new[0]

    def _verify(self, row, st, jid, boot_test) -> tuple:
        px, cfg, name = self.px, self.cfg, row["name"]
        volid = st["volid"]
        try:
            entry = next((b for b in px.backups(self._archive_disk(), row["vmid"]) if b["volid"] == volid), None)
            if entry is None:
                raise VerificationError("backup is not listed on the archive disk after vzdump")
            size = int(entry.get("size") or 0)
            if size < self.min_backup_bytes:
                raise VerificationError(f"backup is implausibly small ({size} bytes)")
            path = px.storage_path(volid)
            self.say("verifying archive integrity")
            px.test_archive(path)
            sha = self.io.sha256(path)
            tmp = px.nextid(*cfg.vm.vmid_range, taken=self.db.used_vmids())
            st["tmp_vmid"] = tmp
            self._persist(jid, "verify", st)
            self.say(f"trial restore to temporary VM {tmp} (network disconnected)")
            px.restore(volid, tmp, cfg.storage.fast_disk)
            px.set(tmp, name=verify_name(name), tags="lab;lab-verify")
            px.disconnect_network(tmp)
            if boot_test:
                self._boot_test(row, tmp)
            self._destroy_verify_vm(row, st)
            return size, sha
        except VerificationError:
            raise
        except LabError as e:
            raise VerificationError(str(e)) from e

    def _boot_test(self, row, tmp: int) -> None:
        self.say("boot test of the restored copy")
        self.px.start(tmp)
        self.px.wait_agent(tmp, self.verify_timeout, sleep=self.sleep, monotonic=self.monotonic)
        check = f'findmnt -n / >/dev/null && id {row["owner"]} >/dev/null && [ "$(hostname)" = {hostname(row["name"])} ]'
        res = self.px.guest_exec(tmp, ["sh", "-c", check], check=False, timeout=60)
        if res.get("exitcode", 1) != 0:
            raise VerificationError("the restored copy booted but failed its checks: "
                                    + (res.get("err-data") or res.get("out-data") or "").strip()[-300:])

    def _destroy_verify_vm(self, row, st: dict) -> None:
        """Remove the temporary trial-restore VM, but only a VM that is plainly ours: the recorded
        VMID, never the original, with one of the two names a trial restore can carry."""
        tmp = st.get("tmp_vmid")
        if tmp is None:
            return
        if tmp == row["vmid"]:
            raise PreconditionError("refusing to treat the original VM as the temporary copy")
        vm = self.px.vm(tmp)
        if vm is not None and vm.get("name") in (verify_name(row["name"]), hostname(row["name"])):
            if vm.get("status") == "running":
                self.px.stop(tmp)  # throwaway copy: a hard stop is fine
            self.px.destroy(tmp)
        st["tmp_vmid"] = None

    def _abort_archive(self, row, st: dict, jid: int, error: BaseException, backup_stage: bool):
        """Undo a failed archive: delete the new backup and trial VM, bring the original back as it
        was. Returns the exception the caller should raise."""
        name = row["name"]
        log.warning("archive of %s failed (%s); rolling back", name, error)
        notes = []
        try:
            self._destroy_verify_vm(row, st)
        except LabError as e:
            notes.append(f"temporary VM {st.get('tmp_vmid')} was not removed ({e})")
        try:
            current = {b["volid"] for b in self.px.backups(self._archive_disk(), row["vmid"])}
            known = {a["volid"] for a in self.db.list_archives(name)}
            for volid in sorted(current - set(st.get("before", [])) - known):
                self.px.free_volume(volid)
        except LabError as e:
            notes.append(f"the new backup could not be deleted ({e})")
        state = self._resume(row, st.get("was_running", False), notes)
        self.db.update_project(name, state=state)
        self.db.journal_finish(jid, "rolled_back", str(error)[:300])
        if not isinstance(error, LabError) or not backup_stage:
            return error
        extra = f" Also: {'; '.join(notes)}." if notes else ""
        return VerificationError(
            f"archive of {name} FAILED: {error}. The new backup was deleted and "
            f"{name} is untouched ({state}).{extra}")

    def _resume(self, row, was_running: bool, notes: list) -> str:
        """Put the original VM back the way it was before the archive started. Returns the project state."""
        vm = self.px.vm(row["vmid"]) if row["vmid"] is not None else None
        if vm is None:
            notes.append("the original VM is missing from Proxmox; run `lab doctor`")
            return "stopped"
        if not was_running:
            return "running" if vm["status"] == "running" else "stopped"
        try:
            if vm["status"] != "running":
                self.px.start(row["vmid"])
            self.px.wait_agent(row["vmid"], self.agent_timeout, sleep=self.sleep, monotonic=self.monotonic)
            if self._tailnet_managed(row):
                en = self.ts.enroll(row["vmid"], hostname(row["name"]), row["owner"])
                self.db.update_project(row["name"], tailnet_ip=en.ip, tailnet_name=en.dns_name)
            return "running"
        except LabError as e:
            notes.append(f"could not bring {row['name']} back up cleanly ({e}); run `lab start {row['name']}`")
            live = self.px.vm(row["vmid"])
            return "running" if live and live["status"] == "running" else "stopped"

    def _commit_archive(self, row, st: dict) -> None:
        """Record the verified archive. Idempotent so recovery can call it again."""
        if not any(a["volid"] == st["volid"] for a in self.db.list_archives(row["name"])):
            self.db.add_archive(row["name"], row["owner"], st["volid"], st.get("size"), st.get("sha256"), verified=True)

    def _finish_keep_active(self, row, st: dict, jid: int) -> None:
        notes: list = []
        state = self._resume(row, st.get("was_running", False), notes)
        self.db.update_project(row["name"], state=state)
        self._prune(row["name"])
        self.db.journal_finish(jid, "ok", "; ".join(notes) or None)

    def _finish_archive(self, row, jid: int, st: dict) -> None:
        """Destroy the original VM and mark the project archived. Safe to run twice."""
        name, vmid = row["name"], row["vmid"]
        if vmid is not None:
            present = {b["volid"] for b in self.px.backups(self._archive_disk(), vmid)}
            vm = self._live_vm(row)
            if vm is not None:
                if st["volid"] not in present:
                    raise PreconditionError("the verified backup is no longer on the archive disk; "
                                            "refusing to destroy the VM")
                if vm["status"] == "running":
                    raise PreconditionError(f"{name} is running after its backup; refusing to destroy it")
                self.say(f"removing {name} from the fast disk")
                self.px.destroy(vmid)
        self._persist(jid, "destroyed", st)
        self.io.remove(self.px.storage_path(self._snippet_volid(name)))
        self.db.update_project(name, state="archived", vmid=None, tailnet_ip=None, storage_id=self._archive_disk())
        self._prune(name)
        self.db.journal_finish(jid, "ok")

    def _prune(self, name: str) -> None:
        """Keep only the newest `archive.keep` archives; runs only after a new one verified."""
        keep = self.cfg.archive.keep
        for a in self.db.list_archives(name)[:-keep]:
            try:
                self.px.free_volume(a["volid"])
                self.db.delete_archive(a["id"])
            except LabError as e:
                log.warning("could not delete old archive %s: %s", a["volid"], e)

    def _delete_archives(self, name: str, held) -> None:
        for a in held:
            try:
                self.px.free_volume(a["volid"])
            except CommandError:
                if a["volid"] in {b["volid"] for b in self.px.backups(self._archive_disk())}:
                    raise  # still there: do not forget it
            self.db.delete_archive(a["id"])

    # restore ------------------------------------------------------------------------
    def restore(self, name: str, *, start: bool = True) -> dict:
        row = self._mine(name)
        self._busy(row)
        if row["state"] != "archived":
            raise PreconditionError(f"{name} is not archived")
        verified = [a for a in self.db.list_archives(name) if a["verified_at"]]
        if not verified:
            raise PreconditionError(f"{name} has no verified archive")
        arc = verified[-1]
        self.cfg.require_storage()
        if arc["volid"] not in {b["volid"] for b in self.px.backups(self._archive_disk())}:
            raise PreconditionError(f"archive {arc['volid']} is missing from the archive disk")
        quotas.check(self.cfg, self.db, row["owner"], cpu=row["cpu"], ram_mb=row["ram_mb"], disk_gb=row["disk_gb"],
                     running=start, is_admin=self.caller.is_admin)
        host, fast, owner = hostname(name), self.cfg.storage.fast_disk, row["owner"]
        with self._global_lock():
            vmid = self.px.nextid(*self.cfg.vm.vmid_range, taken=self.db.used_vmids())
            net = self.cfg.network
            ip = row["static_ip"]
            if net.mode == "static" and not (ip and self._in_pool(ip)):
                ip = netutil.allocate(net, self.db.used_ips(), self.px.r)  # first restore under a new addressing mode
            elif net.mode != "static":
                ip = None
            jid = self.db.journal_begin(name, "restore", self.caller.name)
            self.db.update_project(name, state="restoring", vmid=vmid, static_ip=ip)
            self._persist(jid, "restore", {"vmid": vmid})
            snippet = self.px.storage_path(self._snippet_volid(name))
            try:
                self.say(f"restoring {name} to VMID {vmid} on {fast}")
                self.px.restore(arc["volid"], vmid, fast)
                self.px.set(vmid, name=host, tags=f"lab;owner-{owner}", cores=row["cpu"], memory=row["ram_mb"],
                            cicustom=f"user={self._snippet_volid(name)}", **netutil.ipconfig(net, ip))
                self.io.write_text(snippet, user_data(name, owner))
                self.db.update_project(name, state="stopped", storage_id=fast, tailnet_ip=None)
            except BaseException as e:
                self._rollback_restore(name, host, vmid, snippet, jid, e)
                raise
            self.db.journal_finish(jid, "ok")
        if start:
            try:
                self.px.start(vmid)
                self.px.wait_agent(vmid, self.agent_timeout, sleep=self.sleep, monotonic=self.monotonic)
                self.db.update_project(name, state="running")  # truthful even if enrolment fails below
                fields = {}
                if self._tailnet_managed(row):
                    self.ts.reset_identity(vmid)
                    en = self.ts.enroll(vmid, host, owner)
                    fields.update(tailnet_ip=en.ip, tailnet_name=en.dns_name)
                if fields:
                    self.db.update_project(name, **fields)
            except LabError as e:
                raise LabError(f"{name} was restored but could not be started: {e}. "
                               f"Run `lab start {name}` once the problem is fixed.") from e
        return self.status(name)

    def _in_pool(self, ip: str) -> bool:
        return ip in netutil.pool(self.cfg.network)

    def _rollback_restore(self, name, host, vmid, snippet, jid, error) -> None:
        log.warning("restore of %s failed (%s); rolling back", name, error)
        try:
            vm = self.px.vm(vmid)
            if vm is not None and vm.get("name") in (host, verify_name(name)) and not vm.get("template"):
                if vm.get("status") == "running":
                    self.px.stop(vmid)
                self.px.destroy(vmid)
        except LabError as e:
            log.warning("could not remove restored VM %s: %s", vmid, e)
        self.io.remove(snippet)
        self.db.update_project(name, state="archived", vmid=None)
        self.db.journal_finish(jid, "rolled_back", str(error)[:300])

    # recovery -----------------------------------------------------------------------
    def recover(self, name: str) -> str:
        """Finish or undo an interrupted operation. Before an archive's commit point it rolls back
        (the original VM is still intact); after it, it rolls forward."""
        row = self._mine(name)
        ops = [j for j in self.db.open_journal() if j["project"] == name]
        if not ops:
            raise PreconditionError(f"nothing to recover for {name}")
        out = []
        with self._global_lock():
            for j in ops:
                row = self.db.get_project(name)
                if row is None:
                    self.db.journal_finish(j["id"], "rolled_back", "project no longer exists")
                    continue
                out.append(self._recover_one(row, j))
        return "; ".join(out)

    def _recover_one(self, row, j) -> str:
        name, st = row["name"], json.loads(j["detail"] or "{}") if (j["detail"] or "").startswith("{") else {}
        jid, op = j["id"], j["op"]
        if op == "archive":
            st.setdefault("volid", None)
            if j["step"] in COMMITTED and st.get("volid"):
                self._commit_archive(row, st)
                if st.get("keep_active"):
                    self._finish_keep_active(row, st, jid)
                    return f"{name}: archive was verified; kept the active VM"
                self._finish_archive(row, jid, st)
                return f"{name}: archive had been verified; completed it"
            self._abort_archive(row, st, jid, RuntimeError("interrupted"), backup_stage=False)
            return f"{name}: interrupted archive rolled back; the original VM is intact ({self.db.get_project(name)['state']})"
        if op == "create":
            self._rollback_create(name, hostname(name), row["vmid"], self.px.storage_path(self._snippet_volid(name)),
                                  jid, "interrupted")
            return f"{name}: interrupted create rolled back"
        if op == "restore":
            self._rollback_restore(name, hostname(name), row["vmid"], self.px.storage_path(self._snippet_volid(name)),
                                   jid, "interrupted")
            return f"{name}: interrupted restore rolled back; the archive is untouched"
        if op == "delete":
            vm = self.px.vm(row["vmid"]) if row["vmid"] is not None else None
            if vm is None:
                self.db.delete_project(name)
                self.db.journal_finish(jid, "ok", "VM already gone")
                return f"{name}: delete had finished; cleaned the index"
            self.db.update_project(name, state="running" if vm["status"] == "running" else "stopped")
            self.db.journal_finish(jid, "rolled_back", "interrupted")
            return f"{name}: interrupted delete abandoned; retry `lab delete {name}`"
        self.db.journal_finish(jid, "failed", f"unknown op {op}")
        return f"{name}: unknown operation {op}"
