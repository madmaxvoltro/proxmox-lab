"""`lab init`: discover the host, let the admin pick disks, write the host config (SPEC section 5).

Read-only until the final confirmation. Never formats, partitions or wipes anything; the only
host changes are: (optionally) one new subfolder on an already-mounted disk, one
`pvesm add dir`, the config file and the lab database."""

import os
from dataclasses import dataclass, field

from . import config as config_mod
from . import netutil, tsacl
from .errors import PreconditionError, UsageError
from .hostio import HostIO
from .identity import caller_login
from .prompt import Prompter
from .proxmox import Proxmox
from .runner import Runner

NEW_DIR = "__new_directory_storage__"
ARCHIVE_SUBDIR = "project-lab-archive"


@dataclass
class InitResult:
    config: config_mod.Config
    admin_login: str
    admin_name: str
    acl_snippet: str
    notes: list = field(default_factory=list)


def _with_content(storages, content):
    return [s for s in storages if content in (s.get("content") or "").split(",")]


def _fmt(s) -> str:
    total = int(s.get("maxdisk") or 0) >> 30
    used = int(s.get("disk") or 0) >> 30
    return f"{s.get('plugintype', '?')}, {used}/{total} GiB used"


def _add_archive_storage(px: Proxmox, io: HostIO, ui: Prompter) -> str:
    """Register a dedicated subfolder of an already-mounted disk as a backup storage."""
    mount = ui.ask("Mount path of the archive disk (must already be mounted)")
    if not io.exists(mount) or not mount.startswith("/"):
        raise PreconditionError(f"{mount!r} is not an existing absolute path")
    if io.same_device_as_root(mount):
        raise PreconditionError(
            f"{mount} is on the root filesystem, not a separate disk. Mount the archive disk there first; "
            "lab will not write archives onto the system disk by accident."
        )
    existing = io.listdir(mount)
    if existing:
        ui.info(f"{mount} already contains {len(existing)} item(s), e.g. {', '.join(existing[:5])}.")
        ui.info(f"lab will only create and use the new subfolder '{ARCHIVE_SUBDIR}'; nothing else is touched.")
    folder = mount.rstrip("/") + "/" + ARCHIVE_SUBDIR
    sid = ui.ask("Storage ID for the new archive storage", "lab-archive")
    cmd = px.add_dir_command(sid, folder, "backup", mountpoint=mount)
    ui.info("About to run:\n  mkdir -p " + folder + "\n  " + " ".join(cmd))
    if not ui.confirm("Proceed?", False):
        raise PreconditionError("archive storage not created; run `lab init` again when ready")
    io.makedirs(folder)
    px.add_dir_storage(sid, folder, "backup", mountpoint=mount)
    return sid


def run_init(px: Proxmox, io: HostIO, runner: Runner, ui: Prompter, *, existing: config_mod.Config,
             config_file: str, db=None, env: dict | None = None, write: bool = True) -> InitResult:
    notes: list = []
    ver = px.version()
    ui.info(f"Proxmox VE {ver}")
    if int(ver.split(".")[0]) < 9:
        notes.append(f"developed for Proxmox VE 9.x; found {ver}")

    storages = px.storages()
    ui.info("Discovered storages:\n" + "\n".join(f"  {s['storage']:<16} {_fmt(s)}  [{s.get('content', '')}]" for s in storages))

    cfg = existing
    # fast disk -------------------------------------------------------------------
    fast = _with_content(storages, "images")
    if not fast:
        raise PreconditionError("no storage supports VM images; add one in Proxmox first")
    cfg.storage.fast_disk = ui.choose("Fast disk (active project VMs live here)",
                                      [(s["storage"], _fmt(s)) for s in fast],
                                      default=cfg.storage.fast_disk or None)

    # archive disk -------------------------------------------------------------------
    arch = [s for s in _with_content(storages, "backup") if s["storage"] != cfg.storage.fast_disk]
    options = [(s["storage"], _fmt(s)) for s in arch] + [(NEW_DIR, "register a dedicated folder on a mounted disk")]
    choice = ui.choose("Archive disk (verified backups of archived projects)", options,
                       default=cfg.storage.archive_disk or None)
    cfg.storage.archive_disk = _add_archive_storage(px, io, ui) if choice == NEW_DIR else choice
    if cfg.storage.archive_disk == cfg.storage.fast_disk:
        notes.append("fast and archive disk are the same storage; archiving will not free space")

    # snippets ---------------------------------------------------------------------------
    snip = _with_content(storages, "snippets")
    if not snip:
        raise PreconditionError(
            "no storage has 'snippets' content (needed for cloud-init user-data). In Proxmox: "
            "Datacenter > Storage > local > Content, enable 'Snippets', then rerun `lab init`")
    cfg.storage.snippet_disk = ui.choose("Snippet storage (cloud-init user-data files)",
                                         [(s["storage"], _fmt(s)) for s in snip],
                                         default=cfg.storage.snippet_disk or snip[0]["storage"])

    # bridge ---------------------------------------------------------------------------------
    bridges = px.bridges()
    if not bridges:
        raise PreconditionError("no network bridge found on the node")
    cfg.network.bridge = ui.choose("Network bridge for project VMs", [(b, "") for b in bridges],
                                   default=cfg.network.bridge or (bridges[0] if len(bridges) == 1 else None))

    # addressing -------------------------------------------------------------------------------
    net = cfg.network
    found = io.probe_dhcp(net.bridge)
    ui.info({True: f"A DHCP server answered on {net.bridge}.",
             False: f"No DHCP server answered on {net.bridge}: VMs there need fixed addresses.",
             None: f"Could not probe {net.bridge} for DHCP."}[found])
    net.mode = ui.choose(f"Address assignment on {net.bridge}",
                         [("dhcp", "the network hands out addresses"),
                          ("static", "lab assigns each VM an address from a pool you define")],
                         default={True: "dhcp", False: "static"}.get(found, net.mode))
    if net.mode == "static":
        guess = netutil.host_defaults(runner, net.bridge)
        net.subnet = ui.ask("Subnet (CIDR, e.g. 192.168.10.0/24)", net.subnet or guess.get("subnet"))
        net.gateway = ui.ask("Gateway address", net.gateway or guess.get("gateway"))
        net.dns = ui.ask("DNS servers (space separated)", net.dns or net.gateway)
        net.pool_start = ui.ask("First address lab may assign", net.pool_start or None)
        net.pool_end = ui.ask("Last address lab may assign", net.pool_end or None)
        ui.info("Keep that range free of anything else (reserve it in your router). lab also pings an "
                "address before using it.")
    net.validate()

    # vmid range -------------------------------------------------------------------------------
    lo, hi = cfg.vm.vmid_range
    foreign = [v for v in px.vms() if lo <= int(v["vmid"]) <= hi
               and "lab" not in (v.get("tags") or "").split(";")]
    if foreign:
        ui.info("Existing VMs inside the lab VMID range: " + ", ".join(f"{v['vmid']} {v.get('name')}" for v in foreign))
        if not ui.confirm(f"Use range {lo}-{hi} anyway? (lab never touches VMs it did not create)", False):
            lo = int(ui.ask("VMID range start"))
            hi = int(ui.ask("VMID range end"))
            cfg.vm.vmid_range = (lo, hi)
    cfg.validate()

    # first admin ----------------------------------------------------------------------------------
    login = caller_login(env if env is not None else dict(os.environ), runner)
    if login:
        ui.info(f"Detected your tailnet identity: {login}")
    else:
        login = ui.ask("Your tailnet login (e.g. you@example.com)")
    name = ui.ask("Your lab username (also your login user inside VMs)", login.split("@")[0].lower())
    from .units import validate_name
    validate_name(name)

    acl = tsacl.acl_snippet(login, {name: login}, cfg.tailscale.base_tag, cfg.tailscale.owner_tag_prefix)
    if not io.exists(cfg.tailscale.oauth_file):
        notes.append(f"Tailscale OAuth credential not found at {cfg.tailscale.oauth_file} (root-only, mode 600)")

    # write ----------------------------------------------------------------------------------------------
    if write:
        if not ui.confirm(f"Write {config_file} and create the lab database?", False):
            raise PreconditionError("nothing written")
        io.write_text(config_file, config_mod.dumps(cfg), 0o644)
        if db is not None and db.get_user(login) is None:
            db.add_user(login, name, "admin")
    return InitResult(cfg, login, name, acl, notes)
