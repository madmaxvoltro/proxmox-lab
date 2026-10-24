import base64
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from lab import audit, client, entry
from lab.access import Caller
from lab.approvals import Approvals, fingerprint
from lab.audit import AuditLog, redact
from lab.cli import build_parser, parse_args
from lab.errors import PreconditionError, UsageError
from lab.mcp import McpServer

from .test_m2 import Base


def parse(res):
    return json.loads(res["content"][0]["text"])


class ServerBase(Base):
    def setUp(self):
        super().setUp()
        self.t = [1000.0]
        self.clock = lambda: self.t[0]
        self.audit_path = os.path.join(self.tmp.name, "audit.log")
        self.lab().create("api")  # created outside any MCP session
        self.fake.guest_log.clear()

    def server(self, caller=None, elicitation=False):
        caller = caller or self.max
        s = McpServer(self.lab(caller), self.cfg, self.db, AuditLog(self.audit_path, self.clock),
                      io.StringIO(), io.StringIO(), session="s1", clock=self.clock)
        s.elicitation = elicitation
        return s

    def call(self, s, tool, **args):
        res = s.call(tool, args)
        return res["isError"], parse(res)

    def human_approves(self, approval_id, approve=True, who=None):
        who = who or self.max
        Approvals(self.db, 600, who.name, self.clock).decide(approval_id, approve, who.name, who.is_admin)

    def answers(self, s, *responses):
        s.inp = io.StringIO("".join(json.dumps({"jsonrpc": "2.0", "id": f"lab-{i}", "result": r}) + "\n"
                                    for i, r in enumerate(responses, start=s._rid + 1)))

    def ids(self, text):
        return text.split("(")[1].split(")")[0]

    def audit_records(self):
        return [json.loads(l) for l in Path(self.audit_path).read_text().splitlines()]


class GuestOpsTest(ServerBase):
    def test_exec_runs_as_the_owner_not_root(self):
        self.fake.guest_results.insert(0, (lambda c: "echo hi" in c, {"exitcode": 0, "out-data": "hi\n"}))
        res = self.lab().exec("api", "echo hi")
        self.assertEqual((res["exit_code"], res["stdout"], res["timed_out"]), (0, "hi\n", False))
        self.assertIn("runuser -l -s /bin/bash -c echo hi max", self.fake.guest_log[-1])
        self.lab().exec("api", "id", root=True)
        self.assertTrue(self.fake.guest_log[-1].startswith("bash -lc id"))
        self.lab().exec("api", "make", cwd="/srv/my app")
        self.assertIn("cd -- '/srv/my app' && make", self.fake.guest_log[-1])

    def test_nonzero_exit_and_stderr(self):
        self.fake.guest_results.insert(0, (lambda c: "false" in c, {"exitcode": 3, "err-data": "bad"}))
        res = self.lab().exec("api", "false")
        self.assertEqual((res["exit_code"], res["stderr"]), (3, "bad"))

    def test_must_be_running_and_visible(self):
        with self.assertRaisesRegex(PreconditionError, "no such project"):
            self.lab(self.bo).exec("api", "id")
        self.lab().stop("api")
        with self.assertRaisesRegex(PreconditionError, "stopped"):
            self.lab().exec("api", "id")

    def test_validation(self):
        for kw in ({"cmd": " "}, {"cmd": "ls", "timeout": 0}, {"cmd": "ls", "timeout": 99999}):
            with self.assertRaises(UsageError):
                self.lab().exec("api", kw["cmd"], timeout=kw.get("timeout", 60))

    def test_output_is_capped(self):
        self.cfg.mcp.max_output_bytes = 100
        self.fake.guest_results.insert(0, (lambda c: "big" in c, {"exitcode": 0, "out-data": "x" * 5000}))
        res = self.lab().exec("api", "big")
        self.assertTrue(res["truncated"])
        self.assertLess(len(res["stdout"]), 200)

    def test_background_job_lifecycle(self):
        job = self.lab().exec_background("api", "sleep 100")["job"]
        self.assertRegex(job, r"^[0-9a-f]{12}$")
        start = [c for c in self.fake.guest_log if "lab-job" in c][-1]
        self.assertIn(f"lab-job {job} user max sleep 100", start)
        self.fake.guest_results.insert(0, (lambda c: "lab-poll" in c, {"exitcode": 0, "out-data": "RUNNING\n---OUT\npart\n\n---ERR\n"}))
        self.assertEqual(self.lab().exec_poll("api", job)["running"], True)
        self.fake.guest_results.insert(0, (lambda c: "lab-poll" in c, {"exitcode": 0, "out-data": "RC 2\n---OUT\ndone\n\n---ERR\noops"}))
        res = self.lab().exec_poll("api", job)
        self.assertEqual((res["running"], res["exit_code"], res["stdout"], res["stderr"]), (False, 2, "done", "oops"))
        self.fake.guest_results.insert(0, (lambda c: "lab-cancel" in c, {"exitcode": 0, "out-data": "CANCELLED\n"}))
        self.assertEqual(self.lab().exec_cancel("api", job)["result"], "cancelled")

    def test_job_ids_are_validated_and_unknown_jobs_reported(self):
        for bad in ("../../etc/passwd", "ABC", "", "x" * 12):
            with self.assertRaises(UsageError):
                self.lab().exec_poll("api", bad)
        self.fake.guest_results.insert(0, (lambda c: "lab-poll" in c, {"exitcode": 3, "out-data": "NOJOB\n"}))
        with self.assertRaisesRegex(PreconditionError, "no such job"):
            self.lab().exec_poll("api", "0" * 12)

    def test_read_file_text_binary_and_truncated(self):
        body = base64.b64encode(b"hello").decode()
        self.fake.guest_results.insert(0, (lambda c: "head -c" in c, {"exitcode": 0, "out-data": f"5\n{body}"}))
        res = self.lab().read_file("api", "/etc/motd")
        self.assertEqual((res["encoding"], res["content"], res["truncated"]), ("utf-8", "hello", False))
        self.assertIn("p=/etc/motd;", self.fake.guest_log[-1])
        self.lab().read_file("api", "/tmp/a b; id")
        self.assertIn("p='/tmp/a b; id';", self.fake.guest_log[-1])
        raw = base64.b64encode(b"\xff\xfe").decode()
        self.fake.guest_results.insert(0, (lambda c: "head -c" in c, {"exitcode": 0, "out-data": f"9\n{raw}"}))
        res = self.lab().read_file("api", "/bin/x")
        self.assertEqual((res["encoding"], res["truncated"]), ("base64", True))

    def test_read_missing_file(self):
        self.fake.guest_results.insert(0, (lambda c: "head -c" in c, {"exitcode": 2, "err-data": "not a regular file: /nope"}))
        with self.assertRaisesRegex(PreconditionError, "not a regular file"):
            self.lab().read_file("api", "/nope")

    def test_write_file_sends_content_on_stdin_and_quotes_the_path(self):
        before = len(self.fake.stdin_seen)
        res = self.lab().write_file("api", "/tmp/a b; rm -rf ~/x.txt", "héllo")
        self.assertEqual(res["bytes"], len("héllo".encode()))
        self.assertEqual(base64.b64decode(self.fake.stdin_seen[before]).decode(), "héllo")
        cmd = self.fake.guest_log[-1]
        self.assertIn("'/tmp/a b; rm -rf ~/x.txt'", cmd)  # one quoted word, never interpreted
        self.assertNotIn("héllo", cmd)  # content never in argv

    def test_write_limits_and_validation(self):
        self.cfg.mcp.max_file_bytes = 10
        with self.assertRaisesRegex(UsageError, "too large"):
            self.lab().write_file("api", "/tmp/x", "y" * 11)
        with self.assertRaisesRegex(UsageError, "base64"):
            self.lab().write_file("api", "/tmp/x", "***", base64_encoded=True)
        with self.assertRaises(UsageError):
            self.lab().write_file("api", "", "x")
        with self.assertRaises(UsageError):
            self.lab().read_file("api", "a\x00b")

    def test_logs(self):
        ops = self.lab().logs("api", "ops")["ops"]
        self.assertEqual([o["op"] for o in ops], ["create"])
        self.fake.guest_results.insert(0, (lambda c: "cloud-init-output" in c, {"exitcode": 0, "out-data": "boot ok"}))
        self.assertEqual(self.lab().logs("api", "setup")["setup_log"], "boot ok")
        with self.assertRaises(UsageError):
            self.lab().logs("api", "host-journal")


class ProtocolTest(ServerBase):
    def roundtrip(self, *msgs, elicitation=False):
        s = self.server()
        s.inp = io.StringIO("".join(json.dumps(m) + "\n" for m in msgs))
        s.serve()
        return s, [json.loads(l) for l in s.out.getvalue().splitlines()]

    def test_initialize_list_ping_and_errors(self):
        s, out = self.roundtrip(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-06-18", "capabilities": {"elicitation": {}}, "clientInfo": {"name": "t"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "ping"},
            {"jsonrpc": "2.0", "id": 4, "method": "bogus/method"})
        self.assertTrue(s.elicitation)
        self.assertEqual(out[0]["result"]["protocolVersion"], "2025-06-18")
        self.assertIn("never try to work around", out[0]["result"]["instructions"])
        tools = {t["name"]: t for t in out[1]["result"]["tools"]}
        self.assertEqual(out[2], {"jsonrpc": "2.0", "id": 3, "result": {}})
        self.assertEqual(out[3]["error"]["code"], -32601)
        self.assertEqual(len(out), 4)  # the notification got no reply
        self.assertTrue(all(t["inputSchema"]["additionalProperties"] is False for t in tools.values()))
        # no host access of any kind, and no way to grant an approval
        self.assertFalse([n for n in tools if any(w in n for w in ("approve", "host", "qm", "shell", "config", "unprotect", "protect"))])
        self.assertEqual(sorted(tools), sorted(set(tools)))
        for want in ("lab_list", "lab_create", "lab_exec", "lab_delete", "lab_archive", "lab_approvals", "lab_attach"):
            self.assertIn(want, tools)

    def test_garbage_input_does_not_kill_the_session(self):
        s = self.server()
        s.inp = io.StringIO('not json\n{"jsonrpc":"2.0","id":9,"method":"ping"}\n')
        s.serve()
        out = [json.loads(l) for l in s.out.getvalue().splitlines()]
        self.assertEqual(out[0]["error"]["code"], -32700)
        self.assertEqual(out[1]["result"], {})

    def test_tools_call_over_the_wire_and_only_json_on_stdout(self):
        s, out = self.roundtrip({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                 "params": {"name": "lab_list", "arguments": {}}})
        body = json.loads(out[0]["result"]["content"][0]["text"])
        self.assertEqual([p["name"] for p in body["projects"]], ["api"])
        self.assertFalse(out[0]["result"]["isError"])

    def test_bad_arguments_are_tool_errors(self):
        s = self.server()
        for tool, args in (("nope", {}), ("lab_status", {}), ("lab_status", {"name": 5}),
                           ("lab_status", {"name": "api", "extra": 1}), ("lab_exec", {"name": "api", "command": "x", "timeout": True})):
            err, body = self.call(s, tool, **args)
            self.assertTrue(err, (tool, args))
            self.assertIn("error", body)


class VisibilityTest(ServerBase):
    def test_list_status_and_protected(self):
        s = self.server()
        self.assertEqual([p["name"] for p in self.call(s, "lab_list")[1]["projects"]], ["api"])
        self.assertEqual(self.call(s, "lab_status", name="api")[1]["state"], "running")
        self.lab().set_protected("api", True)
        self.assertEqual(self.call(s, "lab_list")[1]["projects"], [{"name": "api", "protected": True}])
        self.assertEqual(self.call(s, "lab_status", name="api")[1], {"name": "api", "protected": True})

    def test_other_users_projects_do_not_exist(self):
        s = self.server(self.bo)
        self.assertEqual(self.call(s, "lab_list")[1]["projects"], [])
        calls = [("lab_status", {}), ("lab_start", {}), ("lab_stop", {}), ("lab_attach", {}), ("lab_logs", {}),
                 ("lab_exec", {"command": "id"}), ("lab_read_file", {"path": "/etc/passwd"}),
                 ("lab_write_file", {"path": "/tmp/x", "content": "x"}), ("lab_download", {"path": "/etc/passwd"}),
                 ("lab_delete", {}), ("lab_archive", {}), ("lab_restore", {})]
        for tool, extra in calls:
            err, body = self.call(s, tool, name="api", **extra)
            self.assertTrue(err)
            self.assertIn("no such project", body["error"], tool)  # before any approval is even asked
        self.assertEqual(self.db.approval_list(), [])


class CreateTest(ServerBase):
    def test_create_attaches_and_returns_ssh(self):
        s = self.server()
        err, body = self.call(s, "lab_create", name="web", ram="4G")
        self.assertFalse(err, body)
        self.assertEqual((body["owner"], body["ssh"]), ("max", "ssh max@lab-api.tail1234.ts.net"))
        self.assertIn("web", s.attached)
        err, body = self.call(s, "lab_exec", name="web", command="id")
        self.assertFalse(err, body)  # no approval: the session created it

    def test_ai_cannot_choose_owner_disks_or_bridge(self):
        s = self.server()
        for bad in ({"owner": "bo"}, {"storage": "bigdisk"}, {"fast_disk": "x"}, {"bridge": "vmbr9"}, {"archive_disk": "x"}):
            err, body = self.call(s, "lab_create", name="x1", **bad)
            self.assertTrue(err, bad)
            self.assertIn("unexpected argument", body["error"])
        self.assertIsNone(self.db.get_project("x1"))

    def test_creates_above_the_caps_need_approval(self):
        s = self.server()
        err, body = self.call(s, "lab_create", name="big", cpu=12)
        self.assertTrue(err)
        self.assertIn("APPROVAL REQUIRED", body["error"])
        self.assertIn("12 vCPUs (AI limit 8)", body["error"])
        self.assertIsNone(self.db.get_project("big"))
        aid = self.ids(body["error"])
        self.human_approves(aid)
        err, body = self.call(s, "lab_create", name="big", cpu=12)
        self.assertFalse(err, body)
        self.assertEqual(self.db.get_project("big")["cpu"], 12)

    def test_vm_count_cap(self):
        self.cfg.mcp.max_running_vms = 1
        err, body = self.call(self.server(), "lab_create", name="two")
        self.assertIn("running VMs", body["error"])


class ApprovalTest(ServerBase):
    def test_entering_an_unattached_project_needs_approval_once_per_session(self):
        s = self.server()
        err, body = self.call(s, "lab_exec", name="api", command="ls")
        self.assertTrue(err)
        self.assertIn("lab approve", body["error"])
        self.assertEqual(self.fake.guest_log, [])  # nothing ran
        aid = self.ids(body["error"])
        # asking again reuses the same pending approval instead of piling up new ones
        self.assertEqual(self.ids(self.call(s, "lab_exec", name="api", command="ls")[1]["error"]), aid)
        self.assertEqual(len(self.db.approval_list()), 1)
        self.human_approves(aid)
        self.assertFalse(self.call(s, "lab_exec", name="api", command="ls")[0])
        self.assertIn("api", s.attached)
        self.assertEqual(len(self.db.approval_list()), 1)  # attached: no further approvals
        # a different session has to ask again
        s2 = self.server()
        s2.session = "s2"
        self.assertTrue(self.call(s2, "lab_exec", name="api", command="ls")[0])

    def test_attach_tool_is_gated_too(self):
        s = self.server()
        err, body = self.call(s, "lab_attach", name="api")
        self.assertIn("APPROVAL REQUIRED", body["error"])
        self.human_approves(self.ids(body["error"]))
        self.assertEqual(self.call(s, "lab_attach", name="api")[1], {"attached": "api"})

    def test_approvals_are_single_use_and_bound_to_exact_arguments(self):
        s = self.server()
        s.attached.add("api")
        err, body = self.call(s, "lab_exec", name="api", command="ls", root=True)
        aid = self.ids(body["error"])
        self.human_approves(aid)
        err, body = self.call(s, "lab_exec", name="api", command="rm -rf /", root=True)  # different command
        self.assertIn("APPROVAL REQUIRED", body["error"])
        self.assertNotEqual(self.ids(body["error"]), aid)
        self.assertFalse(self.call(s, "lab_exec", name="api", command="ls", root=True)[0])  # the approved one
        self.assertTrue(self.call(s, "lab_exec", name="api", command="ls", root=True)[0])  # used up
        self.assertEqual(self.db.approval_get(aid)["status"], "used")

    def test_approval_expires(self):
        s = self.server()
        s.attached.add("api")
        aid = self.ids(self.call(s, "lab_exec", name="api", command="id", root=True)[1]["error"])
        self.human_approves(aid)
        self.t[0] += 601
        err, body = self.call(s, "lab_exec", name="api", command="id", root=True)
        self.assertTrue(err)
        self.assertNotEqual(self.ids(body["error"]), aid)
        with self.assertRaisesRegex(PreconditionError, "can no longer"):
            self.human_approves(aid)

    def test_only_the_owner_or_an_admin_can_approve_and_deny_works(self):
        s = self.server(self.bo)
        self.lab(self.bo).create("mine")
        err, body = self.call(s, "lab_exec", name="mine", command="id")
        aid = self.ids(body["error"])
        cy = Caller("cy@x.io", "cy", "user")
        with self.assertRaisesRegex(PreconditionError, "no such approval"):
            self.human_approves(aid, who=cy)
        self.human_approves(aid, approve=False, who=self.bo)
        self.assertTrue(self.call(s, "lab_exec", name="mine", command="id")[0])  # denied -> fresh request
        self.assertEqual(self.db.approval_get(aid)["status"], "denied")
        # an admin may decide for someone else
        aid2 = self.ids(self.call(s, "lab_exec", name="mine", command="id")[1]["error"])
        self.human_approves(aid2, who=self.max)

    def test_the_ai_has_no_way_to_approve(self):
        s = self.server()
        err, body = self.call(s, "lab_approvals")
        self.assertFalse(err)
        self.call(s, "lab_exec", name="api", command="id")
        pending = self.call(s, "lab_approvals", pending=True)[1]["approvals"]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["state"], "pending")
        self.assertEqual(self.db.approval_get(pending[0]["id"])["status"], "pending")  # listing changed nothing
        self.assertNotIn("lab_approve", s.tools)

    def test_destructive_tools_need_approval_and_do_nothing_without_it(self):
        s = self.server()
        for tool, extra in (("lab_delete", {}), ("lab_archive", {}), ("lab_restore", {})):
            err, body = self.call(s, tool, name="api", **extra)
            self.assertIn("APPROVAL REQUIRED", body["error"], tool)
        self.assertEqual(self.db.get_project("api")["state"], "running")
        self.assertIn("PERMANENTLY DELETE", [a for a in self.db.approval_list() if a["tool"] == "lab_delete"][0]["plan"])

    def test_approved_delete_and_archive_run(self):
        s = self.server()
        aid = self.ids(self.call(s, "lab_archive", name="api")[1]["error"])
        self.human_approves(aid)
        err, body = self.call(s, "lab_archive", name="api")
        self.assertFalse(err, body)
        self.assertEqual(body["state"], "archived")
        aid = self.ids(self.call(s, "lab_delete", name="api")[1]["error"])
        self.human_approves(aid)
        err, body = self.call(s, "lab_delete", name="api")
        self.assertTrue(err)
        self.assertIn("--archives", body["error"])  # archives are kept unless asked for explicitly
        aid = self.ids(self.call(s, "lab_delete", name="api", archives=True)[1]["error"])
        self.human_approves(aid)
        self.assertFalse(self.call(s, "lab_delete", name="api", archives=True)[0])
        self.assertIsNone(self.db.get_project("api"))


class ElicitationTest(ServerBase):
    def test_accept_runs_the_action_and_records_it(self):
        s = self.server(elicitation=True)
        self.answers(s, {"action": "accept", "content": {"approve": True}})
        err, body = self.call(s, "lab_exec", name="api", command="id")
        self.assertFalse(err, body)
        sent = [json.loads(l) for l in s.out.getvalue().splitlines()]
        self.assertEqual(sent[0]["method"], "elicitation/create")
        self.assertIn("work inside project 'api'", sent[0]["params"]["message"])
        self.assertEqual(sent[0]["params"]["requestedSchema"]["required"], ["approve"])
        self.assertIn("api", s.attached)
        (row,) = self.db.approval_list()
        self.assertEqual((row["status"], row["tool"]), ("used", "enter"))
        end = [r for r in self.audit_records() if r["phase"] == "end"][-1]
        self.assertEqual(end["approver"], "max (client prompt)")

    def test_decline_and_cancel_do_not_run_anything(self):
        for response in ({"action": "decline"}, {"action": "cancel"}, {"action": "accept", "content": {"approve": False}}):
            s = self.server(elicitation=True)
            self.answers(s, response)
            err, body = self.call(s, "lab_delete", name="api")
            self.assertTrue(err)
            self.assertIn("declined", body["error"])
            self.assertEqual(self.db.get_project("api")["state"], "running")
            self.assertEqual(self.db.approval_list(), [])  # nothing granted, nothing pending to approve later

    def test_client_that_errors_falls_back_to_the_terminal(self):
        s = self.server(elicitation=True)
        s.inp = io.StringIO(json.dumps({"jsonrpc": "2.0", "id": "lab-1", "error": {"code": -32601, "message": "no"}}) + "\n")
        err, body = self.call(s, "lab_exec", name="api", command="id")
        self.assertIn("lab approve", body["error"])

    def test_without_the_capability_no_request_is_sent(self):
        s = self.server(elicitation=False)
        self.call(s, "lab_exec", name="api", command="id")
        self.assertEqual(s.out.getvalue(), "")


class ProtectedTest(ServerBase):
    def setUp(self):
        super().setUp()
        self.lab().set_protected("api", True)

    def test_destructive_tools_are_refused_outright(self):
        s = self.server()
        for tool in ("lab_stop", "lab_restart", "lab_archive", "lab_restore", "lab_delete"):
            err, body = self.call(s, tool, name="api")
            self.assertTrue(err)
            self.assertIn("protected", body["error"], tool)
            self.assertNotIn("APPROVAL", body["error"])
        self.assertEqual(self.db.get_project("api")["state"], "running")
        self.assertEqual(self.db.approval_list(), [])

    def test_access_needs_a_fresh_approval_every_time_even_when_attached(self):
        s = self.server()
        s.attached.add("api")
        for _ in range(2):
            err, body = self.call(s, "lab_exec", name="api", command="id")
            self.assertIn("PROTECTED", body["error"])
            self.human_approves(self.ids(body["error"]))
            self.assertFalse(self.call(s, "lab_exec", name="api", command="id")[0])

    def test_files_and_logs_are_gated_too(self):
        s = self.server()
        s.attached.add("api")
        for tool, extra in (("lab_read_file", {"path": "/etc/passwd"}), ("lab_write_file", {"path": "/x", "content": "y"}),
                            ("lab_logs", {}), ("lab_start", {})):
            self.assertIn("PROTECTED", self.call(s, tool, name="api", **extra)[1]["error"], tool)


class AuditTest(ServerBase):
    def test_every_call_is_logged_with_identity_and_result(self):
        s = self.server()
        self.call(s, "lab_list")
        self.call(s, "lab_exec", name="api", command="id")  # approval required
        self.call(s, "lab_status", name="nope")
        recs = self.audit_records()
        by_tool = {}
        for r in recs:
            by_tool.setdefault(r["tool"], []).append(r)
        self.assertEqual([r["phase"] for r in by_tool["lab_list"]], ["end"])  # read-only: just the outcome
        self.assertEqual([r["phase"] for r in by_tool["lab_exec"]], ["start", "end"])
        self.assertEqual(by_tool["lab_exec"][1]["result"], "approval_required")
        self.assertEqual(by_tool["lab_status"][0]["result"], "error")
        for r in recs:
            self.assertEqual((r["user"], r["session"]), ("max", "s1"))
        self.assertEqual(by_tool["lab_exec"][1]["project"], "api")

    def test_secrets_are_redacted(self):
        s = self.server()
        s.attached.add("api")
        secret_cmd = "curl -H 'Authorization: Bearer abc123' https://x && export TOKEN=hunter2 && tskey-auth-ZZZ999"
        self.call(s, "lab_exec", name="api", command=secret_cmd)
        self.call(s, "lab_write_file", name="api", path="/home/max/.env", content="API_KEY=topsecretvalue")
        text = Path(self.audit_path).read_text()
        for leaked in ("abc123", "hunter2", "ZZZ999", "topsecretvalue"):
            self.assertNotIn(leaked, text)
        self.assertIn('"chars": 22', text)  # file content is summarised, not stored
        self.assertEqual(redact({"password": "x", "n": 1, "ok": True, "cmd": "id"}),
                         {"password": "[redacted]", "n": 1, "ok": True, "cmd": "id"})

    def test_fails_closed_when_the_log_cannot_be_written(self):
        os.mkdir(os.path.join(self.tmp.name, "audit-is-a-dir"))
        s = self.server()
        s.audit = AuditLog(os.path.join(self.tmp.name, "audit-is-a-dir"))
        s.attached.add("api")
        before = list(self.fake.calls)
        err, body = self.call(s, "lab_exec", name="api", command="touch /tmp/x")
        self.assertTrue(err)
        self.assertIn("audit log", body["error"])
        self.assertEqual(self.fake.calls, before)  # the command never reached the VM

    def test_approver_is_recorded(self):
        s = self.server()
        s.attached.add("api")
        aid = self.ids(self.call(s, "lab_exec", name="api", command="id", root=True)[1]["error"])
        self.human_approves(aid)
        self.call(s, "lab_exec", name="api", command="id", root=True)
        end = [r for r in self.audit_records() if r["phase"] == "end" and r["result"] == "ok"][-1]
        self.assertEqual(end["approver"], "max")


class ToolBehaviourTest(ServerBase):
    def setUp(self):
        super().setUp()
        self.s = self.server()
        self.s.attached.add("api")

    def test_exec_and_background_through_the_tool(self):
        self.fake.guest_results.insert(0, (lambda c: "echo hi" in c, {"exitcode": 0, "out-data": "hi\n"}))
        self.assertEqual(self.call(self.s, "lab_exec", name="api", command="echo hi")[1]["stdout"], "hi\n")
        err, body = self.call(self.s, "lab_exec", name="api", command="make", background=True)
        self.assertRegex(body["job"], r"^[0-9a-f]{12}$")

    def test_download_and_upload_are_base64(self):
        body = base64.b64encode(b"hi").decode()
        self.fake.guest_results.insert(0, (lambda c: "head -c" in c, {"exitcode": 0, "out-data": f"2\n{body}"}))
        res = self.call(self.s, "lab_download", name="api", path="/f")[1]
        self.assertEqual((res["encoding"], res["content"]), ("base64", body))
        before = len(self.fake.stdin_seen)
        self.assertFalse(self.call(self.s, "lab_upload", name="api", path="/f", content_base64=body)[0])
        self.assertEqual(self.fake.stdin_seen[before], body)

    def test_model_visible_output_is_capped(self):
        self.cfg.mcp.max_output_bytes = 50
        self.fake.guest_results.insert(0, (lambda c: "big" in c, {"exitcode": 0, "out-data": "x" * 9999, "err-data": "e" * 9999}))
        res = self.s.call("lab_exec", {"name": "api", "command": "big"})
        self.assertLess(len(res["content"][0]["text"]), 600)

    def test_start_stop_restart(self):
        self.assertEqual(self.call(self.s, "lab_stop", name="api")[1]["state"], "stopped")
        self.assertEqual(self.call(self.s, "lab_start", name="api")[1]["state"], "running")
        self.assertEqual(self.call(self.s, "lab_restart", name="api")[1]["state"], "running")

    def test_no_tool_exposes_secrets_or_config(self):
        self.call(self.s, "lab_create", name="web")
        blob = json.dumps([self.call(self.s, "lab_status", name="web")[1], self.call(self.s, "lab_list")[1]])
        for needle in ("tskey", "csecret", "client_secret", "ts-oauth"):
            self.assertNotIn(needle, blob)


class HumanCliTest(unittest.TestCase):
    def test_exec_arguments(self):
        a = parse_args(["exec", "api", "--timeout", "5", "--", "ls", "-l", "/tmp"])
        self.assertEqual((a.cmd, a.name, a.argv, a.timeout), ("exec", "api", ["ls", "-l", "/tmp"], 5))
        # what comes after `--` belongs to the command, even if it looks like a lab option
        a = parse_args(["--json", "exec", "api", "--root", "--", "ls", "--timeout", "9", "--json"])
        self.assertEqual((a.json, a.root, a.timeout, a.argv), (True, True, 60, ["ls", "--timeout", "9", "--json"]))
        a = parse_args(["exec", "api", "--", "echo", "a b"])
        self.assertEqual(a.argv, ["echo", "a b"])
        self.assertEqual(parse_args(["exec", "api", "uname"]).argv, ["uname"])  # no `--`: still accepted
        a = build_parser().parse_args(["approve", "ab12cd", "--deny"])
        self.assertEqual((a.cmd, a.id, a.deny), ("approve", "ab12cd", True))
        self.assertEqual(build_parser().parse_args(["mcp"]).cmd, "mcp")

    def test_fingerprint_is_order_independent_and_argument_sensitive(self):
        self.assertEqual(fingerprint("t", "p", {"a": 1, "b": 2}), fingerprint("t", "p", {"b": 2, "a": 1}))
        self.assertNotEqual(fingerprint("t", "p", {"a": 1}), fingerprint("t", "p", {"a": 2}))
        self.assertNotEqual(fingerprint("t", "p", {}), fingerprint("t", "q", {}))


class ClientTest(unittest.TestCase):
    CFG = {"host": "pve.tail1.ts.net", "ssh_user": "root"}

    def test_config_roundtrip_and_missing(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / ".lab" / "client.toml"
            with self.assertRaisesRegex(client.ClientError, "lab setup"):
                client.load_config(p)
            client.save_config("pve.tail1.ts.net", "admin", p)
            self.assertEqual(client.load_config(p), {"host": "pve.tail1.ts.net", "ssh_user": "admin"})
            with self.assertRaises(client.ClientError):
                client.save_config('bad host"', "root", p)

    def test_remote_command_is_quoted_for_the_remote_shell(self):
        argv = client.remote_argv(self.CFG, ["exec", "api", "--", "echo 'a b'; rm -rf /"])
        self.assertEqual(argv[:3], ["ssh", "-T", "root@pve.tail1.ts.net"])
        self.assertEqual(len(argv), 4)  # one remote command string, so the local shell never sees the pieces
        import shlex
        self.assertEqual(shlex.split(argv[3]), ["lab", "exec", "api", "--", "echo 'a b'; rm -rf /"])
        self.assertEqual(client.remote_argv(self.CFG, ["init"], tty=True)[1], "-t")

    def test_ssh_config_block_is_idempotent_and_leaves_the_rest_alone(self):
        projects = [{"name": "api", "owner": "max", "tailnet_name": "lab-api.t.ts.net"},
                    {"name": "lan", "owner": "max", "tailnet_name": None}]
        block = client.render_block(self.CFG, projects)
        self.assertIn("Host lab-api\n  HostName lab-api.t.ts.net\n  User max", block)
        self.assertNotIn("lab-lan", block)
        mine = "Host github.com\n  User git\n"
        once = client.merge_block(mine, block)
        self.assertTrue(once.startswith(mine))
        self.assertEqual(client.merge_block(once, block), once)
        newer = client.render_block(self.CFG, projects[:1] + [{"name": "web", "owner": "max", "tailnet_name": "lab-web.t.ts.net"}])
        twice = client.merge_block(once, newer)
        self.assertIn("lab-web", twice)
        self.assertEqual(twice.count(client.BEGIN), 1)
        self.assertTrue(twice.startswith(mine))
        tail = client.merge_block("Host a\n  User b\n" + block + "Host z\n  User y\n", newer)
        self.assertTrue(tail.endswith("Host z\n  User y\n"))  # text after the block survives

    def test_write_ssh_config_backs_up_once(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / ".ssh" / "config"
            p.parent.mkdir()
            p.write_text("Host x\n  User y\n")
            block = client.render_block(self.CFG, [])
            client.write_ssh_config(block, p)
            client.write_ssh_config(block, p)
            self.assertEqual(p.with_name("config.lab-backup").read_text(), "Host x\n  User y\n")
            self.assertEqual(p.read_text().count(client.BEGIN), 1)

    def test_json_flag_goes_before_the_subcommand(self):
        import subprocess
        seen = []
        real = client.run_remote

        def fake(cfg, args, **kw):
            seen.append(list(args))
            return subprocess.CompletedProcess(args, 0, stdout='[{"name": "a"}]', stderr="")
        client.run_remote = fake
        try:
            self.assertEqual(client.remote_json(self.CFG, ["list"]), [{"name": "a"}])
            client.remote_json(self.CFG, ["status", "api"])
            client.remote_json(self.CFG, ["--json", "list"])
        finally:
            client.run_remote = real
        self.assertEqual(seen, [["--json", "list"], ["--json", "status", "api"], ["--json", "list"]])
        # and the real parser accepts exactly that order, so the command line the client builds works on the host
        a = build_parser().parse_args(["--json", "status", "api"])
        self.assertEqual((a.json, a.cmd, a.name), (True, "status", "api"))

    def test_mcp_entry_uses_ssh_stdio_and_no_secrets(self):
        e = client.mcp_server_entry(self.CFG)
        self.assertEqual(e["command"], "ssh")
        self.assertEqual(e["args"][-2:], ["lab", "mcp"])
        self.assertIn("BatchMode=yes", e["args"])
        self.assertIn("-T", e["args"])

    def test_mode_selection(self):
        old = dict(os.environ)
        try:
            os.environ["LAB_HOST"] = "1"
            self.assertTrue(entry.is_host())
            del os.environ["LAB_HOST"]
            os.environ["LAB_CLIENT"] = "1"
            self.assertFalse(entry.is_host())
        finally:
            os.environ.clear()
            os.environ.update(old)


if __name__ == "__main__":
    unittest.main()
