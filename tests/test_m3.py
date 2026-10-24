import json
import unittest

from lab.errors import AccessError, LabError, PreconditionError, VerificationError

from .test_m2 import KEY, Base, tailscale_ok

TEMPLATE_NET0 = "virtio=AA:BB:CC:DD:EE:01,bridge=vmbr0"


class ArchiveBase(Base):
    def setUp(self):
        super().setUp()
        self.fake.vms[9000]["config"]["net0"] = TEMPLATE_NET0
        self.lab().create("api")
        self.vmid = self.db.get_project("api")["vmid"]
        self.tmp_id = self.vmid + 1  # first free id in the range, used for the trial restore

    def idx(self, *prefix):
        return next(i for i, c in enumerate(self.fake.calls) if tuple(c[: len(prefix)]) == prefix)

    def assert_original_intact(self, state="running"):
        row = self.db.get_project("api")
        self.assertEqual((row["state"], row["vmid"]), (state, self.vmid))
        self.assertEqual(self.fake.vms[self.vmid]["status"], state)
        self.assertEqual(self.fake.backups, {})  # the new backup was deleted
        self.assertEqual(self.db.list_archives("api"), [])
        self.assertEqual(self.vm_names(), {"jellyfin", "lab-tpl-ubuntu-24.04", "lab-api"})  # no temp VM left
        self.assertEqual(self.db.open_journal(), [])
        self.assertEqual(self.db.conn.execute("SELECT status FROM journal WHERE op='archive'").fetchone()[0],
                         "rolled_back")


class ArchiveSuccessTest(ArchiveBase):
    def test_archive_frees_the_fast_disk_and_records_a_verified_archive(self):
        res = self.lab().archive("api")
        self.assertEqual((res["state"], res["vmid"]), ("archived", None))
        self.assertEqual(self.vm_names(), {"jellyfin", "lab-tpl-ubuntu-24.04"})
        row = self.db.get_project("api")
        self.assertEqual((row["state"], row["vmid"], row["tailnet_ip"], row["storage_id"]),
                         ("archived", None, None, "bigdisk"))
        (arc,) = self.db.list_archives("api")
        self.assertTrue(arc["verified_at"])
        self.assertTrue(arc["sha256"] and arc["size_bytes"] == 1 << 30)
        self.assertIn(arc["volid"], self.fake.backups)
        self.assertTrue(arc["volid"].startswith("bigdisk:backup/"))
        self.assertNotIn("/var/lib/vz/snippets/lab-api.yaml", self.io.files)
        self.assertEqual(self.db.open_journal(), [])

    def test_order_of_operations(self):
        self.lab().archive("api")
        order = [self.idx("qm", "guest", "exec", str(self.vmid)),  # tailscale logout first
                 self.idx("qm", "shutdown", str(self.vmid)),
                 self.idx("vzdump"), self.idx("zstd", "-t"),
                 self.idx("qmrestore"), self.idx("qm", "start", str(self.tmp_id)),
                 self.idx("qm", "destroy", str(self.tmp_id)),
                 self.idx("qm", "destroy", str(self.vmid))]  # the original goes last
        self.assertEqual(order, sorted(order))
        self.assertIn("tailscale logout", self.fake.guest_log)

    def test_trial_restore_is_disconnected_and_boot_tested(self):
        seen = {}
        self.fake.hooks.append((lambda a: a[:3] == ["qm", "start", str(self.tmp_id)],
                                lambda a: seen.update(self.fake.vms[self.tmp_id])))
        self.lab().archive("api")
        self.assertIn("link_down=1", seen["config"]["net0"])
        self.assertEqual(seen["name"], "lab-verify-api")
        self.assertEqual(seen["tags"], "lab;lab-verify")
        check = [c for c in self.fake.guest_log if "findmnt" in c][0]
        self.assertIn("id max", check)
        self.assertIn("lab-api", check)
        self.assertNotIn(self.tmp_id, self.fake.vms)

    def test_stopped_project_archives_without_touching_tailscale(self):
        self.lab().stop("api")
        self.fake.guest_log.clear()
        self.lab().archive("api")
        self.assertNotIn("tailscale logout", self.fake.guest_log)
        self.assertEqual(self.db.get_project("api")["state"], "archived")

    def test_no_boot_test_flag(self):
        self.lab().archive("api", boot_test=False)
        self.assertFalse(any("findmnt" in c for c in self.fake.guest_log))
        self.assertFalse(self.fake.called("qm", "start", str(self.tmp_id)))

    def test_keep_active_verifies_but_keeps_the_vm(self):
        res = self.lab().archive("api", keep_active=True)
        self.assertEqual((res["state"], res["vmid"]), ("running", self.vmid))
        self.assertEqual(len(self.db.list_archives("api")), 1)
        self.assertEqual(self.db.get_project("api")["tailnet_ip"], "100.64.0.5")  # re-enrolled
        self.assertEqual(self.db.open_journal(), [])

    def test_list_shows_archived(self):
        self.lab().archive("api")
        (row,) = self.lab().list()
        self.assertEqual((row["name"], row["state"], row["vmid"]), ("api", "archived", None))


class ArchiveFailureTest(ArchiveBase):
    def run_failing(self, pattern=None, exc=VerificationError):
        with self.assertRaisesRegex(exc, pattern or ""):
            self.lab().archive("api")

    def test_vzdump_failure(self):
        self.fake.fail("vzdump", stderr="no space left on device")
        self.run_failing("no space left")
        self.assert_original_intact()
        self.assertIn("100.64.0.5", self.db.get_project("api")["tailnet_ip"])  # back on the tailnet
        self.assertEqual(len(self.io.posts) // 2, 2)  # fresh key minted for the re-enrolment

    def test_corrupt_archive_is_deleted_and_the_original_restarted(self):
        orig_vzdump = self.fake._vzdump

        def corrupt(a):
            res = orig_vzdump(a)
            for b in self.fake.backups.values():
                b["corrupt"] = True
            return res

        self.fake._vzdump = corrupt
        self.run_failing("corrupted")
        self.assert_original_intact()

    def test_trial_restore_failure(self):
        self.fake.fail("qmrestore", stderr="restore exploded")
        self.run_failing("restore exploded")
        self.assert_original_intact()

    def test_boot_test_failure(self):
        self.fake.guest_results.insert(0, (lambda c: "findmnt" in c, {"exitcode": 1, "err-data": "no such user"}))
        self.run_failing("failed its checks")
        self.assert_original_intact()

    def test_restored_copy_never_gets_an_agent(self):
        self.fake.fail(lambda a: a[:3] == ["qm", "agent", str(self.tmp_id)], stderr="agent not running")
        self.run_failing("guest agent did not come up")
        self.assert_original_intact()

    def test_implausibly_small_backup(self):
        self.fake.backup_size = 1024
        self.run_failing("implausibly small")
        self.assert_original_intact()

    def test_backup_missing_from_listing(self):
        orig = self.fake._vzdump
        self.fake._vzdump = lambda a: (orig(a), self.fake.backups.clear())[0]
        self.run_failing("exactly one new backup")
        self.assert_original_intact()

    def test_message_is_loud_and_says_what_happened(self):
        self.fake.fail("qmrestore")
        with self.assertRaises(VerificationError) as cm:
            self.lab().archive("api")
        msg = str(cm.exception)
        self.assertIn("FAILED", msg)
        self.assertIn("backup was deleted", msg)
        self.assertIn("untouched", msg)
        self.assertEqual(cm.exception.exit_code, 4)

    def test_ctrl_c_during_verify_rolls_back_and_propagates(self):
        def boom(a):
            raise KeyboardInterrupt

        self.fake.hooks.append((lambda a: a[0] == "qmrestore", boom))
        with self.assertRaises(KeyboardInterrupt):
            self.lab().archive("api")
        self.assert_original_intact()

    def test_vm_that_will_not_shut_down_is_left_alone(self):
        self.fake.ignore_shutdown = True
        with self.assertRaisesRegex(PreconditionError, "not forcing"):
            self.lab().archive("api")
        self.assertFalse(self.fake.called("vzdump"))
        row = self.db.get_project("api")
        self.assertEqual(row["state"], "running")
        self.assertEqual(self.fake.vms[self.vmid]["status"], "running")
        self.assertEqual(self.db.conn.execute("SELECT status FROM journal WHERE op='archive'").fetchone()[0],
                         "rolled_back")

    def test_original_cannot_come_back_up(self):
        self.fake.fail("vzdump")
        self.fake.fail(lambda a: a[:3] == ["qm", "start", str(self.vmid)])
        with self.assertRaisesRegex(VerificationError, "lab start api"):
            self.lab().archive("api")
        self.assertEqual(self.db.get_project("api")["state"], "stopped")  # honest state, VM still exists
        self.assertIn(self.vmid, self.fake.vms)

    def test_never_destroys_the_original_before_commit(self):
        for patch in (lambda: self.fake.fail("qmrestore"), lambda: self.fake.fail(("zstd",))):
            patch()
        with self.assertRaises(VerificationError):
            self.lab().archive("api")
        self.assertFalse(self.fake.called("qm", "destroy", str(self.vmid)))


class ArchivePreconditionTest(ArchiveBase):
    def test_not_enough_space_changes_nothing(self):
        self.fake.storages["bigdisk"]["used"] = self.fake.storages["bigdisk"]["total"] - (10 << 30)
        with self.assertRaisesRegex(PreconditionError, "archive disk has"):
            self.lab().archive("api")
        self.assertEqual(self.fake.vms[self.vmid]["status"], "running")
        self.assertEqual(self.db.get_project("api")["state"], "running")
        self.assertEqual(self.db.conn.execute("SELECT count(*) FROM journal WHERE op='archive'").fetchone()[0], 0)

    def test_protected_archived_busy_and_other_users(self):
        self.lab().set_protected("api", True)
        with self.assertRaisesRegex(PreconditionError, "protected"):
            self.lab().archive("api")
        self.lab().set_protected("api", False)
        with self.assertRaisesRegex(PreconditionError, "no such project"):
            self.lab(self.bo).archive("api")
        self.db.update_project("api", state="archiving")
        with self.assertRaisesRegex(PreconditionError, "lab recover"):
            self.lab().archive("api")
        self.db.update_project("api", state="running")
        self.lab().archive("api")
        with self.assertRaisesRegex(PreconditionError, "already archived"):
            self.lab().archive("api")

    def test_vm_identity_is_checked(self):
        self.fake.vms[self.vmid]["name"] = "not-ours"
        with self.assertRaisesRegex(PreconditionError, "does not match"):
            self.lab().archive("api")
        self.assertFalse(self.fake.called("vzdump"))


class RestoreTest(ArchiveBase):
    def setUp(self):
        super().setUp()
        self.lab().archive("api")

    def test_restore_brings_it_back_with_a_fresh_tailscale_identity(self):
        tailscale_ok(self.fake, ip="100.64.0.77")
        self.fake.guest_log.clear()
        res = self.lab().restore("api")
        self.assertEqual((res["state"], res["tailnet_ip"]), ("running", "100.64.0.77"))
        row = self.db.get_project("api")
        vm = self.fake.vms[row["vmid"]]
        self.assertEqual((vm["name"], vm["tags"], vm["status"]), ("lab-api", "lab;owner-max", "running"))
        self.assertEqual(row["storage_id"], "nvme")
        self.assertIn("name: max", self.io.files["/var/lib/vz/snippets/lab-api.yaml"].decode())
        reset = [i for i, c in enumerate(self.fake.guest_log) if "tailscaled.state" in c]
        up = [i for i, c in enumerate(self.fake.guest_log) if c.startswith("tailscale up")]
        self.assertTrue(reset and up and reset[0] < up[0])  # old identity dropped before enrolling
        self.assertEqual(len(self.db.list_archives("api")), 1)  # the archive is kept
        self.assertEqual(self.db.open_journal(), [])

    def test_restore_without_start(self):
        res = self.lab().restore("api", start=False)
        self.assertEqual(res["state"], "stopped")
        self.assertEqual(self.fake.vms[res["vmid"]]["status"], "stopped")

    def test_restore_failure_rolls_back_and_keeps_the_archive(self):
        self.fake.fail("qmrestore", stderr="no space on fast disk")
        with self.assertRaises(LabError):
            self.lab().restore("api")
        row = self.db.get_project("api")
        self.assertEqual((row["state"], row["vmid"]), ("archived", None))
        self.assertEqual(self.vm_names(), {"jellyfin", "lab-tpl-ubuntu-24.04"})
        self.assertEqual(len(self.db.list_archives("api")), 1)
        self.assertEqual(len(self.fake.backups), 1)
        self.fake._failures.clear()
        self.lab().restore("api")  # and a retry works

    def test_restore_ctrl_c_rolls_back(self):
        def boom(a):
            raise KeyboardInterrupt

        self.fake.hooks.append((lambda a: a[:2] == ["qm", "set"], boom))
        with self.assertRaises(KeyboardInterrupt):
            self.lab().restore("api")
        self.assertEqual(self.db.get_project("api")["state"], "archived")
        self.assertEqual(self.vm_names(), {"jellyfin", "lab-tpl-ubuntu-24.04"})

    def test_start_failure_keeps_the_restored_vm(self):
        self.fake.guest_results.insert(0, (lambda c: c.startswith("tailscale up"), {"exitcode": 1, "err-data": "x"}))
        with self.assertRaisesRegex(LabError, "restored but could not be started"):
            self.lab().restore("api")
        row = self.db.get_project("api")
        self.assertIn(row["vmid"], self.fake.vms)
        self.assertEqual((row["state"], self.fake.vms[row["vmid"]]["status"]), ("running", "running"))
        self.assertIsNone(row["tailnet_ip"])
        self.fake.guest_results.pop(0)
        self.assertEqual(self.lab().start("api")["tailnet_ip"], "100.64.0.5")  # `lab start` repairs enrolment

    def test_missing_archive_volume(self):
        self.fake.backups.clear()
        with self.assertRaisesRegex(PreconditionError, "missing from the archive disk"):
            self.lab().restore("api")
        self.assertEqual(self.db.get_project("api")["state"], "archived")

    def test_not_archived_and_no_permission(self):
        with self.assertRaisesRegex(PreconditionError, "no such project"):
            self.lab(self.bo).restore("api")
        self.lab().restore("api")
        with self.assertRaisesRegex(PreconditionError, "not archived"):
            self.lab().restore("api")

    def test_restore_respects_quotas(self):
        self.db.add_user("cy@x.io", "cy", "user", {"max_running_vms": 1})
        from lab.access import Caller
        cy = Caller("cy@x.io", "cy", "user")
        self.lab(cy).create("one")
        self.lab(cy).create("two") if False else None
        self.lab(cy).archive("one")
        self.lab(cy).create("two")
        with self.assertRaisesRegex(PreconditionError, "quota exceeded"):
            self.lab(cy).restore("one")
        self.assertEqual(self.db.get_project("one")["state"], "archived")
        self.lab(cy).restore("one", start=False)  # a stopped restore does not need a VM slot


class RetentionTest(ArchiveBase):
    def test_second_archive_replaces_the_first_only_after_it_verifies(self):
        self.lab().archive("api")
        (first,) = self.db.list_archives("api")
        self.lab().restore("api")
        self.fake.fail(("zstd",), stderr="zstd: corrupted")
        with self.assertRaises(VerificationError):
            self.lab().archive("api")
        self.assertEqual([a["volid"] for a in self.db.list_archives("api")], [first["volid"]])
        self.assertEqual(set(self.fake.backups), {first["volid"]})  # old archive survived the failed attempt
        self.fake._failures.clear()
        self.lab().archive("api")
        (second,) = self.db.list_archives("api")
        self.assertNotEqual(second["volid"], first["volid"])
        self.assertEqual(set(self.fake.backups), {second["volid"]})

    def test_keep_two(self):
        self.cfg.archive.keep = 2
        for _ in range(3):
            self.lab().archive("api")
            self.lab().restore("api")
        self.assertEqual(len(self.db.list_archives("api")), 2)
        self.assertEqual(len(self.fake.backups), 2)


class RecoveryTest(ArchiveBase):
    def crash_without_cleanup(self):
        """Make the archive die the way a power cut would: no rollback code runs."""
        lab = self.lab()
        lab._abort_archive = lambda row, st, jid, error, backup_stage: error
        return lab

    def test_crash_before_commit_rolls_back(self):
        lab = self.crash_without_cleanup()
        self.fake.fail("qmrestore", stderr="power cut")
        with self.assertRaises(LabError):
            lab.archive("api")
        # state a crash would leave behind
        self.assertEqual(self.db.get_project("api")["state"], "archiving")
        self.assertEqual(len(self.fake.backups), 1)
        self.assertEqual(self.fake.vms[self.vmid]["status"], "stopped")
        (j,) = self.db.open_journal()
        self.assertEqual((j["op"], j["step"]), ("archive", "verify"))
        with self.assertRaisesRegex(PreconditionError, "lab recover"):
            self.lab().archive("api")
        self.fake._failures.clear()
        msg = self.lab().recover("api")
        self.assertIn("rolled back", msg)
        self.assertEqual(self.fake.vms[self.vmid]["status"], "running")
        self.assertEqual(self.fake.backups, {})
        self.assertEqual(self.db.get_project("api")["state"], "running")
        self.assertEqual(self.db.open_journal(), [])
        self.lab().archive("api")  # and archiving works again

    def test_crash_with_leftover_trial_vm_is_cleaned(self):
        lab = self.crash_without_cleanup()
        self.fake.fail(lambda a: a[:3] == ["qm", "start", str(self.tmp_id)], stderr="power cut")
        with self.assertRaises(LabError):
            lab.archive("api")
        self.assertIn(self.tmp_id, self.fake.vms)  # left behind by the "crash"
        self.lab().recover("api") if not self.fake._failures.clear() else None
        self.assertNotIn(self.tmp_id, self.fake.vms)
        self.assertEqual(self.fake.backups, {})

    def test_crash_after_commit_rolls_forward(self):
        def crash(a):
            raise SystemExit("power cut")

        self.fake.hooks.append((lambda a: a[:3] == ["qm", "destroy", str(self.vmid)], crash))
        with self.assertRaises(SystemExit):
            self.lab().archive("api")
        self.fake.hooks.clear()
        (j,) = self.db.open_journal()
        self.assertEqual(j["step"], "commit")
        self.assertEqual(len(self.db.list_archives("api")), 1)  # already recorded
        self.assertIn(self.vmid, self.fake.vms)
        msg = self.lab().recover("api")
        self.assertIn("completed", msg)
        row = self.db.get_project("api")
        self.assertEqual((row["state"], row["vmid"]), ("archived", None))
        self.assertNotIn(self.vmid, self.fake.vms)
        self.assertEqual(len(self.fake.backups), 1)
        self.assertEqual(self.db.open_journal(), [])
        self.lab().restore("api")  # the rolled-forward archive is usable

    def test_roll_forward_refuses_if_the_backup_vanished(self):
        def crash(a):
            raise SystemExit("power cut")

        self.fake.hooks.append((lambda a: a[:3] == ["qm", "destroy", str(self.vmid)], crash))
        with self.assertRaises(SystemExit):
            self.lab().archive("api")
        self.fake.hooks.clear()
        self.fake.backups.clear()  # e.g. the archive disk was unmounted
        with self.assertRaisesRegex(PreconditionError, "refusing to destroy"):
            self.lab().recover("api")
        self.assertIn(self.vmid, self.fake.vms)

    def test_recover_nothing(self):
        with self.assertRaisesRegex(PreconditionError, "nothing to recover"):
            self.lab().recover("api")

    def test_interrupted_restore_is_rolled_back(self):
        self.lab().archive("api")
        self.db.update_project("api", state="restoring", vmid=1000)
        self.fake.add_vm(1000, "lab-api", tags="lab")
        self.db.journal_begin("api", "restore")
        self.assertIn("restore rolled back", self.lab().recover("api"))
        self.assertEqual(self.db.get_project("api")["state"], "archived")
        self.assertNotIn(1000, self.fake.vms)
        self.assertEqual(len(self.fake.backups), 1)

    def test_only_the_owner_can_recover(self):
        self.db.journal_begin("api", "delete")
        with self.assertRaisesRegex(PreconditionError, "no such project"):
            self.lab(self.bo).recover("api")


class DeleteWithArchivesTest(ArchiveBase):
    def test_live_project_with_archive_needs_the_flag(self):
        self.lab().archive("api", keep_active=True)
        with self.assertRaisesRegex(PreconditionError, "--archives"):
            self.lab().delete("api", "api")
        self.assertIn(self.vmid, self.fake.vms)
        self.lab().delete("api", "api", archives=True)
        self.assertIsNone(self.db.get_project("api"))
        self.assertEqual((self.db.list_archives("api"), self.fake.backups), ([], {}))
        self.assertNotIn(self.vmid, self.fake.vms)

    def test_archived_project_delete(self):
        self.lab().archive("api")
        with self.assertRaisesRegex(PreconditionError, "--archives"):
            self.lab().delete("api", "api")
        self.lab().delete("api", "api", archives=True)
        self.assertIsNone(self.db.get_project("api"))
        self.assertEqual(self.fake.backups, {})

    def test_archive_delete_keeps_the_record_if_the_volume_survives(self):
        self.lab().archive("api")
        self.fake.fail(("pvesm", "free"), stderr="storage offline")
        with self.assertRaises(LabError):
            self.lab().delete("api", "api", archives=True)
        self.assertEqual(len(self.db.list_archives("api")), 1)  # not forgotten while still on disk
        self.assertIsNotNone(self.db.get_project("api"))

    def test_secrets_never_reach_archive_commands(self):
        self.lab().archive("api")
        self.lab().restore("api")
        everything = json.dumps(self.fake.calls) + "".join(self.fake.guest_log)
        self.assertNotIn(KEY, everything)


if __name__ == "__main__":
    unittest.main()
