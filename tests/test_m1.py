import hashlib
import json
import unittest

from lab import config as config_mod
from lab import distros, doctor, tsacl
from lab import init as init_mod
from lab.db import DB
from lab.errors import PreconditionError, UsageError, VerificationError
from lab.identity import caller_login
from lab.prompt import ScriptedPrompter
from lab.proxmox import Proxmox
from lab.runner import Result, Runner
from lab.template import TemplateBuilder, TemplateError, build_user_data, parse_description

from .fake_io import FakeIO
from .fake_proxmox import FakeProxmox

IMAGE = b"fake-cloud-image"
CATALOGUE = distros.load(override=None)
UBUNTU = CATALOGUE["ubuntu-24.04"]


def make_cfg(**over):
    cfg = config_mod.Config()
    cfg.storage.fast_disk, cfg.storage.archive_disk, cfg.storage.snippet_disk = "nvme", "bigdisk", "local"
    cfg.network.bridge = "vmbr0"
    for k, v in over.items():
        setattr(cfg.vm, k, v)
    return cfg.validate()


def make_io(distro=UBUNTU, image=IMAGE):
    io = FakeIO()
    digest = hashlib.new(distro.checksum_type, image).hexdigest()
    io.urls[distro.checksum_url] = f"{digest} *{distro.checksum_name}\n"
    io.urls[distro.image_url] = image
    io.files["/etc/project-lab/ts-oauth"] = b'{"client_id": "cid", "client_secret": "csecret"}'
    return io


class DistroTest(unittest.TestCase):
    def test_bundled_catalogue(self):
        for want in ("ubuntu-22.04", "ubuntu-24.04", "ubuntu-26.04", "debian-12", "debian-13", "rocky-9", "almalinux-9"):
            self.assertIn(want, CATALOGUE)
        self.assertEqual(CATALOGUE["debian-12"].checksum_type, "sha512")
        self.assertEqual(CATALOGUE["rocky-9"].family, "dnf")
        self.assertIn("build-essential", UBUNTU.packages)
        self.assertIn("gcc", CATALOGUE["rocky-9"].packages)
        self.assertEqual(UBUNTU.template_name, "lab-tpl-ubuntu-24.04")
        with self.assertRaises(UsageError):
            distros.get("plan9", CATALOGUE)

    def test_checksum_formats(self):
        h = "a" * 64
        self.assertEqual(distros.find_checksum(f"{h}  x.img\n", "x.img"), h)
        self.assertEqual(distros.find_checksum(f"{h} *x.img\n", "x.img"), h)
        self.assertEqual(distros.find_checksum(f"# c\nSHA256 (x.img) = {h.upper()}\n", "x.img"), h)
        with self.assertRaises(UsageError):
            distros.find_checksum(f"{h}  other.img\n", "x.img")
        with self.assertRaises(UsageError):
            distros.find_checksum(f"{'a' * 40}  x.img\n", "x.img")  # wrong digest length
        with self.assertRaises(UsageError):
            distros.find_checksum(f"{'a' * 64}  x.img\n", "x.img", "sha512")

    def test_user_data_per_family(self):
        apt = build_user_data(UBUNTU)
        self.assertTrue(apt.startswith("#cloud-config"))
        self.assertIn("qemu-guest-agent", apt)
        self.assertIn('"ssh"', apt)
        dnf = build_user_data(CATALOGUE["rocky-9"])
        self.assertIn('"sshd"', dnf)
        self.assertIn("tailscale.com/install.sh", apt)
        self.assertNotIn("auth", apt.lower().replace("authenticated", ""))  # never authenticates in the template


class BuilderBase(unittest.TestCase):
    def setUp(self):
        self.fake = FakeProxmox()
        self.fake.path_root = "/var/lib/vz"
        self.io = make_io()
        self.cfg = make_cfg()
        self.db = DB(":memory:")
        self.ticks = 0

        def mono():
            self.ticks += 100
            return self.ticks

        self.tb = TemplateBuilder(self.cfg, Proxmox(self.fake), self.io, self.db, catalogue=CATALOGUE,
                                  sleep=lambda s: None, monotonic=mono, agent_timeout=500)


class TemplateBuildTest(BuilderBase):
    def test_successful_build(self):
        vmid = self.tb.build("ubuntu-24.04")
        lo, hi = self.cfg.vm.template_range
        self.assertTrue(lo <= vmid <= hi)
        vm = self.fake.vms[vmid]
        self.assertTrue(vm["template"])
        self.assertEqual(vm["name"], "lab-tpl-ubuntu-24.04")
        self.assertNotIn("cicustom", vm["config"])
        meta = parse_description(self.tb.px.config(vmid)["description"])
        self.assertEqual(meta["distro"], "ubuntu-24.04")
        self.assertEqual(meta["sha256"], hashlib.sha256(IMAGE).hexdigest())
        self.assertIn("nvme:0,import-from=", vm["config"]["scsi0"])
        self.assertIn("bridge=vmbr0", vm["config"]["net0"])
        # snippet was written under the snippet storage then removed
        snippet = "/var/lib/vz/snippets/lab-build-ubuntu-24.04.yaml"
        self.assertIn(snippet, self.io.removed)
        self.assertNotIn(snippet, self.io.files)
        joined = "\n".join(self.fake.guest_log)
        for step in ("cloud-init status --wait", "git --version", "tailscale version", "cloud-init clean"):
            self.assertIn(step, joined)
        self.assertEqual(self.tb.default_template(), vmid)
        self.assertEqual([r["present"] for r in self.tb.list() if r["distro"] == "ubuntu-24.04"], [True])

    def test_requires_complete_config(self):
        self.tb.cfg.storage.snippet_disk = ""
        with self.assertRaises(PreconditionError):
            self.tb.build("ubuntu-24.04")

    def test_existing_template_needs_update(self):
        self.tb.build("ubuntu-24.04")
        with self.assertRaises(PreconditionError):
            self.tb.build("ubuntu-24.04")

    def test_checksum_mismatch_deletes_image_and_creates_nothing(self):
        self.io.urls[UBUNTU.image_url] = b"tampered"
        with self.assertRaises(VerificationError):
            self.tb.build("ubuntu-24.04")
        self.assertEqual(self.fake.vms, {})
        self.assertFalse(any(p.endswith(UBUNTU.image_file) for p in self.io.files))

    def test_cached_image_is_reused(self):
        self.tb.build("ubuntu-24.04")
        self.tb.remove("ubuntu-24.04")
        self.io.urls[UBUNTU.image_url] = b"would-fail-checksum-if-downloaded"
        self.tb.build("ubuntu-24.04")  # cache hit; download not attempted

    def test_failed_toolchain_check_cleans_up_build_vm(self):
        self.fake.add_vm(100, "jellyfin", status="running")
        self.fake.guest_results.append((lambda c: "git --version" in c, {"exitcode": 1, "err-data": "git: not found"}))
        with self.assertRaisesRegex(TemplateError, "toolchain verification failed"):
            self.tb.build("ubuntu-24.04")
        self.assertEqual(set(self.fake.vms), {100})  # build VM destroyed, others untouched
        self.assertNotIn("/var/lib/vz/snippets/lab-build-ubuntu-24.04.yaml", self.io.files)

    def test_agent_timeout(self):
        self.fake.guest_agent_ready = False
        with self.assertRaisesRegex(TemplateError, "guest agent did not come up"):
            self.tb.build("ubuntu-24.04")
        self.assertEqual(self.fake.vms, {})

    def test_cleanup_never_destroys_a_vm_it_did_not_create(self):
        self.tb.build("ubuntu-24.04")
        tpl = self.tb.find("ubuntu-24.04")["vmid"]
        self.tb._discard_build(int(tpl), "lab-tpl-ubuntu-24.04-building")  # name mismatch + template flag
        self.assertIn(int(tpl), self.fake.vms)


class TemplateUpdateTest(BuilderBase):
    def test_update_swaps_template(self):
        old = self.tb.build("ubuntu-24.04")
        new = self.tb.update("ubuntu-24.04")
        self.assertNotEqual(old, new)
        self.assertNotIn(old, self.fake.vms)
        self.assertEqual(self.tb.find("ubuntu-24.04")["vmid"], new)

    def test_failed_update_keeps_old_template(self):
        old = self.tb.build("ubuntu-24.04")
        self.fake.guest_results.append((lambda c: "git --version" in c, {"exitcode": 1, "err-data": "boom"}))
        with self.assertRaises(TemplateError):
            self.tb.update("ubuntu-24.04")
        self.assertEqual(self.tb.find("ubuntu-24.04")["vmid"], old)

    def test_linked_clones_block_update_and_remove(self):
        old = self.tb.build("ubuntu-24.04")
        self.db.add_user("m@x.io", "max", "admin")
        self.db.add_project(name="a", vmid=1000, owner="max", distro="ubuntu-24.04", cpu=1, ram_mb=2048,
                            disk_gb=32, template_id=old, clone_mode="linked")
        for op in (self.tb.update, self.tb.remove):
            with self.assertRaisesRegex(PreconditionError, "linked clones"):
                op("ubuntu-24.04")
        self.assertIn(old, self.fake.vms)


class DoctorTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeProxmox()
        self.io = make_io()
        self.cfg = make_cfg()
        self.px = Proxmox(self.fake)
        self.db = DB(":memory:")
        self.tb = TemplateBuilder(self.cfg, self.px, self.io, self.db, catalogue=CATALOGUE)

    def run_doctor(self):
        return {c.name: c for c in doctor.run_checks(self.cfg, self.px, self.io, self.db, self.tb)}

    def test_healthy_host_has_no_failures(self):
        checks = self.run_doctor()
        self.assertEqual(doctor.summarize(checks.values()), 0)
        self.assertEqual(checks["default template"].level, doctor.WARN)  # not built yet is only a warning

    def test_unset_config_fails_and_points_to_init(self):
        self.cfg.storage.fast_disk = ""
        c = self.run_doctor()["config complete"]
        self.assertEqual(c.level, doctor.FAIL)
        self.assertIn("lab init", c.hint)

    def test_wrong_content_type_missing_storage_and_bridge(self):
        self.cfg.storage.archive_disk = "nvme"  # nvme has no backup content
        self.cfg.storage.snippet_disk = "ghost"
        self.cfg.network.bridge = "vmbr9"
        checks = self.run_doctor()
        self.assertEqual(checks["archive storage"].level, doctor.FAIL)
        self.assertIn("not found", checks["snippet storage"].detail)
        self.assertEqual(checks["bridge"].level, doctor.FAIL)

    def test_low_space(self):
        self.fake.storages["nvme"]["used"] = (1 << 40) - (5 << 30)
        self.assertEqual(self.run_doctor()["fast storage"].level, doctor.FAIL)

    def test_archive_disk_on_root_filesystem_fails(self):
        self.io.dirs["/mnt/bigdisk"] = []
        self.io.root_device_paths.add("/mnt/bigdisk")
        c = self.run_doctor()["archive disk mounted"]
        self.assertEqual(c.level, doctor.FAIL)
        self.assertIn("root filesystem", c.detail)

    def test_drift_and_orphans(self):
        self.db.add_user("m@x.io", "max", "admin")
        self.db.add_project(name="gone", vmid=1000, owner="max", distro="ubuntu-24.04", cpu=1, ram_mb=2048,
                            disk_gb=32, state="running")
        self.fake.add_vm(1001, "lab-stray", tags="lab")           # tagged, not indexed
        self.fake.add_vm(1002, "somebody-elses")                   # foreign VM inside lab's range
        self.fake.add_vm(1003, "lab-verify-api")                   # leftover temp VM
        checks = self.run_doctor()
        self.assertEqual(checks["index vs proxmox"].level, doctor.FAIL)
        self.assertIn("gone", checks["index vs proxmox"].detail)
        self.assertIn("lab-stray", checks["untracked lab VMs"].detail)
        self.assertIn("somebody-elses", checks["vmid range clear"].detail)
        self.assertIn("lab-verify-api", checks["leftover temp VMs"].detail)
        self.assertEqual(doctor.summarize(checks.values()), 5)

    def test_missing_archive_backup_and_interrupted_ops(self):
        self.db.add_user("m@x.io", "max", "admin")
        self.db.add_archive("old", "max", "bigdisk:backup/vzdump-qemu-1-0001.vma.zst", verified=True)
        self.db.journal_begin("old", "archive")
        checks = self.run_doctor()
        self.assertEqual(checks["archives present"].level, doctor.FAIL)
        self.assertEqual(checks["interrupted operations"].level, doctor.WARN)

    def test_oauth_file_permissions(self):
        self.io.modes["/etc/project-lab/ts-oauth"] = 0o644
        self.assertEqual(self.run_doctor()["tailscale oauth credential"].level, doctor.FAIL)
        del self.io.files["/etc/project-lab/ts-oauth"]
        self.assertEqual(self.run_doctor()["tailscale oauth credential"].level, doctor.WARN)

    def test_render(self):
        text = doctor.render(doctor.run_checks(self.cfg, self.px, self.io, self.db, self.tb))
        self.assertIn("[ok  ] proxmox version: 9.2.1", text)


class StubRunner(Runner):
    def __init__(self, result):
        self.result, self.calls = result, []

    def run(self, argv, **kw):
        self.calls.append(list(argv))
        return self.result


class IdentityAclTest(unittest.TestCase):
    def test_caller_login(self):
        out = json.dumps({"UserProfile": {"LoginName": "max@example.com"}})
        r = StubRunner(Result([], 0, out))
        self.assertEqual(caller_login({"SSH_CONNECTION": "100.64.0.9 5555 100.64.0.1 22"}, r), "max@example.com")
        self.assertEqual(r.calls[0], ["tailscale", "whois", "--json", "100.64.0.9"])
        self.assertIsNone(caller_login({}, r))
        self.assertIsNone(caller_login({"SSH_CONNECTION": "1.2.3.4 1 5.6.7.8 22"}, StubRunner(Result([], 1))))
        self.assertIsNone(caller_login({"SSH_CONNECTION": "1.2.3.4 1 5.6.7.8 22"}, StubRunner(Result([], 0, "{}"))))

    def test_acl_snippet(self):
        s = tsacl.acl_snippet("max@example.com", {"max": "max@example.com"})
        body = json.loads("\n".join(l for l in s.splitlines() if not l.startswith("//")))
        self.assertIn("tag:user-max", body["tagOwners"])
        self.assertIn("tag:server", body["tagOwners"])
        users_rule = [r for r in body["ssh"] if r["dst"] == ["tag:user-max"]][0]
        self.assertEqual((users_rule["src"], users_rule["users"]), (["max@example.com"], ["max"]))


class InitTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeProxmox()
        self.io = make_io()
        self.io.files.pop("/etc/project-lab/ts-oauth")
        self.px = Proxmox(self.fake)
        self.db = DB(":memory:")
        self.env = {}  # not via SSH: identity is asked

    def run_init(self, answers, **kw):
        ui = ScriptedPrompter({"Your tailnet login": "max@example.com", **answers}, assume_yes=kw.pop("yes", True))
        res = init_mod.run_init(self.px, self.io, StubRunner(Result([], 1)), ui, existing=config_mod.Config(),
                                config_file="/etc/project-lab/config.toml", db=self.db, env=self.env, **kw)
        return res, ui

    def test_picks_existing_storages_and_writes_config(self):
        res, _ = self.run_init({"Fast disk": "nvme", "Archive disk": "bigdisk"})
        written = config_mod.from_dict(__import__("tomllib").loads(self.io.files["/etc/project-lab/config.toml"].decode()))
        self.assertEqual((written.storage.fast_disk, written.storage.archive_disk, written.storage.snippet_disk),
                         ("nvme", "bigdisk", "local"))
        self.assertEqual(written.network.bridge, "vmbr0")
        self.assertEqual(self.db.get_user("max@example.com")["role"], "admin")
        self.assertEqual(self.db.get_user("max@example.com")["name"], "max")
        self.assertTrue(any("OAuth" in n for n in res.notes))
        self.assertIn("tag:user-max", res.acl_snippet)

    def test_nothing_written_without_confirmation(self):
        with self.assertRaises(PreconditionError):
            self.run_init({"Fast disk": "nvme", "Archive disk": "bigdisk"}, yes=False)
        self.assertNotIn("/etc/project-lab/config.toml", self.io.files)
        self.assertEqual(self.db.list_users(), [])

    def test_fast_disk_must_support_images(self):
        with self.assertRaises(UsageError):
            self.run_init({"Fast disk": "bigdisk", "Archive disk": "bigdisk"})

    def test_new_directory_storage_uses_dedicated_subfolder_and_mountpoint(self):
        del self.fake.storages["bigdisk"]
        res, ui = self.run_init({"Fast disk": "nvme", "Archive disk": init_mod.NEW_DIR,
                                 "Mount path": "/mnt/hdd", "Storage ID": "lab-archive"})
        self.assertEqual(res.config.storage.archive_disk, "lab-archive")
        self.assertEqual(self.io.made, ["/mnt/hdd/project-lab-archive"])
        add = self.fake.called("pvesm", "add")[0]
        self.assertEqual(add[add.index("--path") + 1], "/mnt/hdd/project-lab-archive")
        self.assertEqual(add[add.index("--is_mountpoint") + 1], "/mnt/hdd")
        self.assertEqual(add[add.index("--content") + 1], "backup")
        self.assertTrue(any("already contains 2 item" in m for m in ui.log))  # existing data is called out
        self.assertEqual(self.io.dirs["/mnt/hdd"], ["media", "backups"])  # untouched

    def test_refuses_unmounted_archive_disk(self):
        del self.fake.storages["bigdisk"]
        self.io.root_device_paths.add("/mnt/hdd")
        with self.assertRaisesRegex(PreconditionError, "root filesystem"):
            self.run_init({"Fast disk": "nvme", "Archive disk": init_mod.NEW_DIR, "Mount path": "/mnt/hdd"})
        self.assertFalse(self.fake.called("pvesm", "add"))
        self.assertEqual(self.io.made, [])

    def test_declined_storage_command_changes_nothing(self):
        del self.fake.storages["bigdisk"]
        with self.assertRaises(PreconditionError):
            self.run_init({"Fast disk": "nvme", "Archive disk": init_mod.NEW_DIR, "Mount path": "/mnt/hdd",
                           "Proceed?": False}, yes=False)
        self.assertFalse(self.fake.called("pvesm", "add"))
        self.assertEqual(self.io.made, [])

    def test_needs_snippets_storage(self):
        del self.fake.storages["local"]
        with self.assertRaisesRegex(PreconditionError, "snippets"):
            self.run_init({"Fast disk": "nvme", "Archive disk": "bigdisk"})


if __name__ == "__main__":
    unittest.main()
