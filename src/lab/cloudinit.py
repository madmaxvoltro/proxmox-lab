"""Per-VM cloud-init user-data."""

from .units import hostname


def user_data(name: str, owner: str) -> str:
    """Hostname and the owner as the only login user (no password, no keys: access is Tailscale
    SSH or the guest agent). Holds no secrets, so it may stay on disk, and it is deterministic,
    so restore can regenerate it."""
    return "\n".join([
        "#cloud-config",
        f"hostname: {hostname(name)}",
        "manage_etc_hosts: true",
        "users:",
        f"  - name: {owner}",
        "    shell: /bin/bash",
        "    lock_passwd: true",
        "    sudo: ALL=(ALL) NOPASSWD:ALL",
        "",
    ])
