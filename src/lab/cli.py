"""Command-line entry point (host core). M1: init, doctor, template, distros, plus M0 plumbing."""

import argparse
import base64
import json
import logging
import os
import shlex
import sys
import time
from pathlib import Path

from . import __version__, access, distros, doctor as doctor_mod
from . import config as config_mod
from . import init as init_mod
from . import mcp as mcp_mod
from . import progress
from .approvals import Approvals, state_of
from .db import DB
from .errors import LabError, PreconditionError, UsageError
from .hostio import DryRunHostIO, HostIO
from .lifecycle import Lab
from .prompt import ScriptedPrompter, TerminalPrompter
from .proxmox import Proxmox
from .runner import DryRunRunner, SubprocessRunner
from .tailscale import Tailscale
from .template import TemplateBuilder


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="lab", description="Project lab on Proxmox")
    p.add_argument("--config", help="config file (default: $LAB_CONFIG or /etc/project-lab/config.toml)")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--dry-run", action="store_true", help="run read-only commands, print mutating ones instead")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("version", help="print version")
    sub.add_parser("config", help="show effective configuration")
    sub.add_parser("discover", help="read-only: Proxmox version, storages, existing VMs")
    sub.add_parser("distros", help="list supported distros")

    i = sub.add_parser("init", help="discover the host, choose disks, write the host config")
    i.add_argument("--fast-disk")
    i.add_argument("--archive-disk")
    i.add_argument("--snippet-disk")
    i.add_argument("--bridge")
    i.add_argument("--yes", action="store_true", help="non-interactive: accept confirmations")

    sub.add_parser("doctor", help="read-only health and drift checks")

    c = sub.add_parser("create", help="create a project VM")
    c.add_argument("name")
    c.add_argument("--distro")
    c.add_argument("--size", help="preset: small|medium|large (see config presets)")
    c.add_argument("--cpu", type=int)
    c.add_argument("--ram", help="e.g. 4G or 4096M")
    c.add_argument("--disk", help="e.g. 64G (grow only)")
    c.add_argument("--full", dest="clone_mode", action="store_const", const="full")
    c.add_argument("--linked", dest="clone_mode", action="store_const", const="linked")
    c.add_argument("--owner", help="admin only: create for another lab user")
    c.add_argument("--no-tailscale", action="store_true", help="skip tailnet enrolment (guest agent access only)")

    ls = sub.add_parser("list", help="list projects")
    ls.add_argument("--all", action="store_true", help="admin: every user's projects")
    for nm in ("status", "start", "restart", "protect", "unprotect"):
        sub.add_parser(nm).add_argument("name")
    st = sub.add_parser("stop")
    st.add_argument("name")
    sub.choices["stop"].add_argument("--force", action="store_true", help="hard stop if graceful shutdown times out")
    sub.choices["restart"].add_argument("--force", action="store_true")
    ex = sub.add_parser("exec", help="run a command in a project VM (as its user)")
    ex.add_argument("name")
    ex.add_argument("--root", action="store_true")
    ex.add_argument("--timeout", type=int, default=60)
    ex.add_argument("--cwd")
    ex.add_argument("argv", nargs="*", help="-- COMMAND [ARGS...]")
    sub.add_parser("put", help="write stdin to DST in a project VM").add_argument("name")
    sub.choices["put"].add_argument("dst")
    sub.add_parser("get", help="write a file from a project VM to stdout").add_argument("name")
    sub.choices["get"].add_argument("src")
    ap = sub.add_parser("approve", help="approve (or --deny) an action an AI session asked for")
    ap.add_argument("id")
    ap.add_argument("--deny", action="store_true")
    ap.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    sub.add_parser("approvals", help="list approvals").add_argument("--all", action="store_true")
    sub.add_parser("mcp", help="MCP stdio server; launched by the client over SSH")
    ar = sub.add_parser("archive", help="back up to the archive disk, verify, then free the fast disk")
    ar.add_argument("name")
    ar.add_argument("--keep-active", action="store_true", help="verify only; keep the active VM")
    ar.add_argument("--no-boot-test", action="store_true", help="skip the boot test (discouraged)")
    rs = sub.add_parser("restore", help="bring an archived project back onto the fast disk")
    rs.add_argument("name")
    rs.add_argument("--no-start", action="store_true")
    sub.add_parser("recover", help="finish or undo an interrupted operation").add_argument("name")
    d = sub.add_parser("delete", help="destroy a project VM")
    d.add_argument("name")
    d.add_argument("--archives", action="store_true", help="also delete the project's archives")
    d.add_argument("--yes", action="store_true", help="confirm by flag instead of typing the name")
    d.add_argument("--force", action="store_true")

    u = sub.add_parser("user", help="manage lab users (admin)")
    us = u.add_subparsers(dest="ucmd", required=True)
    ua = us.add_parser("add")
    ua.add_argument("login")
    ua.add_argument("name")
    ua.add_argument("--admin", action="store_true")
    us.add_parser("list")
    us.add_parser("remove").add_argument("name")
    p.add_argument("--as", dest="as_name", help="admin: act as another lab user")

    t = sub.add_parser("template", help="manage golden templates")
    ts = t.add_subparsers(dest="tcmd", required=True)
    ts.add_parser("list")
    ts.add_parser("check", help="verify distro image and checksum URLs are reachable")
    for name in ("add", "update", "rm"):
        c = ts.add_parser(name)
        c.add_argument("distro")
    return p


def _emit(args, data, text):
    print(json.dumps(data, indent=2, default=str) if args.json else text)


class Ctx:
    """Wires real or dry-run collaborators for one command."""

    def __init__(self, args):
        self.args = args
        self.cfg = config_mod.load(args.config)
        real = SubprocessRunner()
        self.runner = DryRunRunner(real) if args.dry_run else real
        self.io = DryRunHostIO() if args.dry_run else HostIO()
        self.px = Proxmox(self.runner)
        self._db = None
        self.rep = progress.make()  # stderr: a live window on a terminal, plain lines otherwise

    def db(self, create: bool = False):
        path = self.cfg.paths.db
        if self._db is None and (create or Path(path).exists()):
            self._db = DB(path)
        return self._db

    def templates(self, say=None) -> TemplateBuilder:
        return TemplateBuilder(self.cfg, self.px, self.io, self.db(), progress=say or self.rep)

    def caller(self):
        db = self.db()
        if db is None:
            raise PreconditionError("lab is not initialised; run `lab init`")
        return access.resolve_caller(db, dict(os.environ), self.runner, as_name=self.args.as_name)

    def lab(self, say=None) -> Lab:
        say = say or self.rep
        caller = self.caller()
        return Lab(self.cfg, self.px, self.io, self.db(), self.templates(say), Tailscale(self.cfg, self.px, self.io),
                   caller, say=say)


def cmd_discover(ctx):
    px, args = ctx.px, ctx.args
    data = {
        "pve_version": px.version(),
        "storages": [{"id": s["storage"], "type": s.get("plugintype"), "content": s.get("content"),
                      "total": s.get("maxdisk"), "used": s.get("disk")} for s in px.storages()],
        "vms": [{"vmid": v["vmid"], "name": v.get("name"), "status": v.get("status"),
                 "template": bool(v.get("template"))} for v in px.vms()],
    }
    lines = [f"Proxmox VE {data['pve_version']}", "", "Storages:"]
    lines += [f"  {s['id']:<16} {s['type'] or '?':<10} {s['content'] or ''}" for s in data["storages"]]
    lines += ["", "VMs:"]
    lines += [f"  {v['vmid']:>6} {v['name'] or '':<24} {v['status']}{' (template)' if v['template'] else ''}"
              for v in data["vms"]]
    _emit(args, data, "\n".join(lines))


def cmd_init(ctx):
    a = ctx.args
    answers = {k: v for k, v in {
        "Fast disk": a.fast_disk, "Archive disk": a.archive_disk,
        "Snippet storage": a.snippet_disk, "Network bridge": a.bridge}.items() if v}
    scripted = bool(answers) or a.yes
    ui = ScriptedPrompter(answers, assume_yes=a.yes) if scripted else TerminalPrompter()
    if scripted:
        ui.info = print
    cfg_file = a.config or config_mod.config_path()
    res = init_mod.run_init(
        ctx.px, ctx.io, ctx.runner, ui, existing=ctx.cfg, config_file=cfg_file,
        db=None if a.dry_run else ctx.db(create=True),
        env=dict(os.environ), write=True,
    )
    print("\nTailscale policy to add:\n" + res.acl_snippet)
    for n in res.notes:
        print("note: " + n)
    print(f"\nConfig written to {cfg_file}. Next: `lab doctor`, then `lab template add {res.config.vm.default_distro}`.")


def cmd_doctor(ctx):
    checks = doctor_mod.run_checks(ctx.cfg, ctx.px, ctx.io, ctx.db(), ctx.templates(lambda m: None))
    if ctx.args.json:
        print(json.dumps([c.__dict__ for c in checks], indent=2))
    else:
        print(doctor_mod.render(checks))
    return doctor_mod.summarize(checks)


def cmd_template(ctx):
    a = ctx.args
    tb = ctx.templates()
    if a.tcmd == "list":
        rows = tb.list()
        _emit(a, rows, "\n".join(
            f"  {r['distro']:<16} " + (f"VMID {r['vmid']}  built {r['built'] or '?'}" if r["present"] else "-")
            for r in rows))
    elif a.tcmd == "check":
        bad = 0
        for d in distros.load().values():
            ok = ctx.io.url_exists(d.image_url)
            try:
                distros.find_checksum(ctx.io.fetch_text(d.checksum_url), d.checksum_name, d.checksum_type)
                cs = True
            except LabError:
                cs = False
            bad += not (ok and cs)
            print(f"  {d.id:<16} image {'ok' if ok else 'UNREACHABLE':<12} checksum {'ok' if cs else 'MISSING'}")
        return 5 if bad else 0
    elif a.tcmd == "add":
        tb.build(a.distro)
    elif a.tcmd == "update":
        tb.update(a.distro)
    elif a.tcmd == "rm":
        tb.remove(a.distro)
    return 0


def _table(rows, cols):
    if not rows:
        return "(none)"
    widths = [max(len(c), *(len(str(r.get(c) if r.get(c) is not None else "-")) for r in rows)) for c in cols]
    line = lambda vals: "  ".join(str(v).ljust(w) for v, w in zip(vals, widths)).rstrip()
    return "\n".join([line(cols)] + [line([r.get(c) if r.get(c) is not None else "-" for c in cols]) for r in rows])


def cmd_project(ctx):
    a = ctx.args
    lab = ctx.lab()
    if a.cmd == "create":
        res = lab.create(a.name, distro=a.distro, size=a.size, cpu=a.cpu, ram=a.ram, disk=a.disk,
                         clone_mode=a.clone_mode, owner=a.owner, tailscale=not a.no_tailscale)
        _emit(a, res, f"created {res['name']} (VMID {res['vmid']}, {res['state']})"
              + (f"\n  connect: {res['ssh']}" if res.get("ssh") else "\n  no tailnet; reach it via the guest agent / LAN"))
    elif a.cmd == "list":
        rows = lab.list(everything=a.all)
        cols = ["name", "owner", "state", "vmid", "cpu", "ram_mb", "disk_gb", "tailnet_name", "tailnet_ip"]
        _emit(a, rows, _table(rows, cols))
    elif a.cmd in ("status", "start", "stop", "restart"):
        fn = {"status": lab.status, "start": lab.start}.get(a.cmd)
        res = fn(a.name) if fn else getattr(lab, a.cmd)(a.name, force=a.force)
        _emit(a, res, json.dumps(res, indent=2, default=str))
    elif a.cmd == "delete":
        confirm = a.name if a.yes else input(f"Type the project name ({a.name}) to confirm deletion: ").strip()
        lab.delete(a.name, confirm, force=a.force, archives=a.archives)
        print(f"deleted {a.name}")
    elif a.cmd == "archive":
        res = lab.archive(a.name, keep_active=a.keep_active, boot_test=not a.no_boot_test)
        _emit(a, res, f"{a.name}: {res['state']} (verified archive on the archive disk)")
    elif a.cmd == "restore":
        res = lab.restore(a.name, start=not a.no_start)
        _emit(a, res, f"{a.name}: {res['state']}" + (f"\n  connect: {res['ssh']}" if res.get("ssh") else ""))
    elif a.cmd == "recover":
        print(lab.recover(a.name))
    elif a.cmd in ("protect", "unprotect"):
        lab.set_protected(a.name, a.cmd == "protect")
        print(f"{a.name}: {a.cmd}ed")
    return 0


def cmd_guest(ctx):
    a = ctx.args
    lab = ctx.lab(say=lambda m: print(m, file=sys.stderr))
    if a.cmd == "exec":
        cmd = a.argv[1:] if a.argv[:1] == ["--"] else a.argv
        if not cmd:
            raise UsageError("usage: lab exec NAME -- COMMAND [ARGS...]")
        res = lab.exec(a.name, cmd[0] if len(cmd) == 1 else shlex.join(cmd), root=a.root, timeout=a.timeout, cwd=a.cwd)
        if a.json:
            print(json.dumps(res))
        else:
            sys.stdout.write(res["stdout"])
            sys.stderr.write(res["stderr"])
        if res["timed_out"]:
            return 124
        return int(res["exit_code"] or 0) & 0xFF
    if a.cmd == "put":
        limit = ctx.cfg.mcp.max_file_bytes
        data = sys.stdin.buffer.read(limit + 1)
        if len(data) > limit:
            raise UsageError(f"file too large for the guest agent ({limit} bytes); use scp over Tailscale")
        res = lab.write_file(a.name, a.dst, base64.b64encode(data).decode(), base64_encoded=True)
        _emit(a, res, f"wrote {res['bytes']} bytes to {a.dst}")
    elif a.cmd == "get":
        res = lab.read_file(a.name, a.src)
        if res["truncated"]:
            raise UsageError(f"file is {res['size']} bytes, over the {ctx.cfg.mcp.max_file_bytes} byte limit; use scp")
        sys.stdout.buffer.write(res["content"].encode() if res["encoding"] == "utf-8" else base64.b64decode(res["content"]))
        sys.stdout.buffer.flush()
    return 0


def cmd_approvals(ctx):
    a = ctx.args
    caller, db = ctx.caller(), ctx.db()
    ap = Approvals(db, ctx.cfg.mcp.approval_ttl_s, caller.name)
    if a.cmd == "approvals":
        now = time.time()
        rows = db.approval_list(None if (a.all and caller.is_admin) else caller.name)
        out = [{"id": r["id"], "user": r["user"], "tool": r["tool"], "project": r["project"],
                "state": state_of(r, now), "plan": r["plan"][:70]} for r in rows]
        _emit(a, out, _table(out, ["id", "user", "tool", "project", "state", "plan"]))
        return 0
    row = db.approval_get(a.id)
    if row is None or not (caller.is_admin or row["user"] == caller.name):
        raise PreconditionError(f"no such approval: {a.id}")
    print(f"Requested by an AI session for {row['user']}:\n  {row['plan']}")
    if not a.yes and not a.deny:
        if input("Approve this action? [y/N] ").strip().lower() not in ("y", "yes"):
            print("not approved")
            return 0
    ap.decide(a.id, not a.deny, caller.name, caller.is_admin)
    print("denied" if a.deny else "approved (single use; the AI must repeat the same call)")
    return 0


def cmd_mcp(ctx):
    lab = ctx.lab(say=lambda m: print(m, file=sys.stderr))
    mcp_mod.run(lab, ctx.cfg, ctx.db())
    return 0


def cmd_user(ctx):
    a = ctx.args
    caller = ctx.caller()
    db = ctx.db()
    if a.ucmd == "add":
        access.add_user(db, caller, a.login, a.name, "admin" if a.admin else "user")
        print(f"added {a.name} ({a.login})")
    elif a.ucmd == "list":
        access.require_admin(caller)
        rows = [{"name": u["name"], "login": u["login"], "role": u["role"]} for u in db.list_users()]
        _emit(a, rows, _table(rows, ["name", "login", "role"]))
    elif a.ucmd == "remove":
        access.remove_user(db, caller, a.name)
        print(f"removed {a.name}")
    return 0


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(message)s")
    ctx, ok = None, False
    try:
        ctx = Ctx(args)
        rc = _run(ctx, args)
        ok = rc == 0
        return rc
    except LabError as e:
        if ctx:
            ctx.rep.finish(False)  # close the live progress display before the error text
        print(f"error: {e}", file=sys.stderr)
        return e.exit_code
    finally:
        if ctx:
            ctx.rep.finish(ok)


def _run(ctx, args) -> int:
    if args.cmd == "version":
        _emit(args, {"version": __version__}, f"lab {__version__}")
    elif args.cmd == "config":
        _emit(args, ctx.cfg.to_dict(), json.dumps(ctx.cfg.to_dict(), indent=2, default=list))
    elif args.cmd == "discover":
        cmd_discover(ctx)
    elif args.cmd == "distros":
        cat = distros.load()
        _emit(args, sorted(cat), "\n".join(f"  {d.id:<16} {d.family}  user={d.default_user}" for d in cat.values()))
    elif args.cmd == "init":
        cmd_init(ctx)
    elif args.cmd == "doctor":
        return cmd_doctor(ctx)
    elif args.cmd == "template":
        return cmd_template(ctx)
    elif args.cmd == "user":
        return cmd_user(ctx)
    elif args.cmd in ("exec", "put", "get"):
        return cmd_guest(ctx)
    elif args.cmd in ("approve", "approvals"):
        return cmd_approvals(ctx)
    elif args.cmd == "mcp":
        return cmd_mcp(ctx)
    elif args.cmd in ("create", "list", "status", "start", "stop", "restart", "delete", "protect", "unprotect",
                      "archive", "restore", "recover"):
        return cmd_project(ctx)
    return 0


if __name__ == "__main__":
    sys.exit(main())
