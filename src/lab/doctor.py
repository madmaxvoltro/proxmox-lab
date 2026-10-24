"""`lab doctor`: read-only health and drift checks (SPEC section 5). Never changes anything."""

from dataclasses import dataclass

from . import netutil
from .config import Config
from .errors import LabError
from .hostio import HostIO
from .proxmox import Proxmox

OK, WARN, FAIL = "ok", "warn", "fail"
GIB = 1 << 30
MIN_FREE_WARN = 50 * GIB
MIN_FREE_FAIL = 10 * GIB


@dataclass
class Check:
    name: str
    level: str
    detail: str = ""
    hint: str = ""


def _storage(storages, sid):
    return next((s for s in storages if s.get("storage") == sid), None)


def _has_tag(vm, tag) -> bool:
    return tag in (vm.get("tags") or "").replace(",", ";").split(";")


def run_checks(cfg: Config, px: Proxmox, io: HostIO, db=None, templates=None) -> list:
    out: list = []
    add = lambda *a, **k: out.append(Check(*a, **k))

    # Proxmox version ---------------------------------------------------------
    try:
        ver = px.version()
        major = int(ver.split(".")[0])
        add("proxmox version", OK if major >= 9 else WARN, ver,
            "" if major >= 9 else "lab is developed against Proxmox VE 9.x")
    except (LabError, ValueError) as e:
        add("proxmox version", FAIL, str(e))
        return out  # nothing else can work

    # config completeness -------------------------------------------------------
    unset = [n for n in ("fast_disk", "archive_disk", "snippet_disk") if not getattr(cfg.storage, n)]
    if not cfg.network.bridge:
        unset.append("bridge")
    add("config complete", FAIL if unset else OK, f"unset: {', '.join(unset)}" if unset else "",
        "run `lab init`" if unset else "")

    storages = px.storages()
    vms = px.vms()

    # storages ------------------------------------------------------------------
    def storage_check(label, sid, content, *, space=True):
        if not sid:
            return
        s = _storage(storages, sid)
        if s is None:
            add(f"{label} storage", FAIL, f"{sid!r} not found in Proxmox", "run `lab init` to choose another")
            return
        if content not in (s.get("content") or "").split(","):
            add(f"{label} storage", FAIL, f"{sid!r} has no '{content}' content type",
                f"enable '{content}' on the storage in Proxmox, or choose another in `lab init`")
            return
        free = int(s.get("maxdisk") or 0) - int(s.get("disk") or 0)
        if space and free < MIN_FREE_FAIL:
            add(f"{label} storage", FAIL, f"{sid}: only {free // GIB} GiB free")
        elif space and free < MIN_FREE_WARN:
            add(f"{label} storage", WARN, f"{sid}: {free // GIB} GiB free")
        else:
            add(f"{label} storage", OK, f"{sid}: {free // GIB} GiB free" if space else sid)

    storage_check("fast", cfg.storage.fast_disk, "images")
    storage_check("archive", cfg.storage.archive_disk, "backup")
    storage_check("snippet", cfg.storage.snippet_disk, "snippets", space=False)
    if cfg.storage.fast_disk and cfg.storage.fast_disk == cfg.storage.archive_disk:
        add("archive on separate disk", WARN, "fast and archive disk are the same storage",
            "archives on the active disk do not free space")

    # archive disk actually mounted ---------------------------------------------
    if cfg.storage.archive_disk and _storage(storages, cfg.storage.archive_disk):
        try:
            sc = px.storage_config(cfg.storage.archive_disk)
            path = sc.get("path")
            if sc.get("type") == "dir" and path and io.exists(path):
                if io.same_device_as_root(path):
                    add("archive disk mounted", FAIL, f"{path} is on the root filesystem, not a separate disk",
                        "mount the archive disk before archiving; otherwise backups fill the system disk")
                else:
                    add("archive disk mounted", OK, path)
        except LabError as e:
            add("archive disk mounted", WARN, str(e))

    # network ---------------------------------------------------------------------
    if cfg.network.bridge:
        bridges = px.bridges()
        add("bridge", OK if cfg.network.bridge in bridges else FAIL,
            cfg.network.bridge if cfg.network.bridge in bridges else
            f"{cfg.network.bridge!r} not found (have: {', '.join(bridges) or 'none'})")

    if cfg.network.bridge:
        if cfg.network.mode == "dhcp":
            found = io.probe_dhcp(cfg.network.bridge)
            if found is False:
                add("addressing", WARN, f"no DHCP server answered on {cfg.network.bridge}",
                    "if VMs on this bridge need fixed addresses, run `lab init` and choose static")
            else:
                add("addressing", OK, "dhcp" + ("" if found else " (could not probe)"))
        else:
            used = db.used_ips() if db is not None else set()
            n, size = netutil.usage(cfg.network, used)
            level = FAIL if size and n >= size else WARN if size and n >= 0.9 * size else OK
            add("addressing", level, f"static, {n}/{size} pool addresses assigned "
                f"({cfg.network.pool_start}-{cfg.network.pool_end})",
                "widen the pool in the host config" if level != OK else "")

    # templates ---------------------------------------------------------------------
    if templates is not None:
        have = [t for t in templates.list() if t["present"]]
        wanted = cfg.vm.default_distro
        if any(t["distro"] == wanted for t in have):
            add("default template", OK, wanted)
        else:
            add("default template", WARN, f"no template for {wanted}", f"`lab template add {wanted}` (or created on first use)")

    # drift between index, Proxmox and lab's VMID range ---------------------------------
    lo, hi = cfg.vm.vmid_range
    by_id = {int(v["vmid"]): v for v in vms}
    indexed = {}
    if db is not None:
        projects = db.list_projects()
        indexed = {p["vmid"]: p for p in projects if p["vmid"] is not None}
        gone = [p["name"] for p in projects
                if p["vmid"] is not None and p["state"] != "archived" and int(p["vmid"]) not in by_id]
        add("index vs proxmox", FAIL if gone else OK,
            f"indexed but missing in Proxmox: {', '.join(gone)}" if gone else f"{len(projects)} project(s) consistent",
            "restore from archive or `lab doctor --rebuild-index`" if gone else "")

        # archived projects must still have their backup
        if cfg.storage.archive_disk and _storage(storages, cfg.storage.archive_disk):
            present = {b["volid"] for b in px.backups(cfg.storage.archive_disk)}
            lost = [f"{a['project']} ({a['volid']})" for a in db.list_archives() if a["volid"] not in present]
            add("archives present", FAIL if lost else OK,
                f"backup missing on archive disk: {', '.join(lost)}" if lost else f"{len(db.list_archives())} archive(s) found")

        open_ops = db.open_journal()
        add("interrupted operations", WARN if open_ops else OK,
            ", ".join(f"{j['op']} {j['project']} at {j['step'] or 'start'}" for j in open_ops),
            "an archive/restore was interrupted; the original VM was not removed" if open_ops else "")

    orphans = [v for v in vms if _has_tag(v, "lab") and int(v["vmid"]) not in indexed and db is not None]
    foreign = [v for v in vms if lo <= int(v["vmid"]) <= hi and not _has_tag(v, "lab")
               and int(v["vmid"]) not in indexed]
    add("untracked lab VMs", WARN if orphans else OK,
        ", ".join(f"{v['vmid']} {v.get('name')}" for v in orphans), "")
    add("vmid range clear", WARN if foreign else OK,
        "non-lab VMs inside lab's VMID range: " + ", ".join(f"{v['vmid']} {v.get('name')}" for v in foreign)
        if foreign else f"{lo}-{hi}",
        "lab never touches them, but consider moving the range" if foreign else "")

    leftovers = [v for v in vms if (v.get("name") or "").startswith("lab-verify-")
                 or (v.get("name") or "").endswith("-building")]
    add("leftover temp VMs", WARN if leftovers else OK,
        ", ".join(f"{v['vmid']} {v['name']}" for v in leftovers),
        "left behind by an interrupted archive verify or template build; safe to destroy after checking" if leftovers else "")

    # tailscale credential ------------------------------------------------------------
    p = cfg.tailscale.oauth_file
    if not io.exists(p):
        add("tailscale oauth credential", WARN, f"{p} not found", "create the OAuth client (see `lab init`)")
    else:
        mode = io.mode(p)
        bad = mode & 0o077
        add("tailscale oauth credential", FAIL if bad else OK,
            f"{p} is mode {mode:03o}" if bad else p, f"chmod 600 {p}" if bad else "")
    return out


def summarize(checks) -> int:
    """Exit code: 0 when nothing failed (warnings allowed), 5 when something needs attention."""
    return 5 if any(c.level == FAIL for c in checks) else 0


def render(checks) -> str:
    mark = {OK: "ok  ", WARN: "warn", FAIL: "FAIL"}
    lines = []
    for c in checks:
        lines.append(f"[{mark[c.level]}] {c.name}" + (f": {c.detail}" if c.detail else ""))
        if c.hint and c.level != OK:
            lines.append(f"       -> {c.hint}")
    return "\n".join(lines)
