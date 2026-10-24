import argparse
import contextlib
import io
import json
import tempfile
import types
import unittest
from pathlib import Path

from lab import client, watch
from lab.access import Caller
from lab.cli import cmd_approvals
from lab.client import ClientError
from lab.errors import PreconditionError
from lab.progress import Reporter

from .test_m4 import ServerBase, parse


class WaitForClickTest(ServerBase):
    """With the desktop helper running, a gated call is held open until the user clicks."""

    def setUp(self):
        super().setUp()
        self.sleeps = []

    def watcher_on(self, user="max", age=0.0):
        self.db.heartbeat("approver", user, self.clock() - age)

    def server_with(self, on_sleep=None):
        s = self.server()

        def sleeper(sec):
            self.sleeps.append(sec)
            self.t[0] += sec
            if on_sleep:
                on_sleep()
        s.sleep = sleeper
        return s

    def click(self, approve=True):
        def go():
            for r in self.db.approval_list("max"):
                if r["status"] == "pending":
                    self.human_approves(r["id"], approve=approve)
        return go

    def test_approved_while_waiting_runs_in_the_same_call(self):
        self.watcher_on()
        s = self.server_with(self.click(True))
        err, body = self.call(s, "lab_exec", name="api", command="echo hi")  # api was not created by this session
        self.assertFalse(err, body)
        self.assertIn("api", s.attached)
        self.assertEqual(self.sleeps, [1])  # one second, not the whole wait
        end = [r for r in self.audit_records() if r["phase"] == "end"][-1]
        self.assertEqual((end["result"], end["approver"]), ("ok", "max"))

    def test_denied_while_waiting(self):
        self.watcher_on()
        s = self.server_with(self.click(False))
        err, body = self.call(s, "lab_exec", name="api", command="id")
        self.assertTrue(err)
        self.assertIn("denied", body["error"])
        self.assertEqual(self.fake.guest_log, [])
        self.assertNotIn("api", s.attached)

    def test_nobody_clicks_falls_back_to_the_terminal_command_after_the_wait(self):
        self.watcher_on()
        s = self.server_with()
        err, body = self.call(s, "lab_exec", name="api", command="id")
        self.assertTrue(err)
        self.assertIn("lab approve", body["error"])
        self.assertEqual(sum(self.sleeps), self.cfg.mcp.approval_wait_s)
        self.assertEqual(len([a for a in self.db.approval_list() if a["status"] == "pending"]), 1)

    def test_late_approval_still_works_on_retry(self):
        self.watcher_on()
        s = self.server_with()
        aid = self.ids(self.call(s, "lab_exec", name="api", command="id")[1]["error"])
        self.human_approves(aid)
        self.assertFalse(self.call(s, "lab_exec", name="api", command="id")[0])

    def test_no_waiting_without_a_fresh_heartbeat_for_this_user(self):
        for setup in (lambda: None, lambda: self.watcher_on(age=60), lambda: self.watcher_on(user="bo")):
            setup()
            s = self.server_with()
            err, body = self.call(s, "lab_exec", name="api", command="id")
            self.assertTrue(err)
            self.assertEqual(self.sleeps, [], "must not block when no helper is watching")
            self.db.conn.execute("DELETE FROM approvals")

    def test_session_record_shows_what_the_client_supports(self):
        s = self.server()
        s.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {"roots": {}, "elicitation": {}},
            "clientInfo": {"name": "claude-code", "version": "9.9"}}})
        rec = [r for r in self.audit_records() if r["phase"] == "session"][-1]
        self.assertEqual((rec["client"], rec["elicitation"], rec["capabilities"]),
                         ("claude-code 9.9", True, ["elicitation", "roots"]))
        s2 = self.server()
        s2.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"capabilities": {}}})
        self.assertFalse([r for r in self.audit_records() if r["phase"] == "session"][-1]["elicitation"])


class ApprovalsCommandTest(ServerBase):
    def run_cmd(self, caller, **kw):
        opts = dict(cmd="approvals", all=False, pending=False, heartbeat=False, json=True)
        opts.update(kw)
        ctx = types.SimpleNamespace(args=argparse.Namespace(**opts), cfg=self.cfg, caller=lambda: caller,
                                    db=lambda: self.db, clock=self.clock)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cmd_approvals(ctx)
        return json.loads(out.getvalue())

    def test_pending_filter_full_plan_expiry_and_heartbeat(self):
        s = self.server()
        s.attached.add("api")
        self.call(s, "lab_exec", name="api", command="id", root=True)  # pending
        old = self.call(s, "lab_exec", name="api", command="whoami", root=True)[1]["error"]
        self.human_approves(self.ids(old))  # approved: no longer pending
        rows = self.run_cmd(self.max, pending=True, heartbeat=True)
        self.assertEqual(len(rows), 1)
        self.assertGreater(len(rows[0]["plan"]), 20)  # the JSON carries the whole plan, not a table-width cut
        self.assertEqual(rows[0]["expires_in"], 600)
        self.assertIsNotNone(self.db.heartbeat_age("approver", "max", self.clock() + 1))
        self.assertIsNone(self.db.heartbeat_age("approver", "bo", self.clock()))
        self.t[0] += 700
        self.assertEqual(self.run_cmd(self.max, pending=True), [])  # expired ones are not offered

    def test_users_only_see_their_own(self):
        s = self.server()
        s.attached.add("api")
        self.call(s, "lab_exec", name="api", command="id", root=True)
        self.assertEqual(self.run_cmd(self.bo), [])


class WatchLoopTest(unittest.TestCase):
    def item(self, aid="aa11bb", plan="Delete demo"):
        return {"id": aid, "project": "demo", "tool": "lab_delete", "plan": plan, "expires_in": 600, "state": "pending"}

    def run_once(self, batches, answers, decide_fail=False):
        calls, shown, logs = [], [], []
        queue = list(batches)

        def pending():
            if isinstance(queue[0], Exception):
                raise queue.pop(0)
            return queue.pop(0) if len(queue) > 1 else queue[0]

        def show(item, alive):
            shown.append(item["id"])
            return answers.pop(0)

        def decide(aid, ok):
            if decide_fail:
                raise ClientError("approval aa11bb is expired")
            calls.append((aid, ok))
        watch.run({}, show=show, once=True, sleep=lambda s: None, logger=logs.append,
                  pending_fn=pending, decide_fn=decide)
        return calls, shown, logs

    def test_approve_and_deny_are_recorded(self):
        calls, shown, _ = self.run_once([[self.item("a1"), self.item("b2")]], [True, False])
        self.assertEqual(calls, [("a1", True), ("b2", False)])
        self.assertEqual(shown, ["a1", "b2"])

    def test_closed_elsewhere_makes_no_decision(self):
        calls, _, logs = self.run_once([[self.item()]], [None])
        self.assertEqual(calls, [])
        self.assertTrue(any("settled elsewhere" in l for l in logs))

    def test_a_failed_decision_is_logged_not_fatal(self):
        calls, _, logs = self.run_once([[self.item()]], [True], decide_fail=True)
        self.assertTrue(any("could not record" in l for l in logs))

    def test_same_approval_is_never_shown_twice(self):
        shown, polls = [], []
        pending = lambda: [self.item()]
        n = {"i": 0}

        def sleeper(s):
            n["i"] += 1
            if n["i"] > 3:
                raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            watch.run({}, show=lambda item, alive: shown.append(item["id"]) or True, sleep=sleeper,
                      logger=lambda m: None, pending_fn=pending, decide_fn=lambda a, b: None)
        self.assertEqual(shown, ["aa11bb"])

    def test_unreachable_host_backs_off_and_keeps_going(self):
        sleeps = []
        state = {"n": 0}

        def pending():
            state["n"] += 1
            if state["n"] == 1:
                raise ClientError("ssh: connect timed out")
            return []

        def sleeper(s):
            sleeps.append(s)
            if len(sleeps) >= 2:
                raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            watch.run({}, show=None, sleep=sleeper, logger=lambda m: None, pending_fn=pending)
        self.assertEqual(sleeps[0], 10)

    def test_once_reports_an_unreachable_host(self):
        with self.assertRaises(ClientError):
            self.run_once([ClientError("no route")], [])


class StartupTest(unittest.TestCase):
    def test_script_runs_the_watcher_hidden(self):
        script = watch.startup_script("C:\\Python312\\python.exe")
        self.assertIn("-m lab.entry watch", script)
        self.assertIn(", 0, False", script)  # window style 0 = hidden
        self.assertIn('""C:\\Python312\\python.exe"" ', script)  # doubled quotes: VBScript's escape

    def test_install_and_uninstall(self):
        with tempfile.TemporaryDirectory() as d:
            path = watch.install(Path(d) / "Startup")
            self.assertTrue(path.exists())
            self.assertIn("lab.entry watch", path.read_text())
            self.assertTrue(watch.uninstall(Path(d) / "Startup"))
            self.assertFalse(watch.uninstall(Path(d) / "Startup"))


class ClientTerminalTest(unittest.TestCase):
    def test_terminal_only_when_you_are_at_one_and_the_traffic_is_human(self):
        w = client.wants_tty
        self.assertTrue(w(["create", "demo"], True, True))
        self.assertTrue(w(["--dry-run", "archive", "demo"], True, True))
        self.assertFalse(w(["create", "demo"], False, True))  # stdin piped
        self.assertFalse(w(["create", "demo"], True, False))  # output redirected to a file
        self.assertFalse(w(["--json", "list"], True, True))
        self.assertFalse(w(["list", "--json"], True, True))
        for binary in ("put", "get", "mcp", "exec"):
            self.assertFalse(w([binary, "x"], True, True), binary)
        self.assertFalse(w([], True, True))

    def test_tty_flag_and_quiet_logging_reach_ssh(self):
        argv = client.remote_argv({"host": "h", "ssh_user": "root"}, ["create", "demo"], tty=True,
                                  extra=["-o", "LogLevel=ERROR"])
        self.assertEqual(argv[:5], ["ssh", "-t", "-o", "LogLevel=ERROR", "root@h"])


class NoiseTest(unittest.TestCase):
    def test_lvm_chatter_is_dropped_and_warnings_are_shown_once_at_the_end(self):
        out = io.StringIO()
        r = Reporter(out)
        r("Cloning")
        for ln in ('Logical volume "vm-1001-disk-0" created.', "Logical volume vg-drive-c/vm-1001-disk-0 changed.",
                   "WARNING: Sum of all thin volume sizes (<3.97 TiB) exceeds the size of thin pool vg/data",
                   "WARNING: Sum of all thin volume sizes (<4.00 TiB) exceeds the size of thin pool vg/data",
                   "WARNING: You have not turned on protection against thin pools running out of space.",
                   "transferred 3.2 GiB of 32.0 GiB (10.01%)", "create full clone of drive scsi0"):
            r.line(ln)
        self.assertNotIn("Logical volume", out.getvalue())
        self.assertNotIn("WARNING", out.getvalue())  # nothing mid-run
        r.finish()
        text = out.getvalue()
        self.assertEqual(text.count("thin volume sizes"), 1)  # same warning, different numbers: once
        self.assertEqual(text.count("note: "), 2)
        self.assertIn("create full clone of drive scsi0", text)
        r.finish()  # idempotent: notes are not printed again
        self.assertEqual(out.getvalue().count("note: "), 2)


if __name__ == "__main__":
    unittest.main()
