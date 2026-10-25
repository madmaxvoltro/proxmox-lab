"""`lab connect`: one guided pass that wires a Windows PC to a lab host.

It never edits anything silently: each change is shown, asked for, and (for files that already
exist) backed up once first. Everything it can do is also available as a separate command."""

import glob
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import client, watch
from . import ui as term
from .client import ClientError

BACKUP_SUFFIX = ".lab-backup"
CREATE_NEW_PROCESS_GROUP, CREATE_NO_WINDOW = 0x00000200, 0x08000000  # Windows process-creation flags
WINDOWS_SSH = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "OpenSSH" / "ssh.exe"


def ssh_command() -> str:
    """Full path to the Windows OpenSSH client when present, so apps with a trimmed PATH still find it."""
    return str(WINDOWS_SSH) if WINDOWS_SSH.exists() else (shutil.which("ssh") or "ssh")


def mcp_entry(cfg: dict) -> dict:
    return {"command": ssh_command(), "args": client.mcp_server_entry(cfg)["args"]}


def find_claude_desktop_config(env=os.environ, platform: str | None = None, home: Path | None = None) -> Path | None:
    """Where Claude Desktop keeps its config: Windows (classic or Store/MSIX install), macOS, or Linux."""
    platform, home = platform or sys.platform, home or Path.home()
    if platform == "darwin":
        c = home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
        return c if c.parent.exists() else None
    if platform != "win32":
        c = home / ".config" / "Claude" / "claude_desktop_config.json"  # unofficial builds; only if already there
        return c if c.parent.exists() else None
    appdata, local = env.get("APPDATA"), env.get("LOCALAPPDATA")
    candidates = []
    if appdata:
        candidates.append(Path(appdata) / "Claude" / "claude_desktop_config.json")
    if local:  # the Store (MSIX) install keeps it inside the package's private Roaming folder
        candidates += [Path(p) / "claude_desktop_config.json" for p in sorted(glob.glob(str(
            Path(local) / "Packages" / "Claude_*" / "LocalCache" / "Roaming" / "Claude")))]
    for c in candidates:
        if c.exists():
            return c
    for c in candidates:  # installed but never configured: the folder exists, the file does not
        if c.parent.exists():
            return c
    return None


def merge_mcp_server(path: Path, entry: dict, name: str = "lab") -> str:
    """Add or update one server in a Claude config file, keeping everything else. Returns
    'added', 'updated' or 'unchanged'. Refuses to touch a file it cannot parse."""
    text = path.read_text(encoding="utf-8-sig") if path.exists() else ""
    try:
        data = json.loads(text) if text.strip() else {}
    except json.JSONDecodeError as e:
        raise ClientError(f"{path} is not valid JSON ({e}); fix it or edit it by hand. Nothing was changed.") from e
    if not isinstance(data, dict):
        raise ClientError(f"{path} does not contain a JSON object; nothing was changed.")
    servers = data.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise ClientError(f"'mcpServers' in {path} is not an object; nothing was changed.")
    if servers.get(name) == entry:
        return "unchanged"
    state = "updated" if name in servers else "added"
    servers[name] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = path.with_name(path.name + BACKUP_SUFFIX)
    if text.strip() and not backup.exists():
        backup.write_text(text, encoding="utf-8")
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return state


def test_connection(cfg: dict, timeout: int = 15) -> tuple:
    """(ok, text): can this PC reach `lab` on the host over SSH without any prompt?"""
    cmd = [ssh_command(), "-T", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={timeout}", client.target(cfg), "lab", "version"]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 10)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    return res.returncode == 0, (res.stdout.strip() or res.stderr.strip())[-300:]


def explain_failure(text: str) -> str:
    t = text.lower()
    if "could not resolve" in t or "name or service not known" in t:
        return "The name does not resolve. Is Tailscale running on this PC, and is it the host's tailnet name (not a LAN address)?"
    if "permission denied" in t or "publickey" in t:
        return ("The host refused the login. Tailscale SSH must be enabled on it (`tailscale set --ssh`) and your "
                "policy must allow it (the README shows the rule).")
    if "timed out" in t or "connection refused" in t:
        return "Could not reach the host. Check Tailscale is connected on both machines."
    if "not a lab user" in t:
        return "The host reached you but your Tailscale login is not a lab user. Ask the admin to run `lab user add`."
    if "command not found" in t or "not found" in t:
        return "The host is reachable but lab is not installed there. Run the host installer first."
    return "See the message above."


def run(argv: list, *, ask=input, say=print, env=os.environ, load=client.load_config, save=client.save_config) -> int:
    def yes(question: str, default: bool = True) -> bool:
        if ask is input and term.interactive():
            return term.confirm(question, default)
        raw = ask(f"{question} [{'Y/n' if default else 'y/N'}] ").strip().lower()
        return default if not raw else raw in ("y", "yes")

    say(term.banner(subtitle="connect this computer to your Proxmox host"))
    say("\n  Re-running it is safe.\n")
    host = next((a for a in argv if not a.startswith("-")), None)
    ssh_user = argv[argv.index("--user") + 1] if "--user" in argv else None
    try:
        old = load()
    except ClientError:
        old = {}
    default = old.get("host", "")
    host = host or ask(f"Proxmox host's Tailscale name{f' [{default}]' if default else ''}: ").strip() or default
    if not host:
        raise ClientError("a host is needed, e.g. `lab connect pve.your-tailnet.ts.net`")
    ssh_user = ssh_user or old.get("ssh_user") or "root"
    cfg = {"host": host, "ssh_user": ssh_user}

    say(f"\n[1/5] Reaching {client.target(cfg)} ...")
    ok, text = test_connection(cfg)
    say(f"  {'ok' if ok else 'FAILED'}: {text}")
    if not ok:
        say(f"  -> {explain_failure(text)}")
        if not yes("Save the settings anyway?", False):
            return 3
    save(host, ssh_user)

    say("\n[2/5] SSH shortcuts (ssh lab-<project>, VS Code Remote-SSH)")
    if ok and yes("Add them to ~/.ssh/config?"):
        client.cmd_ssh_config(cfg)

    say("\n[3/5] Claude Desktop")
    path = find_claude_desktop_config(env)
    entry = mcp_entry(cfg)
    if path is None:
        say("  Claude Desktop does not seem to be installed. When it is, add this under \"mcpServers\":")
        say(json.dumps({"lab": entry}, indent=2))
    elif yes(f"Add the lab server to {path}?"):
        state = merge_mcp_server(path, entry)
        say(f"  {state}. Quit Claude Desktop completely (tray icon -> Quit) and reopen it.")

    say("\n[4/5] Claude Code")
    claude = shutil.which("claude")
    cmd = ["claude", "mcp", "add", "--scope", "user", "lab", "--", entry["command"], *entry["args"]]
    if claude and yes("Register lab with Claude Code for all your projects?"):
        res = subprocess.run([claude, *cmd[1:]], capture_output=True, text=True)
        say("  registered." if res.returncode == 0 else f"  failed: {(res.stderr or res.stdout).strip()[-200:]}")
    elif not claude:
        say("  The `claude` command is not on this PC. For a project folder, put this in `.mcp.json`:")
        say(json.dumps({"mcpServers": {"lab": entry}}, indent=2))

    say("\n[5/5] Approval popups")
    say("  When an AI session needs permission, a window appears here with Approve / Deny"
        + (" (a blurred card)." if sys.platform == "win32" else " (a native dialog)."))
    if yes("Run the approval helper automatically at login and start it now?"):
        print_path = watch.install()
        if sys.platform != "darwin":  # on macOS the LaunchAgent has already started it
            subprocess.Popen(watch.launcher_args(), close_fds=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL,
                             # a hidden console (not DETACHED_PROCESS, which has none): the helper's ssh polls inherit it
                             **({"creationflags": CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW}
                                if sys.platform == "win32" else {"start_new_session": True}))
        say(f"  installed ({print_path.name}) and running.")

    say("\nDone. Try:  lab list     or ask Claude: \"list my lab projects\"")
    return 0
