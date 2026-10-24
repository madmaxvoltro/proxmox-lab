"""Addressing for project VMs: DHCP or lab-assigned static addresses."""

import ipaddress
import re

from .config import Network
from .errors import PreconditionError


def ipconfig(net: Network, ip: str | None = None) -> dict:
    """`qm set` options that give a VM its address (cloud-init)."""
    if net.mode == "static":
        prefix = ipaddress.ip_network(net.subnet, strict=False).prefixlen
        return {"ipconfig0": f"ip={ip}/{prefix},gw={net.gateway}", "nameserver": net.dns}
    return {"ipconfig0": "ip=dhcp"}


def net0(net: Network, current: str | None = None) -> str:
    """The NIC definition for a project VM: keeps the model and MAC address of `current` (what clone
    or restore produced) and forces the configured bridge and VLAN tag. Without `current`, a fresh NIC."""
    parts = [p for p in (current or "virtio").split(",") if p and not p.startswith(("bridge=", "tag="))]
    parts.append(f"bridge={net.bridge}")
    if net.vlan:
        parts.append(f"tag={net.vlan}")
    return ",".join(parts)


def host_vlan(runner, bridge: str, subnet: str) -> int:
    """VLAN id when the host has an interface like `vmbr1.60` holding an address in `subnet`, else 0."""
    res = runner.run(["ip", "-4", "-o", "addr", "show"], check=False)
    try:
        want = ipaddress.ip_network(subnet, strict=False)
    except ValueError:
        return 0
    for m in re.finditer(r"^\d+:\s+(\S+)\s+inet (\S+)", res.stdout or "", re.M):
        iface, addr = m.groups()
        vm = re.fullmatch(re.escape(bridge) + r"\.(\d+)", iface)
        if vm:
            try:
                if ipaddress.ip_interface(addr).network == want:
                    return int(vm.group(1))
            except ValueError:
                continue
    return 0


def pool(net: Network):
    lo, hi = int(ipaddress.ip_address(net.pool_start)), int(ipaddress.ip_address(net.pool_end))
    return [str(ipaddress.ip_address(i)) for i in range(lo, hi + 1)]


def in_use(runner, ip: str) -> bool:
    """True if something already answers at `ip` (read-only ping from the host)."""
    return runner.run(["ping", "-c", "1", "-W", "1", ip], check=False).ok


def allocate(net: Network, used: set, runner) -> str | None:
    """First pool address that lab has not handed out and nothing answers at. None for DHCP."""
    if net.mode != "static":
        return None
    for ip in pool(net):
        if ip not in used and not in_use(runner, ip):
            return ip
    raise PreconditionError(
        f"no free address left in {net.pool_start}-{net.pool_end} ({len(used)} assigned by lab); "
        "widen the pool in the host config or delete unused projects")


def usage(net: Network, used: set) -> tuple:
    """(addresses assigned by lab, pool size) for doctor."""
    size = len(pool(net)) if net.mode == "static" else 0
    return len(used & set(pool(net))) if size else 0, size


def host_defaults(runner, bridge: str) -> dict:
    """Suggested subnet/gateway for `lab init`, from the host's own address on the bridge."""
    out = {}
    res = runner.run(["ip", "-4", "-o", "addr", "show", "dev", bridge], check=False)
    m = re.search(r"inet (\S+)", res.stdout or "")
    if m:
        try:
            out["subnet"] = str(ipaddress.ip_interface(m.group(1)).network)
        except ValueError:
            pass
    res = runner.run(["ip", "-4", "route", "show", "default"], check=False)
    m = re.search(r"default via (\S+) dev (\S+)", res.stdout or "")
    if m and m.group(2) == bridge:
        out["gateway"] = m.group(1)
    return out
