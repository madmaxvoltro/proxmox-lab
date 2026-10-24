"""`lab setup`: one guided pass from a fresh Proxmox host to a working lab.

Safe to run again at any time: every step first looks at what already exists and skips what is done.
Read-only until the first confirmation; it never formats, partitions or deletes anything."""

import json
import os
import sys

from . import config as config_mod
from . import doctor as doctor_mod
from . import init as init_mod
from .errors import LabError, PreconditionError
from .prompt import Prompter
from .tailscale import Tailscale

TOTAL = 6
OAUTH_HELP = """\
Tailscale needs one credential so lab can enrol each new VM without you handling auth keys:

  1. Open  https://login.tailscale.com/admin/settings/oauth
     (newer consoles: Settings -> Trust credentials -> Credential -> OAuth)
  2. Scope: "Auth Keys" -> Write.   Tags: {tags}
  3. Generate it, then paste the Client ID and the Client secret below.
     The secret is shown only once. It is stored root-only at {path}."""


def _mark(ok: bool) -> str:
    utf = "utf" in (getattr(sys.stdout, "encoding", "") or "").lower()
    return ("✔" if utf else "+") if ok else ("✘" if utf else "x")


def step(ui: Prompter, n: int, title: str) -> None:
    ui.info(f"\n[{n}/{TOTAL}] {title}")


def _is_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


def preflight(ctx, ui: Prompter) -> list:
    """Checks that nothing else can work without. Returns a list of blocking problems."""
    problems = []

    def check(ok: bool, label: str, fix: str = "") -> None:
        ui.info(f"  {_mark(ok)} {label}")
        if not ok:
            problems.append(f"{label}: {fix}" if fix else label)

    check(sys.version_info >= (3, 11), f"Python {sys.version_info.major}.{sys.version_info.minor}",
          "Python 3.11 or newer is required (Proxmox VE 9 ships 3.13)")
    check(_is_root(), "running as root", "run `lab setup` as root on the Proxmox host")
    try:
        ver = ctx.px.version()
        check(int(ver.split(".")[0]) >= 8, f"Proxmox VE {ver}", "Proxmox VE 8 or newer is needed")
    except (LabError, ValueError) as e:
        check(False, "Proxmox VE", f"not detected ({e}); this command runs on the Proxmox host")
    ts = ctx.runner.run(["tailscale", "status"], check=False)
    check(ts.ok, "Tailscale is installed and connected",
          "install it (https://tailscale.com/download) and run `tailscale up --ssh`")
    return problems


def credential_step(ctx, ui: Prompter, owner: str) -> bool:
    """Make sure a working OAuth credential is stored. Returns True if one is in place."""
    cfg, io = ctx.cfg, ctx.io
    path = cfg.tailscale.oauth_file
    ts = Tailscale(cfg, ctx.px, io)
    if io.exists(path):
        try:
            ts.verify_credentials(owner)
            ui.info(f"  {_mark(True)} the stored Tailscale credential works")
            return True
        except LabError as e:
            ui.info(f"  {_mark(False)} the stored credential does not work ({e}); let's replace it")
    tags = f"{cfg.tailscale.base_tag} and {cfg.tailscale.owner_tag_prefix}{owner}"
    ui.info(OAUTH_HELP.format(tags=tags, path=path))
    for attempt in range(3):
        if attempt == 0 and not ui.confirm("Ready to paste the credential?", True):
            ui.info("  skipped. Add it later and run `lab setup` again.")
            return False
        client_id = ui.ask("OAuth client ID")
        secret = ui.secret("OAuth client secret")
        io.makedirs(os.path.dirname(path) or ".", 0o755)
        io.write_text(path, json.dumps({"client_id": client_id, "client_secret": secret}) + "\n", 0o600)
        try:
            ts.verify_credentials(owner)
            ui.info(f"  {_mark(True)} Tailscale accepted it (a test key with the right tags was minted)")
            return True
        except LabError as e:
            io.remove(path)
            ui.info(f"  {_mark(False)} Tailscale rejected it: {e}")
            ui.info("    Check the scope is Auth Keys: Write, the tags exist in your policy, and the secret is complete.")
    raise PreconditionError("could not validate the Tailscale credential after 3 tries; run `lab setup` again")


def run(ctx, ui: Prompter, *, skip_template: bool = False, config_file: str | None = None) -> int:
    ui.info("lab setup: this takes a fresh Proxmox host to a working lab.\n"
            "Re-running it is safe; finished steps are detected and skipped.")

    step(ui, 1, "Checking the host")
    problems = preflight(ctx, ui)
    if problems:
        ui.info("\nFix these first, then run `lab setup` again:")
        for p in problems:
            ui.info(f"  - {p}")
        return 3

    step(ui, 2, "Storage, network and your admin account")
    cfg_file = config_file or config_mod.config_path()
    res = init_mod.run_init(ctx.px, ctx.io, ctx.runner, ui, existing=ctx.cfg, config_file=cfg_file,
                            db=None if ctx.dry_run else ctx.db(create=True), env=dict(os.environ), write=True)
    ctx.cfg = res.config
    for n in res.notes:
        ui.info(f"  note: {n}")

    step(ui, 3, "Tailscale policy")
    ui.info("Paste this into https://login.tailscale.com/admin/acls (merge it into your existing sections):\n")
    ui.info(res.acl_snippet)
    if not ui.confirm("Have you saved the policy?", True):
        ui.info("  not yet: the next steps need the tags to exist. Run `lab setup` again when you have.")
        return 0

    step(ui, 4, "Tailscale credential")
    have_credential = credential_step(ctx, ui, res.admin_name)

    step(ui, 5, f"Base image ({res.config.vm.default_distro})")
    template_ok = False
    if skip_template:
        ui.info("  skipped (--skip-template). The first `lab create` builds it for you.")
    elif not have_credential:
        ui.info("  waiting for the Tailscale credential; run `lab setup` again after adding it.")
    else:
        tb = ctx.templates()
        if tb.find(res.config.vm.default_distro):
            ui.info(f"  {_mark(True)} the template already exists")
            template_ok = True
        elif ui.confirm("Build it now? (about 5 minutes: downloads the image, installs tools, checks them)", True):
            tb.build(res.config.vm.default_distro)
            ctx.rep.finish(True)
            template_ok = True
        else:
            ui.info("  skipped. `lab template add` or the first `lab create` builds it.")

    step(ui, 6, "Final check")
    checks = doctor_mod.run_checks(ctx.cfg, ctx.px, ctx.io, ctx.db(), ctx.templates(lambda m: None))
    ui.info(doctor_mod.render(checks))
    failed = doctor_mod.summarize(checks)
    ui.info("")
    if failed:
        ui.info("Some checks failed: fix them and run `lab doctor` (or `lab setup`) again.")
        return failed
    if template_ok:
        ui.info("All set. Try it:   lab create hello && lab exec hello -- uname -a")
    ui.info("Connect your PC and Claude:  see 'Windows' in the README (one command: irm .../install.ps1 | iex)")
    return 0
