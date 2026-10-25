"""MCP stdio server (SPEC 16). Runs on the Proxmox host, launched by the client over SSH
(`ssh host lab mcp`), as the calling human's lab identity. Newline-delimited JSON-RPC 2.0.

Trust model: tools reach project VMs (guest agent) and lab's own lifecycle code. There is no
host shell, no raw qm and no host file access. Risky actions need a human approval, every call
is audited, and the AI session can never grant approvals itself."""

import base64
import json
import logging
import secrets
import sys
import time

from . import config as config_mod
from .approvals import ApprovalDenied, ApprovalRequired, Approvals, state_of
from .audit import AuditError, AuditLog, redact
from .errors import AccessError, LabError, PreconditionError, UsageError
from .lifecycle import resolve_resources
from .units import parse_size

log = logging.getLogger("lab.mcp")
PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
INSTRUCTIONS = (
    "Tools for the user's personal Proxmox project lab. Work happens inside project VMs. Risky "
    "actions (delete, archive, restore, root commands, entering a project this session has not "
    "created or been attached to, oversized creates) need the human's approval: if a call says "
    "APPROVAL REQUIRED, tell the user the approval ID and wait; never try to work around it. "
    "Protected projects can only be read about, never changed.")
ROOT_TOOLS = ("lab_exec_root", "lab_read_root", "lab_write_root")
DESTRUCTIVE = ("lab_stop", "lab_restart", "lab_archive", "lab_restore", "lab_delete")


def _str(desc, **extra):
    return {"type": "string", "description": desc, **extra}


NAME = _str("project name")
TYPES = {"string": str, "integer": int, "boolean": bool}


class Tool:
    def __init__(self, name, description, props, required, handler, read_only=False):
        self.name, self.description, self.handler, self.read_only = name, description, handler, read_only
        self.schema = {"type": "object", "properties": props, "required": list(required),
                       "additionalProperties": False}

    def validate(self, args) -> dict:
        if not isinstance(args, dict):
            raise UsageError("arguments must be an object")
        extra = sorted(set(args) - set(self.schema["properties"]))
        if extra:
            raise UsageError(f"unexpected argument(s): {', '.join(extra)}")
        for r in self.schema["required"]:
            if r not in args:
                raise UsageError(f"missing argument: {r}")
        for k, v in args.items():
            want = TYPES[self.schema["properties"][k]["type"]]
            if not isinstance(v, want) or (want is int and isinstance(v, bool)):
                raise UsageError(f"argument {k} must be a {self.schema['properties'][k]['type']}")
        return args


class McpServer:
    def __init__(self, lab, cfg, db, audit: AuditLog, inp, out, *, session: str | None = None, clock=time.time,
                 sleep=time.sleep):
        self.lab, self.cfg, self.db, self.audit, self.inp, self.out = lab, cfg, db, audit, inp, out
        self.sleep = sleep
        self.caller = lab.caller
        self.session = session or secrets.token_hex(4)
        self.clock = clock
        self.approvals = Approvals(db, cfg.mcp.approval_ttl_s, self.caller.name, clock)
        self.attached: set = set()
        self.elicitation = False
        self._rid = 0
        self._approver = None
        self.tools = {t.name: t for t in self._tools()}

    # transport ------------------------------------------------------------------------
    def _send(self, obj: dict) -> None:
        self.out.write(json.dumps(obj, separators=(",", ":")) + "\n")
        self.out.flush()

    def _read(self):
        while True:
            line = self.inp.readline()
            if not line:
                return None
            line = line.strip()
            if not line:
                continue
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                self._send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})

    def serve(self) -> None:
        while (msg := self._read()) is not None:
            self.handle(msg)

    def _request(self, method: str, params: dict):
        """Server -> client request (elicitation); waits for the matching response."""
        self._rid += 1
        rid = f"lab-{self._rid}"
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        while (msg := self._read()) is not None:
            if msg.get("id") == rid and ("result" in msg or "error" in msg):
                return msg
            if msg.get("method") == "ping":
                self._send({"jsonrpc": "2.0", "id": msg.get("id"), "result": {}})
        raise ConnectionError("client closed the connection")

    # protocol ---------------------------------------------------------------------------
    def handle(self, msg: dict) -> None:
        method, mid = msg.get("method"), msg.get("id")
        if method is None:
            return  # a stray response
        try:
            if method == "initialize":
                p = msg.get("params") or {}
                self.elicitation = "elicitation" in (p.get("capabilities") or {})
                info = p.get("clientInfo") or {}
                try:  # lets the admin see what this client supports (e.g. whether it can show approval prompts)
                    self.audit.write(phase="session", session=self.session, user=self.caller.name,
                                     client=f"{info.get('name')} {info.get('version')}",
                                     capabilities=sorted(p.get("capabilities") or {}), elicitation=self.elicitation,
                                     watcher=self._watcher_online())
                except AuditError as e:
                    log.error("%s", e)
                want = p.get("protocolVersion")
                self._reply(mid, {"protocolVersion": want if want in PROTOCOLS else PROTOCOLS[0],
                                  "capabilities": {"tools": {"listChanged": False}},
                                  "serverInfo": {"name": "project-lab", "version": "0.0.1"},
                                  "instructions": INSTRUCTIONS})
            elif method == "ping":
                self._reply(mid, {})
            elif method == "tools/list":
                self._reply(mid, {"tools": [{"name": t.name, "description": t.description,
                                             "inputSchema": t.schema} for t in self.tools.values()]})
            elif method == "tools/call":
                p = msg.get("params") or {}
                self._reply(mid, self.call(p.get("name"), p.get("arguments") or {}))
            elif mid is not None:
                self._send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"unknown method {method}"}})
        except ConnectionError:
            raise
        except Exception:  # never let a bug kill the session or leak a traceback to the model
            log.exception("internal error handling %s", method)
            if mid is not None:
                self._send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": "internal error"}})

    def _reply(self, mid, result) -> None:
        if mid is not None:
            self._send({"jsonrpc": "2.0", "id": mid, "result": result})

    # calls -------------------------------------------------------------------------------
    def call(self, name, args) -> dict:
        tool = self.tools.get(name)
        if tool is None:
            return self._result({"error": f"unknown tool {name}"}, True)
        project = args.get("name") if isinstance(args, dict) and isinstance(args.get("name"), str) else None
        self._approver = None
        outcome, detail, is_error = "ok", None, False
        t0 = self.clock()
        try:
            tool.validate(args)
            if not tool.read_only:
                self.audit.write(phase="start", session=self.session, user=self.caller.name, tool=name,
                                 project=project, args=args)
            data = tool.handler(args)
        except ApprovalRequired as e:
            outcome, detail, is_error, data = "approval_required", e.approval_id, True, {"error": str(e)}
        except ApprovalDenied as e:
            outcome, detail, is_error, data = "denied", str(e), True, {"error": str(e)}
        except AuditError as e:
            return self._result({"error": str(e)}, True)  # cannot even record it: do nothing
        except LabError as e:
            outcome, detail, is_error, data = "error", str(e), True, {"error": str(e)}
        except Exception:
            log.exception("tool %s crashed", name)
            outcome, detail, is_error, data = "error", "internal error", True, {"error": "internal error"}
        try:
            self.audit.write(phase="end", session=self.session, user=self.caller.name, tool=name, project=project,
                             args=args, result=outcome, detail=detail if is_error else None,
                             approver=self._approver, ms=int((self.clock() - t0) * 1000))
        except AuditError as e:
            log.error("%s", e)
            if not tool.read_only and not is_error:
                data, is_error = {"error": f"the action ran but could not be audited: {e}"}, True
        return self._result(data, is_error)

    def _result(self, data, is_error: bool) -> dict:
        text = json.dumps(data, default=str)
        cap = 2 * self.cfg.mcp.max_output_bytes + 4096
        if len(text) > cap:
            text = text[:cap] + "...[truncated]"
        return {"content": [{"type": "text", "text": text}], "isError": is_error}

    # approvals ------------------------------------------------------------------------------
    def _elicit(self, plan: str):
        """True/False from the human via the client UI, or None if unavailable."""
        if not self.elicitation:
            return None
        resp = self._request("elicitation/create", {
            "message": plan + "\n\nApprove this action?",
            "requestedSchema": {"type": "object", "properties": {
                "approve": {"type": "boolean", "title": "Approve", "description": "Allow exactly this action"}},
                "required": ["approve"]}})
        if "error" in resp:
            return None
        r = resp["result"]
        return r.get("action") == "accept" and (r.get("content") or {}).get("approve") is True

    def _approve(self, tool: str, project, args: dict, plan: str) -> None:
        """Return only if a human approved exactly this action; otherwise raise."""
        if tool in ROOT_TOOLS and self.cfg.mcp.root_without_approval:
            # the admin chose this in the config: no popup, but every use stays in the approvals log
            self._approver = "auto (mcp.root_without_approval)"
            self.approvals.record_granted(tool, project, args, plan, self.session, self._approver)
            return
        row = self.approvals.take(tool, project, args)
        if row:
            self._approver = row["decided_by"]
            return
        answer = self._elicit(plan)
        if answer is True:
            self._approver = f"{self.caller.name} (client prompt)"
            self.approvals.record_granted(tool, project, args, plan, self.session, self._approver)
            return
        if answer is False:
            raise ApprovalDenied(f"the user declined: {plan}")
        aid = self.approvals.request(tool, project, args, plan, self.session)
        if self._watcher_online():
            # a popup is on the user's screen: hold this call until they click (or the wait runs out)
            deadline = self.clock() + self.cfg.mcp.approval_wait_s
            while self.clock() < deadline:
                self.sleep(1)
                state = self.db.approval_get(aid)["status"]
                if state == "denied":
                    raise ApprovalDenied(f"the user denied: {plan}")
                if state == "approved":
                    row = self.approvals.take(tool, project, args)
                    if row:
                        self._approver = row["decided_by"]
                        return
        raise ApprovalRequired(aid, plan)

    def _watcher_online(self) -> bool:
        age = self.db.heartbeat_age("approver", self.caller.name, self.clock())
        return age is not None and age < 20

    def _row(self, name: str):
        return self.lab._mine(name)  # same "no such project" for missing and not-yours

    def _forbid_protected(self, row, what: str) -> None:
        if row["protected"]:
            raise PreconditionError(f"{row['name']} is protected; {what} is not available through MCP "
                                    "(the owner can run `lab unprotect` on the host)")

    def _enter(self, name: str, tool: str, args: dict) -> None:
        """Gate for working inside a project: attachment per session; protected projects need a fresh approval."""
        row = self._row(name)
        if row["protected"]:
            self._approve(tool, name, args, f"'{name}' is a PROTECTED project. Allow this AI session to run: "
                                               f"{self._brief(tool, args)}")
            return
        if name in self.attached:
            return
        if row["vmid"] is not None and self.db.ai_created_has(name, row["vmid"], self.caller.name):
            self.attached.add(name)  # an earlier AI session of this user made it (this server restarted since)
            return
        if self.cfg.mcp.attach_without_approval and row["owner"] == self.caller.name:
            # the admin chose this: sessions may enter their own user's projects; still logged
            self._approver = "auto (mcp.attach_without_approval)"
            self.approvals.record_granted("enter", name, {"session": self.session},
                                          f"Enter own project '{name}'", self.session, self._approver)
            self.attached.add(name)
            return
        self._approve("enter", name, {"session": self.session},
                      f"Let this AI session work inside project '{name}' (owner {row['owner']}): run commands, "
                      "read and write files, start and stop it. It did not create it and it was not attached.")
        self.attached.add(name)

    @staticmethod
    def _brief(tool: str, args: dict) -> str:
        shown = {k: v for k, v in args.items() if k not in ("content",)}
        return f"{tool} {json.dumps(redact(shown), default=str)[:300]}"

    # tools --------------------------------------------------------------------------------------
    def _tools(self):
        L = self.lab
        T = Tool
        root = {"type": "boolean", "description": "run as root (needs human approval unless the admin set mcp.root_without_approval)"}

        def t_list(a):
            rows = L.list(everything=bool(a.get("all")))
            return {"projects": [{"name": r["name"], "protected": True} if r["protected"] else r for r in rows]}

        def t_status(a):
            row = self._row(a["name"])
            if row["protected"]:
                return {"name": row["name"], "protected": True}
            return L.status(a["name"])

        def t_create(a):
            unknown = set(a) - {"name", "distro", "size", "cpu", "ram", "disk", "clone_mode"}
            if unknown:
                raise UsageError(f"unexpected argument(s): {', '.join(sorted(unknown))}")
            req = config_mod.project_request({k: a[k] for k in ("distro", "cpu", "ram", "disk", "clone_mode") if k in a})
            cpu, ram_mb, disk_gb = resolve_resources(self.cfg, a.get("size"), req.get("cpu"), req.get("ram"), req.get("disk"))
            caps, reasons = self.cfg.mcp, []
            if cpu > caps.max_cpu:
                reasons.append(f"{cpu} vCPUs (AI limit {caps.max_cpu})")
            if ram_mb > parse_size(caps.max_ram) // (1 << 20):
                reasons.append(f"{ram_mb} MiB RAM (AI limit {caps.max_ram})")
            running = sum(1 for p in L.list() if p["state"] == "running")
            if running + 1 > caps.max_running_vms:
                reasons.append(f"{running + 1} running VMs (AI limit {caps.max_running_vms})")
            if reasons:
                self._approve("lab_create", a["name"], a, f"Create '{a['name']}' beyond the AI limits: " + "; ".join(reasons))
            res = L.create(a["name"], distro=req.get("distro"), size=a.get("size"), cpu=req.get("cpu"),
                           ram=req.get("ram"), disk=req.get("disk"), clone_mode=req.get("clone_mode"))
            self.attached.add(a["name"])
            made = self.db.get_project(a["name"])
            if made and made["vmid"] is not None:  # remembered beyond this process (a restart must not forget it)
                self.db.ai_created_add(a["name"], made["vmid"], self.caller.name)
            return res

        def lifecycle(fn, tool):
            def run(a):
                row = self._row(a["name"])
                if tool in DESTRUCTIVE:
                    self._forbid_protected(row, tool[4:])
                self._enter(a["name"], tool, a)
                return fn(a["name"])
            return run

        def t_archive(a):
            row = self._row(a["name"])
            self._forbid_protected(row, "archive")
            self._approve("lab_archive", a["name"], a,
                          f"ARCHIVE '{a['name']}': stop it, back it up to the archive disk, verify the backup with a "
                          "trial restore and boot test, then remove it from the fast disk. It is unavailable until restored.")
            return L.archive(a["name"])

        def t_restore(a):
            row = self._row(a["name"])
            self._forbid_protected(row, "restore")
            self._approve("lab_restore", a["name"], a,
                          f"RESTORE '{a['name']}' from its archive onto the fast disk and start it.")
            return L.restore(a["name"])

        def t_delete(a):
            row = self._row(a["name"])
            self._forbid_protected(row, "delete")
            n = len(self.db.list_archives(a["name"]))
            with_arc = bool(a.get("archives"))
            self._approve("lab_delete", a["name"], a,
                          f"PERMANENTLY DELETE '{a['name']}' (VM {row['vmid']}) and its disk"
                          + (f", and its {n} archive(s)" if with_arc else (f". Its {n} archive(s) are kept" if n else ""))
                          + ". This cannot be undone.")
            L.delete(a["name"], a["name"], archives=with_arc)
            self.attached.discard(a["name"])
            return {"deleted": a["name"]}

        def t_attach(a):
            self._enter(a["name"], "lab_attach", a)
            self.attached.add(a["name"])
            return {"attached": a["name"]}

        def t_exec(a):
            self._enter(a["name"], "lab_exec", a)
            if a.get("root"):
                self._approve("lab_exec_root", a["name"], a, f"Run as ROOT in '{a['name']}': {a['command'][:500]}")
            if a.get("background"):
                return L.exec_background(a["name"], a["command"], root=bool(a.get("root")), cwd=a.get("cwd"))
            return L.exec(a["name"], a["command"], root=bool(a.get("root")), timeout=a.get("timeout", 60), cwd=a.get("cwd"))

        def job_tool(fn, tool):
            def run(a):
                self._enter(a["name"], tool, a)
                return fn(a["name"], a["job"])
            return run

        def t_read(a):
            self._enter(a["name"], "lab_read_file", a)
            if a.get("root"):
                self._approve("lab_read_root", a["name"], a, f"Read '{a['path']}' as ROOT in '{a['name']}'")
            return L.read_file(a["name"], a["path"], root=bool(a.get("root")))

        def t_write(a):
            self._enter(a["name"], "lab_write_file", a)
            if a.get("root"):
                self._approve("lab_write_root", a["name"], a, f"Write '{a['path']}' as ROOT in '{a['name']}'")
            return L.write_file(a["name"], a["path"], a["content"], root=bool(a.get("root")))

        def t_upload(a):
            self._enter(a["name"], "lab_upload", a)
            if a.get("root"):
                self._approve("lab_write_root", a["name"], a, f"Write '{a['path']}' as ROOT in '{a['name']}'")
            return L.write_file(a["name"], a["path"], a["content_base64"], base64_encoded=True, root=bool(a.get("root")))

        def t_download(a):
            self._enter(a["name"], "lab_download", a)
            res = L.read_file(a["name"], a["path"])
            if res["encoding"] != "base64":
                res = {**res, "encoding": "base64", "content": base64.b64encode(res["content"].encode()).decode()}
            return res

        def t_logs(a):
            self._enter(a["name"], "lab_logs", a)
            return L.logs(a["name"], a.get("which", "ops"), a.get("lines", 50))

        def t_approvals(a):
            now = self.clock()
            rows = [r for r in self.db.approval_list(self.caller.name) if not a.get("pending") or state_of(r, now) == "pending"]
            return {"approvals": [{"id": r["id"], "tool": r["tool"], "project": r["project"],
                                   "state": state_of(r, now), "plan": r["plan"]} for r in rows[:20]]}

        return [
            T("lab_list", "List your projects (protected ones show only their name).",
              {"all": {"type": "boolean", "description": "admins: every user's projects"}}, [], t_list, True),
            T("lab_status", "Status of one project: state, resources, tailnet name, ssh command.",
              {"name": NAME}, ["name"], t_status, True),
            T("lab_create", "Create a project VM (owned by the user you act for). Joins the tailnet; returns the ssh command.",
              {"name": NAME, "distro": _str("e.g. ubuntu-24.04"), "size": _str("small|medium|large"),
               "cpu": {"type": "integer", "description": "vCPUs"}, "ram": _str("e.g. 4G"), "disk": _str("e.g. 64G"),
               "clone_mode": _str("full|linked")}, ["name"], t_create),
            T("lab_attach", "Attach this session to a project it did not create (needs human approval unless the admin set mcp.attach_without_approval).",
              {"name": NAME}, ["name"], t_attach),
            T("lab_start", "Start a project VM.", {"name": NAME}, ["name"], lifecycle(L.start, "lab_start")),
            T("lab_stop", "Gracefully stop a project VM.", {"name": NAME}, ["name"], lifecycle(L.stop, "lab_stop")),
            T("lab_restart", "Gracefully restart a project VM.", {"name": NAME}, ["name"], lifecycle(L.restart, "lab_restart")),
            T("lab_archive", "Archive a project to the archive disk after verifying the backup (needs human approval).",
              {"name": NAME}, ["name"], t_archive),
            T("lab_restore", "Restore an archived project (needs human approval).", {"name": NAME}, ["name"], t_restore),
            T("lab_delete", "Permanently delete a project (needs human approval). Archives are kept unless archives=true.",
              {"name": NAME, "archives": {"type": "boolean", "description": "also delete its archives"}}, ["name"], t_delete),
            T("lab_exec", "Run a shell command in a project VM as its user. Use background=true for long jobs, then lab_exec_poll.",
              {"name": NAME, "command": _str("shell command"), "root": root, "cwd": _str("working directory"),
               "timeout": {"type": "integer", "description": "seconds, 1-3600 (default 60)"},
               "background": {"type": "boolean", "description": "return a job id immediately"}}, ["name", "command"], t_exec),
            T("lab_exec_poll", "Output and exit code of a background job.", {"name": NAME, "job": _str("job id")},
              ["name", "job"], job_tool(L.exec_poll, "lab_exec_poll")),
            T("lab_exec_cancel", "Cancel a background job.", {"name": NAME, "job": _str("job id")},
              ["name", "job"], job_tool(L.exec_cancel, "lab_exec_cancel")),
            T("lab_read_file", "Read a text/binary file in a project VM (size limited).",
              {"name": NAME, "path": _str("path in the VM"), "root": root}, ["name", "path"], t_read),
            T("lab_write_file", "Write a text file in a project VM (size limited, atomic).",
              {"name": NAME, "path": _str("path in the VM"), "content": _str("file content"), "root": root},
              ["name", "path", "content"], t_write),
            T("lab_upload", "Write a binary file (base64) into a project VM.",
              {"name": NAME, "path": _str("path in the VM"), "content_base64": _str("base64 data"), "root": root},
              ["name", "path", "content_base64"], t_upload),
            T("lab_download", "Fetch a file from a project VM as base64.", {"name": NAME, "path": _str("path in the VM")},
              ["name", "path"], t_download),
            T("lab_logs", "Operation history ('ops') or first-boot setup log ('setup') of a project.",
              {"name": NAME, "which": _str("ops|setup"), "lines": {"type": "integer", "description": "1-500"}},
              ["name"], t_logs),
            T("lab_approvals", "Your approvals and their state (you cannot grant them; the user runs `lab approve ID`).",
              {"pending": {"type": "boolean", "description": "only pending"}}, [], t_approvals, True),
        ]


def run(lab, cfg, db, stdin=None, stdout=None) -> None:
    server = McpServer(lab, cfg, db, AuditLog(cfg.paths.audit_log), stdin or sys.stdin, stdout or sys.stdout)
    try:
        server.serve()
    except ConnectionError:
        pass
