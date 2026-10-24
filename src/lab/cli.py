"""Command-line entry point. M0 exposes only read-only plumbing: version, config, discover."""

import argparse
import json
import logging
import sys

from . import __version__, config as config_mod
from .errors import LabError
from .proxmox import Proxmox
from .runner import DryRunRunner, SubprocessRunner


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
    return p


def _emit(args, data, text):
    print(json.dumps(data, indent=2, default=str) if args.json else text)


def make_runner(args):
    runner = SubprocessRunner()
    return DryRunRunner(runner) if args.dry_run else runner


def cmd_discover(args, cfg):
    px = Proxmox(make_runner(args))
    vms = px.vms()
    storages = px.storages()
    data = {
        "pve_version": px.version(),
        "storages": [
            {"id": s["storage"], "type": s.get("plugintype"), "content": s.get("content"),
             "total": s.get("maxdisk"), "used": s.get("disk")}
            for s in storages
        ],
        "vms": [{"vmid": v["vmid"], "name": v.get("name"), "status": v.get("status"),
                 "template": bool(v.get("template"))} for v in vms],
    }
    lines = [f"Proxmox VE {data['pve_version']}", "", "Storages:"]
    lines += [f"  {s['id']:<16} {s['type'] or '?':<10} {s['content'] or ''}" for s in data["storages"]]
    lines += ["", "VMs:"]
    lines += [f"  {v['vmid']:>6} {v['name'] or '':<24} {v['status']}{' (template)' if v['template'] else ''}"
              for v in data["vms"]]
    _emit(args, data, "\n".join(lines))


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(message)s")
    try:
        cfg = config_mod.load(args.config)
        if args.cmd == "version":
            _emit(args, {"version": __version__}, f"lab {__version__}")
        elif args.cmd == "config":
            _emit(args, cfg.to_dict(), json.dumps(cfg.to_dict(), indent=2, default=list))
        elif args.cmd == "discover":
            cmd_discover(args, cfg)
        return 0
    except LabError as e:
        print(f"error: {e}", file=sys.stderr)
        return e.exit_code


if __name__ == "__main__":
    sys.exit(main())
