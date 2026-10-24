"""Golden template builder (SPEC section 6).

Flow: verified cloud image -> VM with a build-time cloud-init user-data snippet ->
wait for the guest agent -> verify the toolchain -> generalise -> shut down -> template.
A failed build destroys only the build VM this run created."""

import json
import logging
import time
from datetime import datetime, timezone

from . import distros, netutil, progress
from .config import Config
from .errors import LabError, PreconditionError, VerificationError
from .hostio import HostIO
from .proxmox import Proxmox, ProxmoxError
from .runner import DryRunRunner
from .units import to_gib

log = logging.getLogger("lab.template")

TAG = "lab-template"
BUILD_SUFFIX = "-building"

# Run in the guest after cloud-init finishes; every tool must answer or the build fails.
VERIFY_CMD = (
    "git --version && curl --version >/dev/null && gcc --version >/dev/null && "
    "python3 --version && node --version && npm --version && tailscale version >/dev/null && "
    "(systemctl is-enabled ssh || systemctl is-enabled sshd) && systemctl is-active qemu-guest-agent"
)
# Remove everything instance-specific so each clone gets its own identity.
GENERALISE_CMD = (
    "cloud-init clean --logs --machine-id; rm -f /etc/ssh/ssh_host_*; "
    "rm -f /var/lib/tailscale/tailscaled.state; : > /etc/machine-id; "
    "(apt-get clean || dnf clean all) >/dev/null 2>&1; sync"
)


class TemplateError(LabError):
    pass


def build_user_data(d: distros.Distro) -> str:
    ssh_service = "ssh" if d.family == "apt" else "sshd"
    runcmd = [
        ["sh", "-c", "curl -fsSL https://tailscale.com/install.sh | sh"],  # installed, never authenticated
        ["systemctl", "enable", "--now", "qemu-guest-agent"],
        ["systemctl", "enable", "--now", ssh_service],
    ]
    lines = [
        "#cloud-config",
        "package_update: true",
        f"packages: {json.dumps(list(d.packages))}",
        "runcmd:",
        *[f"  - {json.dumps(c)}" for c in runcmd],
    ]
    return "\n".join(lines) + "\n"


def describe(distro: distros.Distro, image_hash: str, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    # One line, no colons: `qm config` prints it as "description: <text>" and lab splits on the first colon.
    return " ".join([
        "lab-template", f"distro={distro.id}", f"built={now.strftime('%Y%m%dT%H%M%SZ')}",
        f"image={distro.image_file}", f"{distro.checksum_type}={image_hash}",
    ])


def parse_description(text: str) -> dict:
    out = {}
    for word in (text or "").split():
        if "=" in word:
            k, v = word.split("=", 1)
            out[k] = v
    return out


class TemplateBuilder:
    def __init__(self, cfg: Config, px: Proxmox, io: HostIO, db=None, *, catalogue=None,
                 sleep=time.sleep, monotonic=time.monotonic, progress=lambda msg: None,
                 agent_timeout: float = 900, poll: float = 5):
        self.cfg, self.px, self.io, self.db = cfg, px, io, db
        self.cat = catalogue if catalogue is not None else distros.load()
        self.sleep, self.monotonic, self.say = sleep, monotonic, progress
        self.agent_timeout, self.poll = agent_timeout, poll
        self.cloudinit_timeout = 900
        self.dry_run = isinstance(px.r, DryRunRunner)

    # lookup ----------------------------------------------------------------
    def find(self, distro_id: str) -> dict | None:
        d = distros.get(distro_id, self.cat)
        lo, hi = self.cfg.vm.template_range
        for v in self.px.vms():
            if v.get("template") and v.get("name") == d.template_name and lo <= int(v["vmid"]) <= hi:
                return v
        return None

    def list(self) -> list:
        """One row per known distro: whether its template exists and when it was built."""
        rows = []
        for did in sorted(self.cat):
            v = self.find(did)
            meta = parse_description(self.px.config(int(v["vmid"])).get("description", "")) if v else {}
            rows.append({"distro": did, "vmid": int(v["vmid"]) if v else None,
                         "built": meta.get("built"), "present": v is not None})
        return rows

    def default_template(self) -> int:
        """VMID of the default distro's template, building nothing. Raises if missing."""
        v = self.find(self.cfg.vm.default_distro)
        if v is None:
            raise PreconditionError(f"no template for {self.cfg.vm.default_distro}; run `lab template add`")
        return int(v["vmid"])

    # image -----------------------------------------------------------------
    def fetch_image(self, d: distros.Distro) -> tuple:
        """Download (or reuse) the cloud image and verify it against the published checksum."""
        self.say("Fetching the published checksum")
        progress.detail(self.say, d.checksum_url)
        expected = distros.find_checksum(self.io.fetch_text(d.checksum_url), d.checksum_name, d.checksum_type)
        path = f"{self.cfg.paths.image_dir}/{d.image_file}"
        self.say("Getting the cloud image")
        if self.io.exists(path):
            progress.detail(self.say, f"checking the cached copy: {path}")
            if self.io.sha256(path, d.checksum_type, progress.bytes_cb(self.say, "hashing")) == expected:
                progress.detail(self.say, "cached copy is intact; no download needed")
                return path, expected
        progress.detail(self.say, f"downloading {d.image_url}")
        self.io.fetch_file(d.image_url, path, on_progress=progress.bytes_cb(self.say, "downloaded"))
        if not self.io.exists(path):  # dry-run
            return path, expected
        progress.detail(self.say, f"verifying the {d.checksum_type}")
        actual = self.io.sha256(path, d.checksum_type, progress.bytes_cb(self.say, "hashing"))
        if actual != expected:
            self.io.remove(path)
            raise VerificationError(
                f"{d.checksum_type} mismatch for {d.image_url}: expected {expected}, got {actual}; image deleted")
        return path, expected

    # build -----------------------------------------------------------------
    def build(self, distro_id: str, *, replace: bool = False) -> int:
        cfg = self.cfg
        cfg.require_storage()
        if not cfg.storage.snippet_disk:
            raise PreconditionError("config.storage.snippet_disk unset (a storage with 'snippets' content); run `lab init`")
        d = distros.get(distro_id, self.cat)
        old = self.find(distro_id)
        if old and not replace:
            raise PreconditionError(f"template for {distro_id} already exists (VMID {old['vmid']}); use `lab template update`")

        progress.plan(self.say, 11)
        image, image_hash = self.fetch_image(d)
        vmid = self.px.nextid(*cfg.vm.template_range)
        build_name = d.template_name + BUILD_SUFFIX
        snippet_id = f"lab-build-{d.id}.yaml"
        snippet_volid = f"{cfg.storage.snippet_disk}:snippets/{snippet_id}"
        snippet_path = self.px.storage_path(snippet_volid)
        self.say(f"Creating the build VM (VMID {vmid})")
        created = False
        try:
            self.io.write_text(snippet_path, build_user_data(d))
            fast = cfg.storage.fast_disk
            self.px.create(
                vmid, name=build_name, ostype="l26", memory=2048, cores=2, cpu="host",
                net0=f"virtio,bridge={cfg.network.bridge}", scsihw="virtio-scsi-pci",
                agent="enabled=1", serial0="socket", vga="serial0", tags=TAG,
            )
            created = True
            progress.detail(self.say, f"{build_name} on bridge {cfg.network.bridge}")
            self.say(f"Importing the image into {fast}")
            self.px.set(vmid, scsi0=f"{fast}:0,import-from={image},discard=on,ssd=1",
                        on_line=progress.line_cb(self.say))
            self.say("Configuring cloud-init, network and disk size")
            build_ip = netutil.allocate(cfg.network, self.db.used_ips() if self.db else set(), self.px.r)
            if build_ip:
                progress.detail(self.say, f"build VM address: {build_ip}")
            self.px.set(vmid, ide2=f"{fast}:cloudinit", boot="order=scsi0", **netutil.ipconfig(cfg.network, build_ip),
                        ciuser=d.default_user, cicustom=f"user={snippet_volid}")
            self.px.resize(vmid, "scsi0", f"{to_gib(cfg.vm.default_disk)}G")
            self.say("Booting the build VM")
            self.px.start(vmid)
            self._wait_agent(vmid)
            self.say("Installing packages (cloud-init first boot)")
            self._wait_cloud_init(vmid)
            self.say("Checking the toolchain")
            self._guest(vmid, ["sh", "-c", VERIFY_CMD], "toolchain verification failed", 60)
            self.say("Generalising the image (host keys, machine-id, Tailscale state)")
            self._guest(vmid, ["sh", "-c", GENERALISE_CMD], "generalising the image failed", 120)
            self.say("Shutting down")
            self.px.shutdown(vmid)
            if not self.dry_run and self.px.status(vmid) != "stopped":
                raise TemplateError(f"VM {vmid} did not shut down; leaving it for inspection")
            self.say("Converting to a template")
            # the build VM's address and user-data must not leak into clones (create sets its own)
            self.px.set(vmid, delete="cicustom,ipconfig0,nameserver", name=d.template_name,
                        description=describe(d, image_hash))
            self.px.to_template(vmid)
        except BaseException:
            if created:
                self._discard_build(vmid, build_name)
            self.io.remove(snippet_path)
            raise
        self.io.remove(snippet_path)
        progress.detail(self.say, f"{d.template_name} is ready (VMID {vmid})")
        return vmid

    def _wait_agent(self, vmid: int) -> None:
        try:
            self.px.wait_agent(vmid, self.agent_timeout, self.poll, sleep=self.sleep, monotonic=self.monotonic,
                               on_wait=progress.wait_cb(self.say))
        except ProxmoxError as e:
            raise TemplateError(str(e)) from e

    def _wait_cloud_init(self, vmid: int) -> None:
        try:
            self.px.wait_cloud_init(vmid, self.cloudinit_timeout, self.poll, sleep=self.sleep,
                                    monotonic=self.monotonic, on_wait=progress.wait_cb(self.say),
                                    on_detail=lambda text, key: progress.detail(self.say, text, key))
        except ProxmoxError as e:
            raise TemplateError(f"cloud-init did not finish cleanly: {e}") from e

    def _guest(self, vmid: int, argv, failure: str, timeout: int) -> dict:
        res = self.px.guest_exec(vmid, argv, check=False, timeout=timeout)
        if res.get("exitcode", 1) != 0:
            tail = (res.get("err-data") or res.get("out-data") or "").strip()[-500:]
            raise TemplateError(f"{failure}: {tail}")
        return res

    def _discard_build(self, vmid: int, expected_name: str) -> None:
        """Destroy the build VM, but only if it is still the VM this run created."""
        try:
            vm = self.px.vm(vmid)
            if vm is None or vm.get("name") != expected_name or vm.get("template"):
                return
            if vm.get("status") == "running":
                self.px.stop(vmid)
            self.px.destroy(vmid)
        except LabError as e:
            log.warning("could not clean up build VM %s: %s", vmid, e)

    # update / remove ---------------------------------------------------------
    def _dependents(self, vmid: int) -> list:
        if self.db is None:
            return []
        return [p["name"] for p in self.db.list_projects()
                if p["template_id"] == vmid and p["clone_mode"] == "linked"]

    def update(self, distro_id: str) -> int:
        """Build a fresh template, then retire the old one. Linked clones block the swap."""
        old = self.find(distro_id)
        if old is None:
            raise PreconditionError(f"no template for {distro_id}; use `lab template add`")
        deps = self._dependents(int(old["vmid"]))
        if deps:
            raise PreconditionError(f"linked clones depend on the current template: {', '.join(deps)}")
        old_id = int(old["vmid"])
        d = distros.get(distro_id, self.cat)
        self.px.set(old_id, name=d.template_name + "-old")  # frees the name for the new template
        try:
            new = self.build(distro_id, replace=True)
        except BaseException:
            self.px.set(old_id, name=d.template_name)  # build failed: the old template stays in service
            raise
        self.px.destroy(old_id)
        return new

    def remove(self, distro_id: str) -> None:
        old = self.find(distro_id)
        if old is None:
            raise PreconditionError(f"no template for {distro_id}")
        deps = self._dependents(int(old["vmid"]))
        if deps:
            raise PreconditionError(f"linked clones depend on this template: {', '.join(deps)}")
        self.px.destroy(int(old["vmid"]))
