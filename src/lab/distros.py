"""Distro catalogue (data/distros.toml, plus an optional admin override file)."""

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .errors import UsageError

BUNDLED = Path(__file__).parent / "data" / "distros.toml"
OVERRIDE = "/etc/project-lab/distros.toml"
_ALGOS = ("sha256", "sha512")
_FAMILIES = ("apt", "dnf")


@dataclass(frozen=True)
class Distro:
    id: str
    family: str
    image_url: str
    checksum_url: str
    checksum_type: str
    default_user: str
    checksum_name: str
    packages: tuple

    @property
    def image_file(self) -> str:
        return f"{self.id}-{self.image_url.rsplit('/', 1)[-1]}"

    @property
    def template_name(self) -> str:
        return f"lab-tpl-{self.id}"


def _read(path) -> dict:
    with open(path, "rb") as fh:
        return tomllib.load(fh)


def load(override: str | None = OVERRIDE) -> dict:
    """Return {id: Distro}. Override entries replace bundled ones with the same id."""
    data = _read(BUNDLED)
    if override and Path(override).exists():
        extra = _read(override)
        data["distro"] = {**data.get("distro", {}), **extra.get("distro", {})}
        data["base_packages"] = {**data.get("base_packages", {}), **extra.get("base_packages", {})}
    out = {}
    for did, d in data.get("distro", {}).items():
        missing = {"family", "image_url", "checksum_url", "default_user"} - set(d)
        if missing:
            raise UsageError(f"distro {did}: missing {sorted(missing)}")
        if d["family"] not in _FAMILIES:
            raise UsageError(f"distro {did}: family must be one of {_FAMILIES}")
        algo = d.get("checksum_type", "sha256")
        if algo not in _ALGOS:
            raise UsageError(f"distro {did}: checksum_type must be one of {_ALGOS}")
        out[did] = Distro(
            id=did, family=d["family"], image_url=d["image_url"], checksum_url=d["checksum_url"],
            checksum_type=algo, default_user=d["default_user"],
            checksum_name=d.get("checksum_name") or d["image_url"].rsplit("/", 1)[-1],
            packages=tuple(data.get("base_packages", {}).get(d["family"], [])),
        )
    return out


def get(distro_id: str, catalogue: dict | None = None) -> Distro:
    cat = catalogue if catalogue is not None else load()
    if distro_id not in cat:
        raise UsageError(f"unknown distro {distro_id!r}; available: {', '.join(sorted(cat))}")
    return cat[distro_id]


def find_checksum(text: str, name: str, algo: str = "sha256") -> str:
    """Pull the hex digest for `name` out of a published checksum file.

    Handles GNU style ('<hex>  name' / '<hex> *name') and BSD style
    ('SHA256 (name) = <hex>'), which Rocky/Alma use."""
    want = {"sha256": 64, "sha512": 128}[algo]
    for line in text.splitlines():
        line = line.strip()
        m = re.fullmatch(r"([0-9a-fA-F]+)\s+\*?(\S+)", line) or None
        if m and m.group(2) == name and len(m.group(1)) == want:
            return m.group(1).lower()
        m = re.fullmatch(r"SHA(?:256|512)\s*\((.+)\)\s*=\s*([0-9a-fA-F]+)", line)
        if m and m.group(1) == name and len(m.group(2)) == want:
            return m.group(2).lower()
    raise UsageError(f"no {algo} entry for {name!r} in checksum file")
