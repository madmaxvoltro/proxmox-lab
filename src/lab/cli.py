"""Command-line entry point (host core). M1: init, doctor, template, distros, plus M0 plumbing."""

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from . import __version__, distros, doctor as doctor_mod
from . import config as config_mod
from . import init as init_mod
from .db import DB
from .errors import LabError
from .hostio import DryRunHostIO, HostIO
from .prompt import ScriptedPrompter, TerminalPrompter
from .proxmox import Proxmox
from .runner import DryRunRunner, SubprocessRunner
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

    def db(self, create: bool = False):
        path = self.cfg.paths.db
        if self._db is None and (create or Path(path).exists()):
            self._db = DB(path)
        return self._db

    def templates(self, progress=print) -> TemplateBuilder:
        return TemplateBuilder(self.cfg, self.px, self.io, self.db(), progress=progress)


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


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(message)s")
    try:
        ctx = Ctx(args)
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
        return 0
    except LabError as e:
        print(f"error: {e}", file=sys.stderr)
        return e.exit_code


if __name__ == "__main__":
    sys.exit(main())
