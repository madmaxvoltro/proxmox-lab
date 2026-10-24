import json
import tempfile
import unittest
from pathlib import Path

from lab import config as config_mod
from lab.db import DB, DBError
from lab.errors import CommandError, PreconditionError, UsageError
from lab.locking import lock
from lab.proxmox import Proxmox
from lab.runner import DryRunRunner, is_read_only
from lab.units import hostname, parse_size, to_gib, to_mib, validate_name

from .fake_proxmox import FakeProxmox


class UnitsTest(unittest.TestCase):
    def test_sizes(self):
        self.assertEqual(to_mib("2G"), 2048)
        self.assertEqual(to_gib("32GiB"), 32)
        self.assertEqual(parse_size("512M"), 512 << 20)
        with self.assertRaises(UsageError):
            parse_size("lots")

    def test_names(self):
        self.assertEqual(hostname("api-test"), "lab-api-test")
        for bad in ("", "Api", "1x", "a_b", "x-", "a" * 40, "-a"):
            with self.assertRaises(UsageError, msg=bad):
                validate_name(bad)
        validate_name("a")
        validate_name("ab")


class ConfigTest(unittest.TestCase):
    def test_defaults_match_spec(self):
        c = config_mod.Config().validate()
        self.assertEqual((c.vm.default_cores, c.vm.default_memory, c.vm.default_disk), (1, 2048, "32G"))
        self.assertEqual(c.vm.default_distro, "ubuntu-24.04")
        self.assertEqual(c.storage.fast_disk, "")  # never assume storage IDs
        with self.assertRaises(UsageError):
            c.require_storage()

    def test_load_toml(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.toml"
            p.write_text('[storage]\nfast_disk="nv"\narchive_disk="ar"\n[network]\nbridge="vmbr0"\n'
                         '[vm]\nvmid_range=[2000, 2100]\n[presets]\nhuge={cpu=16, ram="64G", disk="128G"}\n')
            c = config_mod.load(str(p))
            c.require_storage()
            self.assertEqual(c.vm.vmid_range, (2000, 2100))
            self.assertIn("huge", c.presets)
            self.assertIn("small", c.presets)

    def test_rejects_typos_and_bad_values(self):
        for bad in ({"storage": {"fast_dsk": "x"}}, {"bogus": {}}, {"vm": {"clone_mode": "thin"}},
                    {"vm": {"vmid_range": [50, 60]}}, {"vm": {"vmid_range": [9000, 9100], "template_range": [9050, 9060]}}):
            with self.assertRaises(UsageError, msg=str(bad)):
                config_mod.from_dict(bad)

    def test_project_cannot_choose_disks(self):
        ok = config_mod.project_request({"distro": "debian-12", "cpu": 4, "ram": "8G", "disk": "64G"})
        self.assertEqual(ok["cpu"], 4)
        for bad in ({"fast_disk": "nvme"}, {"storage": "bigdisk"}, {"archive_disk": "x"},
                    {"bridge": "vmbr9"}, {"state_dir": "/tmp"}, {"cpu": 2, "snippet_disk": "local"}):
            with self.assertRaises(UsageError, msg=str(bad)):
                config_mod.project_request(bad)
        with self.assertRaises(UsageError):
            config_mod.project_request({"clone_mode": "thin"})

    def test_dumps_roundtrips(self):
        import tomllib
        c = config_mod.Config()
        c.storage.fast_disk = "nv"
        c.vm.vmid_range = (2000, 2100)
        again = config_mod.from_dict(tomllib.loads(config_mod.dumps(c)))
        self.assertEqual(again.to_dict(), c.to_dict())

    def test_missing_file_gives_defaults(self):
        self.assertEqual(config_mod.load("/nonexistent/x.toml").vm.default_cores, 1)


class DBTest(unittest.TestCase):
    def setUp(self):
        self.db = DB(":memory:")
        self.db.add_user("max@example.com", "max", "admin")

    def project(self, name="api", **kw):
        base = dict(name=name, vmid=1000, owner="max", distro="ubuntu-24.04", cpu=1, ram_mb=2048, disk_gb=32)
        base.update(kw)
        self.db.add_project(**base)

    def test_project_roundtrip(self):
        self.project()
        p = self.db.get_project("api")
        self.assertEqual((p["state"], p["cpu"], p["protected"]), ("creating", 1, 0))
        self.db.update_project("api", state="running", tailnet_ip="100.64.0.1")
        self.assertEqual(self.db.get_project("api")["state"], "running")
        self.assertEqual(self.db.used_vmids(), {1000})

    def test_constraints(self):
        self.project()
        with self.assertRaises(Exception):
            self.project("other", vmid=1000)  # duplicate vmid
        with self.assertRaises(Exception):
            self.project("ghost", vmid=1001, owner="nobody")  # FK to users
        with self.assertRaises(DBError):
            self.db.update_project("api", state="bogus")
        with self.assertRaises(DBError):
            self.db.update_project("missing", state="running")

    def test_owner_filter_and_roles(self):
        self.db.add_user("bo@example.com", "bo")
        self.project("a", vmid=1000)
        self.project("b", vmid=1001, owner="bo")
        self.assertEqual([r["name"] for r in self.db.list_projects("bo")], ["b"])
        self.assertEqual(len(self.db.list_projects()), 2)
        with self.assertRaises(DBError):
            self.db.add_user("x@example.com", "x", "root")

    def test_transaction_rolls_back(self):
        try:
            with self.db.tx():
                self.db.conn.execute("DELETE FROM users")
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        self.assertEqual(len(self.db.list_users()), 1)

    def test_journal_and_archives(self):
        jid = self.db.journal_begin("api", "archive", actor="max")
        self.db.journal_step(jid, "backup")
        self.assertEqual([r["step"] for r in self.db.open_journal()], ["backup"])
        self.db.journal_finish(jid, "rolled_back", "verify failed")
        self.assertEqual(self.db.open_journal(), [])
        aid = self.db.add_archive("api", "max", "bigdisk:backup/x", 123, "ab", verified=True)
        self.assertIsNotNone(self.db.list_archives("api")[0]["verified_at"])
        self.db.delete_archive(aid)
        self.assertEqual(self.db.list_archives(), [])

    def test_schema_version_and_reopen(self):
        with tempfile.TemporaryDirectory() as d:
            path = str(Path(d) / "sub" / "lab.db")
            DB(path).close()
            db2 = DB(path)
            self.assertEqual(db2.conn.execute("PRAGMA user_version").fetchone()[0], 1)
            db2.close()


class RunnerTest(unittest.TestCase):
    def test_read_only_classification(self):
        for argv in (["qm", "list"], ["qm", "status", "1"], ["pvesh", "get", "/x"], ["pvesm", "status"],
                     ["/usr/sbin/qm", "config", "1"], ["pveversion"]):
            self.assertTrue(is_read_only(argv), argv)
        for argv in (["qm", "start", "1"], ["qm", "destroy", "1"], ["vzdump", "1"], ["qmrestore", "a", "1"],
                     ["pvesh", "create", "/x"], ["rm", "-rf", "/"], ["qm"]):
            self.assertFalse(is_read_only(argv), argv)

    def test_dry_run_executes_reads_skips_writes(self):
        fake = FakeProxmox()
        fake.add_vm(9000, "tpl", template=True)
        dry = DryRunRunner(fake)
        px = Proxmox(dry)
        self.assertEqual(len(px.vms()), 1)  # read executed
        px.clone(9000, 1000, "lab-x", full=True, storage="nvme")
        px.start(1000)
        px.destroy(9000)
        self.assertEqual(len(fake.vms), 1)  # nothing changed
        self.assertEqual([a[1] for a in dry.skipped], ["clone", "start", "destroy"])
        self.assertFalse(fake.called("qm", "clone"))


class ProxmoxTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeProxmox()
        self.fake.add_vm(9000, "tpl", template=True)
        self.fake.add_vm(100, "jellyfin", status="running")
        self.px = Proxmox(self.fake)

    def test_discovery(self):
        self.assertEqual(self.px.version(), "9.2.1")
        ids = {s["storage"] for s in self.px.storages()}
        self.assertEqual(ids, {"nvme", "bigdisk"})
        self.assertEqual(self.px.vm(100)["name"], "jellyfin")
        self.assertIsNone(self.px.vm(5))

    def test_nextid_respects_range_proxmox_and_index(self):
        self.fake.add_vm(1000, "taken")
        self.assertEqual(self.px.nextid(1000, 1010), 1001)
        self.assertEqual(self.px.nextid(1000, 1010, taken={1001, 1002}), 1003)
        with self.assertRaises(PreconditionError):
            self.px.nextid(1000, 1000)

    def test_clone_configure_start_stop_destroy(self):
        self.px.clone(9000, 1000, "lab-api", full=True, storage="nvme")
        self.px.set(1000, cores=4, memory=8192)
        self.px.resize(1000, "scsi0", "64G")
        self.px.start(1000)
        self.assertEqual(self.px.status(1000), "running")
        cfg = self.px.config(1000)
        self.assertEqual((cfg["cores"], cfg["memory"], cfg["name"]), ("4", "8192", "lab-api"))
        self.assertIn("size=64G", cfg["scsi0"])
        with self.assertRaises(CommandError):
            self.px.destroy(1000)  # still running
        self.px.shutdown(1000)
        self.px.destroy(1000)
        self.assertIsNone(self.px.vm(1000))

    def test_clone_errors_surface(self):
        with self.assertRaises(CommandError):
            self.px.clone(100, 1000, "x", full=True)  # not a template

    def test_backup_restore_roundtrip(self):
        self.px.clone(9000, 1000, "lab-api", full=True)
        volid = self.px.vzdump(1000, "bigdisk")
        self.assertTrue(volid.startswith("bigdisk:backup/"))
        self.assertEqual(self.px.backups("bigdisk", 1000)[0]["volid"], volid)
        self.px.restore(volid, 1050, "nvme")
        self.assertEqual(self.px.vm(1050)["name"], "lab-api")

    def test_vzdump_failure_and_corrupt_restore(self):
        self.px.clone(9000, 1000, "lab-api", full=True)
        with self.assertRaises(CommandError):
            self.px.vzdump(1000, "nvme")  # storage has no backup content
        volid = self.px.vzdump(1000, "bigdisk")
        self.fake.backups[volid]["corrupt"] = True
        with self.assertRaises(CommandError):
            self.px.restore(volid, 1050, "nvme")
        self.fake.fail("vzdump", stderr="no space left")
        with self.assertRaises(CommandError):
            self.px.vzdump(1000, "bigdisk")

    def test_guest_exec_secret_goes_to_stdin_not_argv(self):
        self.px.clone(9000, 1000, "lab-api", full=True)
        self.px.start(1000)
        secret = "tskey-auth-SECRET"
        self.px.guest_exec(1000, ["tailscale", "up", "--auth-key=file:/dev/stdin"], stdin=secret)
        call = self.fake.called("qm", "guest")[0]
        self.assertNotIn(secret, " ".join(call))
        self.assertIn("--pass-stdin", call)
        self.assertEqual(self.fake.stdin_seen, [secret])
        self.px.shutdown(1000)
        with self.assertRaises(CommandError):
            self.px.guest_exec(1000, ["true"])  # agent unavailable when stopped


class LockTest(unittest.TestCase):
    def test_second_lock_fails_fast(self):
        with tempfile.TemporaryDirectory() as d:
            path = str(Path(d) / "x.lock")
            with lock(path):
                with self.assertRaises(PreconditionError):
                    with lock(path):
                        pass
            with lock(path):  # released
                pass


class CLITest(unittest.TestCase):
    def test_version_and_config(self):
        import contextlib
        import io

        from lab.cli import main

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(main(["--json", "version"]), 0)
        self.assertEqual(json.loads(buf.getvalue())["version"], "0.0.1")

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(main(["--json", "--config", "/nonexistent.toml", "config"]), 0)
        self.assertEqual(json.loads(buf.getvalue())["vm"]["default_memory"], 2048)


if __name__ == "__main__":
    unittest.main()
