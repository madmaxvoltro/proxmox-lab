"""Host configuration (SPEC section 4). Storage IDs, bridge and addresses are never defaulted."""

import dataclasses
import ipaddress
import json
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
    snippet_disk: str = ""  # storage with 'snippets' content, used for cloud-init user-data


@dataclass
class Network:
    """How project VMs get an address on `bridge`. `dhcp`: the network hands them out.
    `static`: lab assigns each VM one address from [pool_start, pool_end] (checked free with a ping)."""
    bridge: str = ""
    vlan: int = 0           # VLAN tag for project VMs on a VLAN-aware bridge; 0 = untagged
    mode: str = "dhcp"      # dhcp | static
    subnet: str = ""        # static: CIDR, e.g. 192.168.10.0/24
    gateway: str = ""       # static
    dns: str = ""           # static: space-separated resolvers
    pool_start: str = ""    # static
    pool_end: str = ""      # static

    def validate(self) -> None:
        if not isinstance(self.vlan, int) or isinstance(self.vlan, bool) or not (self.vlan == 0 or 2 <= self.vlan <= 4094):
            raise UsageError("network.vlan must be 0 (untagged) or between 2 and 4094")
        if self.mode not in ("dhcp", "static"):
            raise UsageError("network.mode must be 'dhcp' or 'static'")
        if self.mode == "dhcp":
            return
        try:
            net = ipaddress.ip_network(self.subnet, strict=False)
            gw = ipaddress.ip_address(self.gateway)
            lo, hi = ipaddress.ip_address(self.pool_start), ipaddress.ip_address(self.pool_end)
            dns = [ipaddress.ip_address(d) for d in self.dns.split()]
        except ValueError as e:
            raise UsageError(f"network (static): {e}") from e
        if net.version != 4:
            raise UsageError("network (static): only IPv4 is supported")
        if not dns:
            raise UsageError("network (static): dns needs at least one resolver")
        if gw not in net or lo not in net or hi not in net:
            raise UsageError("network (static): gateway and pool must be inside the subnet")
        if lo > hi:
            raise UsageError("network (static): pool_start must not be after pool_end")
        if lo <= gw <= hi:
            raise UsageError("network (static): the gateway must not be inside the pool")
        if net.network_address in (lo, hi) or net.broadcast_address in (lo, hi):
            raise UsageError("network (static): the pool must not include the network or broadcast address")


@dataclass
class VM:
    vmid_range: tuple = (1000, 1999)
    template_range: tuple = (9000, 9099)  # golden templates, one per distro
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
    base_tag: str = "tag:lab"      # dedicated tag: gets none of your servers' tailnet permissions
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
    max_output_bytes: int = 65536      # per stream returned to the model
    max_file_bytes: int = 4 << 20      # file read/write through the guest agent
    approval_ttl_s: int = 600
    approval_wait_s: int = 50          # a gated call waits this long for a click when the desktop helper is running


@dataclass
class Quota:
    """Per-user defaults; users.quota_json can override any key. Admins are exempt."""
    max_running_vms: int = 4
    max_cpu: int = 8
    max_ram: str = "24G"
    max_disk: str = "256G"  # total fast-disk allocation across the user's non-archived projects


@dataclass
class Paths:
    state_dir: str = DEFAULT_STATE_DIR
    log_dir: str = "/var/log/project-lab"

    @property
    def db(self) -> str:
        return str(Path(self.state_dir) / "lab.db")

    @property
    def audit_log(self) -> str:
        return str(Path(self.log_dir) / "mcp-audit.log")

    @property
    def image_dir(self) -> str:
        return str(Path(self.state_dir) / "images")


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
    quota: Quota = field(default_factory=Quota)
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
        tlo, thi = self.vm.template_range
        if not (100 <= tlo <= thi):
            raise UsageError(f"vm.template_range {self.vm.template_range} must satisfy 100 <= low <= high")
        if lo <= thi and tlo <= hi:
            raise UsageError("vm.template_range must not overlap vm.vmid_range")
        self.network.validate()
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
    "quota": Quota,
    "paths": Paths,
}


def _build(cls, data: dict, section: str):
    known = {f.name for f in dataclasses.fields(cls)}
    unknown = set(data) - known
    if unknown:
        raise UsageError(f"unknown key(s) in [{section}]: {', '.join(sorted(unknown))}")
    if section == "vm":
        data = {k: tuple(v) if k.endswith("_range") else v for k, v in data.items()}
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


PROJECT_KEYS = frozenset({"distro", "cpu", "ram", "disk", "clone_mode", "profile"})


def project_request(data: dict) -> dict:
    """Validate settings that arrive with a project (repo lab.toml, profile, client, MCP).

    Only PROJECT_KEYS are accepted. Storage, bridge, paths and the like are decided by the
    host config alone (SPEC 4.1), so anything else is an error rather than silently ignored."""
    bad = sorted(set(data) - PROJECT_KEYS)
    if bad:
        raise UsageError(
            f"project settings may not set {', '.join(bad)}: disks, network and paths are "
            f"decided by the host config (allowed: {', '.join(sorted(PROJECT_KEYS))})"
        )
    if data.get("clone_mode", "full") not in ("full", "linked"):
        raise UsageError("clone_mode must be 'full' or 'linked'")
    for k in ("ram", "disk"):
        if k in data:
            parse_size(data[k])
    return dict(data)


def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{k} = {_toml_value(x)}" for k, x in v.items()) + " }"
    return json.dumps(str(v))  # JSON string escaping is valid TOML basic-string escaping


def dumps(cfg: Config) -> str:
    """Serialise to TOML (stdlib can only read it)."""
    out = []
    for section in _SECTIONS:
        out.append(f"[{section}]")
        out += [f"{k} = {_toml_value(v)}" for k, v in dataclasses.asdict(getattr(cfg, section)).items()]
        out.append("")
    out.append("[presets]")
    out += [f"{name} = {_toml_value(p)}" for name, p in cfg.presets.items()]
    return "\n".join(out) + "\n"


def config_path() -> str:
    return os.environ.get("LAB_CONFIG", DEFAULT_CONFIG_PATH)


def load(path: str | None = None) -> Config:
    """Load config; a missing file yields defaults (unset storage IDs), so `lab init` can run."""
    p = Path(path or config_path())
    if not p.exists():
        return Config().validate()
    with p.open("rb") as fh:
        return from_dict(tomllib.load(fh))
