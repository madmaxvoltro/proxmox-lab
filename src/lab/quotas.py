"""Per-user quotas (SPEC section 17). Admins are exempt. Disk counts every non-archived project;
vCPU, RAM and VM count only count projects that are running (or about to be)."""

import dataclasses

from .access import user_quota_overrides
from .config import Config
from .db import DB
from .errors import PreconditionError
from .units import parse_size

_RUNNING = ("running", "creating", "restoring")
_GIB = 1 << 30
_MIB = 1 << 20


def limits(cfg: Config, db: DB, owner: str) -> dict:
    base = dataclasses.asdict(cfg.quota)
    row = db.get_user_by_name(owner)
    base.update(user_quota_overrides(row) if row else {})
    return {
        "vms": int(base["max_running_vms"]), "cpu": int(base["max_cpu"]),
        "ram_mb": parse_size(base["max_ram"]) // _MIB, "disk_gb": parse_size(base["max_disk"]) // _GIB,
    }


def usage(db: DB, owner: str, exclude: str | None = None) -> dict:
    mine = [p for p in db.list_projects(owner) if p["state"] != "archived" and p["name"] != exclude]
    running = [p for p in mine if p["state"] in _RUNNING]
    return {
        "vms": len(running), "cpu": sum(p["cpu"] for p in running),
        "ram_mb": sum(p["ram_mb"] for p in running), "disk_gb": sum(p["disk_gb"] for p in mine),
    }


def check(cfg: Config, db: DB, owner: str, *, cpu: int, ram_mb: int, disk_gb: int,
          running: bool = True, exclude: str | None = None, is_admin: bool = False) -> None:
    """Raise if adding this allocation would exceed any of the owner's limits."""
    if is_admin:
        return
    lim, use = limits(cfg, db, owner), usage(db, owner, exclude)
    want = {"vms": 1 if running else 0, "cpu": cpu if running else 0,
            "ram_mb": ram_mb if running else 0, "disk_gb": disk_gb}
    labels = {"vms": ("running VMs", ""), "cpu": ("vCPUs", ""), "ram_mb": ("RAM", " MiB"),
              "disk_gb": ("fast-disk allocation", " GiB")}
    for key, (label, unit) in labels.items():
        if use[key] + want[key] > lim[key]:
            raise PreconditionError(
                f"quota exceeded for {owner}: {label} would be {use[key] + want[key]}{unit}, "
                f"limit {lim[key]}{unit} (currently {use[key]}{unit})")
