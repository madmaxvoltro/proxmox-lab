import json
import tempfile
import unittest

from lab import access
from lab.access import Caller
from lab.db import DB
from lab.errors import AccessError, LabError, PreconditionError, UsageError
from lab.lifecycle import Lab, resolve_resources, user_data
from lab.proxmox import Proxmox
from lab.runner import Result
from lab.tailscale import Tailscale, TailscaleError, TOKEN_URL, KEYS_URL
from lab.template import TemplateBuilder

from .fake_io import FakeIO
from .fake_proxmox import FakeProxmox
from .test_m1 import CATALOGUE, StubRunner, make_cfg, make_io

KEY = "tskey-auth-SECRET123"


def tailscale_ok(fake, ip="100.64.0.5", dns="lab-api.tail1234.ts.net."):
    fake.guest_results[:0] = [
        (lambda c: c.startswith("tailscale ip"), {"exitcode": 0, "out-data": ip + "\n"}),
        (lambda c: c.startswith("tailscale status"),
         {"exitcode": 0, "out-data": json.dumps({"Self": {"DNSName": dns}})}),
    ]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fake = FakeProxmox()
        self.fake.add_vm(100, "jellyfin", status="running")
        self.fake.add_vm(9000, "lab-tpl-ubuntu-24.04", template=True)
        tailscale_ok(self.fake)
        self.io = make_io()
        self.cfg = make_cfg()
        self.db = DB(":memory:")
        self.db.add_user("max@x.io", "max", "admin")
        self.db.add_user("bo@x.io", "bo", "user")
        self.px = Proxmox(self.fake)
        self.max = Caller("max@x.io", "max", "admin")
        self.bo = Caller("bo@x.io", "bo", "user")

    def lab(self, caller=None):
        tb = TemplateBuilder(self.cfg, self.px, self.io, self.db, catalogue=CATALOGUE,
                             sleep=lambda s: None, agent_timeout=1)
        return Lab(self.cfg, self.px, self.io, self.db, tb, Tailscale(self.cfg, self.px, self.io),
                   caller or self.max, lock_dir=self.tmp.name, sleep=lambda s: None, agent_timeout=0)

    def vm_names(self):
        return {v["name"] for v in self.fake.vms.values()}


class ResourcesTest(unittest.TestCase):
    def test_defaults_presets_and_overrides(self):
        cfg = make_cfg()
        self.assertEqual(resolve_resources(cfg), (1, 2048, 32))
        self.assertEqual(resolve_resources(cfg, size="large"), (8, 16384, 64))
        self.assertEqual(resolve_resources(cfg, size="large", ram="4G"), (8, 4096, 64))
        self.assertEqual(resolve_resources(cfg, cpu=4, ram="8G", disk="100G"), (4, 8192, 100))

    def test_rejects_bad_values(self):
        cfg = make_cfg()
        for kw in ({"size": "huge"}, {"cpu": 0}, {"ram": "256M"}, {"disk": "16G"}, {"ram": "lots"}):
            with self.assertRaises(UsageError, msg=str(kw)):
                resolve_resources(cfg, **kw)

    def test_user_data_has_only_the_owner_and_no_secrets(self):
        ud = user_data("api", "max")
        self.assertIn("hostname: lab-api", ud)
        self.assertIn("name: max", ud)
        self.assertIn("lock_passwd: true", ud)
        self.assertIn("NOPASSWD", ud)
        self.assertNotIn("ssh_authorized_keys", ud)


class CreateTest(Base):
    def test_create_runs_end_to_end(self):
        res = self.lab().create("api")
        self.assertEqual((res["state"], res["owner"], res["tailnet_ip"]), ("running", "max", "100.64.0.5"))
        self.assertEqual(res["tailnet_name"], "lab-api.tail1234.ts.net")
        self.assertEqual(res["ssh"], "ssh max@lab-api.tail1234.ts.net")
        vmid = res["vmid"]
        lo, hi = self.cfg.vm.vmid_range
        self.assertTrue(lo <= vmid <= hi)
        vm = self.fake.vms[vmid]
        self.assertEqual((vm["name"], vm["tags"], vm["status"]), ("lab-api", "lab;owner-max", "running"))
        self.assertEqual((vm["config"]["cores"], vm["config"]["memory"]), ("1", "2048"))
        self.assertEqual(vm["config"]["full"], "1")
        self.assertEqual(vm["config"]["cicustom"], "user=local:snippets/lab-api.yaml")
        self.assertIn("name: max", self.io.files["/var/lib/vz/snippets/lab-api.yaml"].decode())
        self.assertEqual(self.db.open_journal(), [])

    def test_tailscale_key_is_minted_tagged_single_use_and_only_sent_on_stdin(self):
        self.lab().create("api")
        key_req = [p for p in self.io.posts if p["url"] == KEYS_URL][0]
        create = key_req["json"]["capabilities"]["devices"]["create"]
        self.assertEqual(create["tags"], ["tag:server", "tag:user-max"])
        self.assertEqual((create["ephemeral"], create["reusable"], create["preauthorized"]), (True, False, True))
        self.assertLessEqual(key_req["json"]["expirySeconds"], 600)
        self.assertEqual(key_req["headers"], {"Authorization": "Bearer at-1"})
        token_req = [p for p in self.io.posts if p["url"] == TOKEN_URL][0]
        self.assertEqual(token_req["form"]["grant_type"], "client_credentials")
        self.assertEqual(self.fake.stdin_seen, [KEY])
        everything = json.dumps(self.fake.calls) + "".join(self.fake.guest_log) + json.dumps(list(self.io.files.values()), default=str)
        self.assertNotIn(KEY, everything)  # never in argv, guest commands or written files
        up = [c for c in self.fake.guest_log if c.startswith("tailscale up")][0]
        self.assertIn("--ssh", up)
        self.assertIn("--hostname=lab-api", up)

    def test_size_and_overrides_applied(self):
        res = self.lab().create("big", size="large", ram="8G")
        vm = self.fake.vms[res["vmid"]]
        self.assertEqual((vm["config"]["cores"], vm["config"]["memory"]), ("8", "8192"))
        self.assertIn("size=64G", vm["config"]["scsi0"])  # grown from the 32G template disk

    def test_linked_clone_mode(self):
        res = self.lab().create("lnk", clone_mode="linked")
        self.assertEqual(self.fake.vms[res["vmid"]]["config"]["full"], "0")
        self.assertEqual(self.db.get_project("lnk")["clone_mode"], "linked")

    def test_no_tailscale_skips_enrolment(self):
        res = self.lab().create("lan", tailscale=False)
        self.assertEqual((res["state"], res["tailnet_ip"], res["ssh"]), ("running", None, None))
        self.assertEqual(self.io.posts, [])

    def test_missing_template_is_built_on_first_use(self):
        del self.fake.vms[9000]
        res = self.lab().create("api")
        self.assertEqual(res["state"], "running")
        self.assertTrue(any(v["template"] and v["name"] == "lab-tpl-ubuntu-24.04" for v in self.fake.vms.values()))

    def test_name_unavailable_reveals_nothing(self):
        self.lab().create("api")
        self.fake.add_vm(1500, "lab-foreign")
        for who in (self.max, self.bo):
            for name in ("api", "foreign"):
                with self.assertRaisesRegex(PreconditionError, "unavailable"):
                    self.lab(who).create(name)
        self.db.update_project("api", state="stopped")
        with self.assertRaises(UsageError):
            self.lab().create("Bad_Name")

    def test_only_admin_can_create_for_others(self):
        res = self.lab().create("for-bo", owner="bo")
        self.assertEqual(res["owner"], "bo")
        self.assertIn("lab;owner-bo", self.fake.vms[res["vmid"]]["tags"])
        with self.assertRaises(AccessError):
            self.lab(self.bo).create("sneaky", owner="max")
        with self.assertRaises(UsageError):
            self.lab().create("x", owner="nobody")


class RollbackTest(Base):
    def assert_clean(self, name="api"):
        self.assertIsNone(self.db.get_project(name))
        self.assertEqual(self.vm_names(), {"jellyfin", "lab-tpl-ubuntu-24.04"})  # others untouched, nothing leaked
        self.assertNotIn(f"/var/lib/vz/snippets/lab-{name}.yaml", self.io.files)
        self.assertEqual(self.db.open_journal(), [])

    def test_enrolment_failure_rolls_back_and_hides_the_key(self):
        self.fake.guest_results.insert(0, (lambda c: c.startswith("tailscale up"),
                                           {"exitcode": 1, "err-data": f"invalid key {KEY}"}))
        with self.assertRaises(TailscaleError) as cm:
            self.lab().create("api")
        self.assertNotIn(KEY, str(cm.exception))
        self.assert_clean()
        self.assertEqual(self.db.conn.execute("SELECT status FROM journal").fetchone()[0], "rolled_back")

    def test_clone_failure(self):
        self.fake.fail(("qm", "clone"), stderr="storage full")
        with self.assertRaises(LabError):
            self.lab().create("api")
        self.assert_clean()

    def test_agent_never_comes_up(self):
        self.fake.guest_agent_ready = False
        with self.assertRaises(LabError):
            self.lab().create("api")
        self.assert_clean()

    def test_first_boot_failure(self):
        self.fake.guest_results.insert(0, (lambda c: c.startswith("cloud-init status"),
                                           {"exitcode": 1, "err-data": "boom"}))
        with self.assertRaisesRegex(LabError, "first-boot"):
            self.lab().create("api")
        self.assert_clean()

    def test_oauth_rejected(self):
        self.io.post_replies[TOKEN_URL] = {}
        with self.assertRaisesRegex(TailscaleError, "rejected"):
            self.lab().create("api")
        self.assert_clean()

    def test_missing_oauth_file(self):
        del self.io.files["/etc/project-lab/ts-oauth"]
        with self.assertRaisesRegex(TailscaleError, "not found"):
            self.lab().create("api")
        self.assert_clean()


class QuotaTest(Base):
    def test_running_vm_limit_per_user_with_override(self):
        self.db.add_user("cy@x.io", "cy", "user", {"max_running_vms": 1})
        cy = Caller("cy@x.io", "cy", "user")
        self.lab(cy).create("one")
        with self.assertRaisesRegex(PreconditionError, "running VMs would be 2, limit 1"):
            self.lab(cy).create("two")
        self.assertIsNone(self.db.get_project("two"))
        self.lab(cy).stop("one")  # a stopped project no longer counts
        self.lab(cy).create("two")
        with self.assertRaisesRegex(PreconditionError, "running VMs"):
            self.lab(cy).start("one")

    def test_cpu_ram_and_disk_limits(self):
        lab = self.lab(self.bo)
        with self.assertRaisesRegex(PreconditionError, "vCPUs"):
            lab.create("a", cpu=9)
        with self.assertRaisesRegex(PreconditionError, "RAM"):
            lab.create("b", ram="32G")
        with self.assertRaisesRegex(PreconditionError, "fast-disk"):
            lab.create("c", disk="512G")
        self.assertEqual(self.db.list_projects("bo"), [])

    def test_admins_are_exempt(self):
        self.lab().create("huge", cpu=32, ram="128G", disk="2000G")

    def test_other_users_usage_is_separate(self):
        for i in range(4):
            self.lab(self.bo).create(f"b{i}")
        with self.assertRaises(PreconditionError):
            self.lab(self.bo).create("b4")
        self.lab(self.max).create("m0")


class PermissionTest(Base):
    def setUp(self):
        super().setUp()
        self.lab().create("api")

    def test_other_users_cannot_see_or_touch_a_project(self):
        lab = self.lab(self.bo)
        self.assertEqual(lab.list(), [])
        self.assertEqual(lab.list(everything=True), [])  # --all is admin only
        calls = (lambda: lab.status("api"), lambda: lab.start("api"), lambda: lab.stop("api"),
                 lambda: lab.delete("api", "api"), lambda: lab.set_protected("api", True))
        for call in calls:
            with self.assertRaisesRegex(PreconditionError, "no such project"):
                call()
        self.assertEqual(self.px.status(self.db.get_project("api")["vmid"]), "running")

    def test_owner_and_admin_can(self):
        self.lab(self.bo).create("mine")
        self.assertEqual([p["name"] for p in self.lab(self.bo).list()], ["mine"])
        self.assertEqual({p["name"] for p in self.lab().list(everything=True)}, {"api", "mine"})
        self.assertEqual({p["name"] for p in self.lab().list()}, {"api"})  # admin default: own only
        self.lab().stop("mine")  # admin manages anyone's


class StartStopTest(Base):
    def setUp(self):
        super().setUp()
        self.lab().create("api")
        self.vmid = self.db.get_project("api")["vmid"]

    def test_stop_logs_out_of_tailscale_then_shuts_down(self):
        res = self.lab().stop("api")
        self.assertEqual(res["state"], "stopped")
        self.assertIn("tailscale logout", self.fake.guest_log)
        self.assertEqual(self.fake.vms[self.vmid]["status"], "stopped")
        row = self.db.get_project("api")
        self.assertEqual((row["state"], row["tailnet_ip"]), ("stopped", None))
        self.assertEqual(row["tailnet_name"], "lab-api.tail1234.ts.net")  # name kept for the SSH shortcut

    def test_start_reenrols_with_a_new_key_and_updates_ip(self):
        self.lab().stop("api")
        tailscale_ok(self.fake, ip="100.64.0.9")
        res = self.lab().start("api")
        self.assertEqual((res["state"], res["tailnet_ip"]), ("running", "100.64.0.9"))
        self.assertEqual(len([p for p in self.io.posts if p["url"] == KEYS_URL]), 2)  # one key per enrolment

    def test_graceful_shutdown_timeout_is_not_forced(self):
        self.fake.ignore_shutdown = True
        with self.assertRaisesRegex(PreconditionError, "not forcing"):
            self.lab().stop("api")
        self.assertEqual(self.fake.vms[self.vmid]["status"], "running")
        self.assertEqual(self.db.get_project("api")["state"], "running")
        self.assertFalse(self.fake.called("qm", "stop"))
        self.lab().stop("api", force=True)
        self.assertEqual(self.fake.vms[self.vmid]["status"], "stopped")

    def test_start_is_idempotent_and_repairs_missing_enrolment(self):
        self.db.update_project("api", tailnet_ip=None)
        before = len(self.io.posts)
        self.lab().start("api")
        self.assertGreater(len(self.io.posts), before)  # running but not on the tailnet: re-enrol
        self.assertEqual(self.db.get_project("api")["tailnet_ip"], "100.64.0.5")
        n = len(self.io.posts)
        self.lab().start("api")
        self.assertEqual(len(self.io.posts), n)  # already running and enrolled: nothing to do

    def test_restart(self):
        self.lab().restart("api")
        self.assertEqual(self.db.get_project("api")["state"], "running")
        self.assertIn("tailscale logout", self.fake.guest_log)

    def test_archived_cannot_start(self):
        self.db.update_project("api", state="archived")
        with self.assertRaisesRegex(PreconditionError, "archived"):
            self.lab().start("api")

    def test_stop_without_tailnet_does_not_touch_tailscale(self):
        self.lab().create("lan", tailscale=False)
        self.fake.guest_log.clear()
        self.lab().stop("lan")
        self.assertNotIn("tailscale logout", self.fake.guest_log)


class DeleteTest(Base):
    def setUp(self):
        super().setUp()
        self.lab().create("api")
        self.lab().create("other")
        self.vmid = self.db.get_project("api")["vmid"]

    def test_requires_typed_name(self):
        with self.assertRaises(UsageError):
            self.lab().delete("api", "yes")
        self.assertIn(self.vmid, self.fake.vms)

    def test_deletes_running_vm_cleanly_and_nothing_else(self):
        self.lab().delete("api", "api")
        self.assertNotIn(self.vmid, self.fake.vms)
        self.assertIsNone(self.db.get_project("api"))
        self.assertNotIn("/var/lib/vz/snippets/lab-api.yaml", self.io.files)
        self.assertIn("tailscale logout", self.fake.guest_log)
        self.assertEqual(self.vm_names(), {"jellyfin", "lab-tpl-ubuntu-24.04", "lab-other"})
        self.assertEqual(self.db.open_journal(), [])

    def test_protected_must_be_unprotected_first(self):
        lab = self.lab()
        lab.set_protected("api", True)
        with self.assertRaisesRegex(PreconditionError, "protected"):
            lab.delete("api", "api")
        lab.set_protected("api", False)
        lab.delete("api", "api")

    def test_refuses_when_the_vm_is_not_what_lab_created(self):
        self.fake.vms[self.vmid]["name"] = "something-else"
        with self.assertRaisesRegex(PreconditionError, "does not match"):
            self.lab().delete("api", "api")
        self.assertIn(self.vmid, self.fake.vms)
        self.fake.vms[self.vmid]["name"] = "lab-api"
        self.fake.vms[self.vmid]["tags"] = ""
        with self.assertRaisesRegex(PreconditionError, "does not match"):
            self.lab().delete("api", "api")
        self.assertIsNotNone(self.db.get_project("api"))

    def test_failed_shutdown_keeps_everything(self):
        self.fake.ignore_shutdown = True
        with self.assertRaisesRegex(PreconditionError, "not forcing"):
            self.lab().delete("api", "api")
        self.assertIn(self.vmid, self.fake.vms)
        self.assertEqual(self.db.get_project("api")["state"], "running")
        self.assertEqual(self.db.conn.execute("SELECT status FROM journal WHERE op='delete'").fetchone()[0], "failed")

    def test_vm_already_gone_is_cleaned_from_the_index(self):
        del self.fake.vms[self.vmid]
        self.lab().delete("api", "api")
        self.assertIsNone(self.db.get_project("api"))

    def test_archived_project_is_not_deleted_here(self):
        self.db.update_project("api", state="archived")
        with self.assertRaisesRegex(PreconditionError, "archived"):
            self.lab().delete("api", "api")


class CallerTest(unittest.TestCase):
    def setUp(self):
        self.db = DB(":memory:")
        self.db.add_user("max@x.io", "max", "admin")
        self.db.add_user("bo@x.io", "bo", "user")

    def who(self, login):
        out = json.dumps({"UserProfile": {"LoginName": login}}) if login else ""
        return StubRunner(Result([], 0 if login else 1, out))

    def test_ssh_identity_must_be_a_lab_user(self):
        env = {"SSH_CONNECTION": "100.64.0.9 1 100.64.0.1 22"}
        c = access.resolve_caller(self.db, env, self.who("bo@x.io"), local_root=False)
        self.assertEqual((c.name, c.role), ("bo", "user"))
        with self.assertRaisesRegex(AccessError, "not a lab user"):
            access.resolve_caller(self.db, env, self.who("eve@x.io"), local_root=False)
        with self.assertRaisesRegex(AccessError, "cannot determine"):
            access.resolve_caller(self.db, env, self.who(None), local_root=False)

    def test_local_root_is_the_first_admin_only_without_ssh(self):
        c = access.resolve_caller(self.db, {}, self.who(None), local_root=True)
        self.assertEqual(c.name, "max")
        with self.assertRaises(AccessError):
            access.resolve_caller(self.db, {}, self.who(None), local_root=False)
        # root over ssh is still identified by tailnet login, not promoted
        env = {"SSH_CONNECTION": "100.64.0.9 1 100.64.0.1 22"}
        self.assertEqual(access.resolve_caller(self.db, env, self.who("bo@x.io"), local_root=True).name, "bo")

    def test_as_name_is_admin_only(self):
        env = {"SSH_CONNECTION": "100.64.0.9 1 100.64.0.1 22"}
        c = access.resolve_caller(self.db, env, self.who("max@x.io"), as_name="bo", local_root=False)
        self.assertEqual((c.name, c.role), ("bo", "user"))  # acts with bo's permissions, not admin's
        with self.assertRaisesRegex(AccessError, "only admins"):
            access.resolve_caller(self.db, env, self.who("bo@x.io"), as_name="max", local_root=False)
        with self.assertRaises(UsageError):
            access.resolve_caller(self.db, env, self.who("max@x.io"), as_name="ghost", local_root=False)

    def test_user_management(self):
        admin = Caller("max@x.io", "max", "admin")
        user = Caller("bo@x.io", "bo", "user")
        access.add_user(self.db, admin, "cy@x.io", "cy")
        self.assertEqual(self.db.get_user("cy@x.io")["role"], "user")
        with self.assertRaises(AccessError):
            access.add_user(self.db, user, "dan@x.io", "dan")
        with self.assertRaises(PreconditionError):
            access.add_user(self.db, admin, "cy@x.io", "cy2")  # login taken
        with self.assertRaises(PreconditionError):
            access.add_user(self.db, admin, "new@x.io", "cy")  # name taken
        with self.assertRaises(UsageError):
            access.add_user(self.db, admin, "e@x.io", "Not Valid")
        self.db.add_project(name="p", vmid=1000, owner="cy", distro="d", cpu=1, ram_mb=1, disk_gb=1)
        with self.assertRaisesRegex(PreconditionError, "still owns"):
            access.remove_user(self.db, admin, "cy")
        with self.assertRaisesRegex(PreconditionError, "last admin"):
            access.remove_user(self.db, admin, "max")
        access.remove_user(self.db, admin, "bo")


if __name__ == "__main__":
    unittest.main()
