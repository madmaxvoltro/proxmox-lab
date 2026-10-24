"""Host configuration (SPEC section 4). Storage IDs, bridge and addresses are never defaulted."""

import dataclasses
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .errors import UsageError
from .units import parse_size

DEFAULT_CONFIG_PATH = "/etc/project-lab/config.toml"
DEFAULT_STATE_DIR = "/var/lib/project-lab"


@dataclass
class Storage:
    fast_disk: str = ""
    archive_disk: str = ""


@dataclass
class Network:
    bridge: str = ""


@dataclass
class VM:
    vmid_range: tuple = (1000, 1999)
    template_id: int = 9000
    default_distro: str = "ubuntu-24.04"
    default_cores: int = 1
    default_memory: int = 2048  # MiB
    default_disk: str = "32G"
    clone_mode: str = "full"


@dataclass
class Archive:
    keep: int = 1


@dataclass
class Tailscale:
    base_tag: str = "tag:server"
    owner_tag_prefix: str = "tag:user-"
    oauth_file: str = "/etc/project-lab/ts-oauth"


@dataclass
class Idle:
    enabled: bool = False
    days: int = 7
    cpu_percent: float = 5.0


@dataclass
class MCP:
    max_cpu: int = 8
    max_ram: str = "16G"
    max_running_vms: int = 6


@dataclass
class Paths:
    state_dir: str = DEFAULT_STATE_DIR
    log_dir: str = "/var/log/project-lab"

    @property
    def db(self) -> str:
        return str(Path(self.state_dir) / "lab.db")


DEFAULT_PRESETS = {
    "small": {"cpu": 1, "ram": "2G", "disk": "32G"},
    "medium": {"cpu": 4, "ram": "8G", "disk": "48G"},
    "large": {"cpu": 8, "ram": "16G", "disk": "64G"},
}


@dataclass
class Config:
    storage: Storage = field(default_factory=Storage)
    network: Network = field(default_factory=Network)
    vm: VM = field(default_factory=VM)
    archive: Archive = field(default_factory=Archive)
    tailscale: Tailscale = field(default_factory=Tailscale)
    idle: Idle = field(default_factory=Idle)
    mcp: MCP = field(default_factory=MCP)
    paths: Paths = field(default_factory=Paths)
    presets: dict = field(default_factory=lambda: {k: dict(v) for k, v in DEFAULT_PRESETS.items()})

    def validate(self) -> "Config":
        lo, hi = self.vm.vmid_range
        if not (100 <= lo <= hi):
            raise UsageError(f"vm.vmid_range {self.vm.vmid_range} must satisfy 100 <= low <= high")
        if self.vm.clone_mode not in ("full", "linked"):
            raise UsageError("vm.clone_mode must be 'full' or 'linked'")
        if self.archive.keep < 1:
            raise UsageError("archive.keep must be >= 1")
        if self.vm.template_id and lo <= self.vm.template_id <= hi:
            raise UsageError("vm.template_id must be outside vm.vmid_range")
        parse_size(self.vm.default_disk)
        for name, p in self.presets.items():
            missing = {"cpu", "ram", "disk"} - set(p)
            if missing:
                raise UsageError(f"preset {name!r} is missing {sorted(missing)}")
            parse_size(p["ram"])
            parse_size(p["disk"])
        return self

    def require_storage(self) -> None:
        """Commands that touch disks call this; run `lab init` first if it fails."""
        missing = [n for n in ("fast_disk", "archive_disk") if not getattr(self.storage, n)]
        if missing or not self.network.bridge:
            what = missing + ([] if self.network.bridge else ["bridge"])
            raise UsageError(f"config incomplete ({', '.join(what)} unset); run `lab init`")

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


_SECTIONS = {
    "storage": Storage,
    "network": Network,
    "vm": VM,
    "archive": Archive,
    "tailscale": Tailscale,
    "idle": Idle,
    "mcp": MCP,
    "paths": Paths,
}


def _build(cls, data: dict, section: str):
    known = {f.name for f in dataclasses.fields(cls)}
    unknown = set(data) - known
    if unknown:
        raise UsageError(f"unknown key(s) in [{section}]: {', '.join(sorted(unknown))}")
    if section == "vm" and "vmid_range" in data:
        data = {**data, "vmid_range": tuple(data["vmid_range"])}
    return cls(**data)


def from_dict(data: dict) -> Config:
    unknown = set(data) - set(_SECTIONS) - {"presets"}
    if unknown:
        raise UsageError(f"unknown config section(s): {', '.join(sorted(unknown))}")
    kwargs = {name: _build(cls, data[name], name) for name, cls in _SECTIONS.items() if name in data}
    cfg = Config(**kwargs)
    if "presets" in data:
        cfg.presets = {**cfg.presets, **data["presets"]}
    return cfg.validate()


def config_path() -> str:
    return os.environ.get("LAB_CONFIG", DEFAULT_CONFIG_PATH)


def load(path: str | None = None) -> Config:
    """Load config; a missing file yields defaults (unset storage IDs), so `lab init` can run."""
    p = Path(path or config_path())
    if not p.exists():
        return Config().validate()
    with p.open("rb") as fh:
        return from_dict(tomllib.load(fh))
