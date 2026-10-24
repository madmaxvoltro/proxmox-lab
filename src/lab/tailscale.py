"""Tailscale enrolment for project VMs (SPEC section 9, decisions Q1 and Q10).

An OAuth client on the host mints a single-use, short-lived, ephemeral, pre-approved key per
enrolment, tagged with the base tag (tag:lab) + the owner's tag. The key reaches the guest on stdin via
the QEMU guest agent: never in argv, cloud-init, the VM disk, logs or backups."""

import json
from dataclasses import dataclass

from .config import Config
from .errors import LabError
from .hostio import HostIO
from .proxmox import Proxmox

TOKEN_URL = "https://api.tailscale.com/api/v2/oauth/token"
KEYS_URL = "https://api.tailscale.com/api/v2/tailnet/-/keys"
KEY_LIFETIME_S = 600


class TailscaleError(LabError):
    pass


@dataclass
class Enrolment:
    ip: str
    dns_name: str


def _redact(text: str, secret: str) -> str:
    return (text or "").replace(secret, "[redacted]")


class Tailscale:
    def __init__(self, cfg: Config, px: Proxmox, io: HostIO):
        self.cfg, self.px, self.io = cfg, px, io

    def tags_for(self, owner: str) -> list:
        return [self.cfg.tailscale.base_tag, self.cfg.tailscale.owner_tag_prefix + owner]

    def _credentials(self) -> dict:
        path = self.cfg.tailscale.oauth_file
        if not self.io.exists(path):
            raise TailscaleError(f"Tailscale OAuth credential not found at {path} (see `lab init`)")
        try:
            creds = json.loads(self.io.read_text(path))
            return {"client_id": creds["client_id"], "client_secret": creds["client_secret"]}
        except (ValueError, KeyError) as e:
            raise TailscaleError(f"{path} must be JSON with client_id and client_secret") from e

    def mint_key(self, tags: list, description: str = "lab") -> str:
        token = self.io.post(TOKEN_URL, form={**self._credentials(), "grant_type": "client_credentials"})
        access = token.get("access_token")
        if not access:
            raise TailscaleError("Tailscale rejected the OAuth credential (no access token returned)")
        body = {
            "capabilities": {"devices": {"create": {
                "reusable": False, "ephemeral": True, "preauthorized": True, "tags": tags}}},
            "expirySeconds": KEY_LIFETIME_S,
            "description": description,
        }
        reply = self.io.post(KEYS_URL, json_body=body, headers={"Authorization": f"Bearer {access}"})
        key = reply.get("key", "")
        if not key.startswith("tskey-"):
            raise TailscaleError("Tailscale did not return an auth key (check the OAuth client's auth_keys scope and tags)")
        return key

    def verify_credentials(self, owner: str) -> None:
        """Prove the OAuth client works end to end: it must be able to mint an auth key with exactly the
        tags lab uses. The key is single-use and expires in 10 minutes; it is never delivered anywhere."""
        self.mint_key(self.tags_for(owner), "lab setup check")

    def enroll(self, vmid: int, hostname: str, owner: str) -> Enrolment:
        key = self.mint_key(self.tags_for(owner), f"lab {hostname}")
        res = self.px.guest_exec(
            vmid, ["tailscale", "up", "--auth-key=file:/dev/stdin", "--ssh", f"--hostname={hostname}"],
            stdin=key, check=False, timeout=90)
        if res.get("exitcode", 1) != 0:
            raise TailscaleError("tailscale up failed in the guest: "
                                 + _redact((res.get("err-data") or res.get("out-data") or "").strip()[-300:], key))
        ip = (self._guest(vmid, ["tailscale", "ip", "-4"]).get("out-data") or "").strip().splitlines()
        if not ip:
            raise TailscaleError("enrolled, but the guest reported no tailnet IPv4 address")
        dns = hostname
        status = self._guest(vmid, ["tailscale", "status", "--json"]).get("out-data") or ""
        try:
            dns = json.loads(status)["Self"]["DNSName"].rstrip(".") or hostname
        except (ValueError, KeyError, TypeError):
            pass
        return Enrolment(ip[0].strip(), dns)

    def reset_identity(self, vmid: int) -> None:
        """Drop any Tailscale state carried in a restored disk so the VM enrols as a fresh device."""
        res = self.px.guest_exec(
            vmid, ["sh", "-c", "systemctl stop tailscaled; rm -f /var/lib/tailscale/tailscaled.state; "
                               "systemctl start tailscaled"], check=False, timeout=60)
        if res.get("exitcode", 1) != 0:
            raise TailscaleError("could not reset the Tailscale identity in the restored VM: "
                                 + (res.get("err-data") or "").strip()[-200:])

    def logout(self, vmid: int) -> bool:
        """Best effort: leave the tailnet so the device disappears. Returns whether the guest confirmed."""
        try:
            return self.px.guest_exec(vmid, ["tailscale", "logout"], check=False, timeout=30).get("exitcode") == 0
        except LabError:
            return False

    def _guest(self, vmid, argv) -> dict:
        res = self.px.guest_exec(vmid, argv, check=False, timeout=30)
        if res.get("exitcode", 1) != 0:
            raise TailscaleError(f"{' '.join(argv)} failed in the guest: {(res.get('err-data') or '').strip()[-200:]}")
        return res
