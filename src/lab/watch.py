"""Desktop approval helper for the user's PC (`lab watch`).

It polls the host for approvals that an AI session is waiting on and shows a popup with Approve and
Deny buttons. While it runs it sends a heartbeat, so the MCP server holds a gated call open for a
click instead of failing it. Works with any MCP client (no client support for prompts needed).

Caveat worth knowing: it approves as *you*, over your SSH identity. An AI session that also has a
shell on this PC (Claude Code with Bash) could run the same commands, so keep `lab`/`ssh` out of
its auto-approved commands. The MCP tools themselves have no way to grant an approval."""

import os
import shutil
import sys
import time
from pathlib import Path

from . import client
from .client import ClientError

POLL = 3.0
LOG = Path.home() / ".lab" / "watch.log"
STARTUP_FILE = "lab-approvals.vbs"


def log(msg: str, path: Path = LOG) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > 200_000:
            path.write_text("", encoding="utf-8")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass


def pending(cfg: dict) -> list:
    """Approvals waiting for a decision (also tells the server a human is watching)."""
    return client.remote_json(cfg, ["approvals", "--pending", "--heartbeat"])


def decide(cfg: dict, approval_id: str, approve: bool) -> None:
    args = ["approve", approval_id, "--yes"] if approve else ["approve", approval_id, "--deny"]
    res = client.run_remote(cfg, args, capture=True)
    if res.returncode != 0:
        raise ClientError((res.stderr or res.stdout or "approve failed").strip())


SCRIPT = Path(__file__).parent / "data" / "approve_popup.ps1"
# exit codes of the popup script
APPROVED, DENIED, TIMED_OUT = 10, 11, 12


class PopupError(Exception):
    pass


def describe(item: dict) -> dict:
    """What the popup says, in plain words, for one pending approval (kept out of the UI code so
    it is testable): headline, one-line explanation, whether it is dangerous, and the detail rows."""
    tool, project, plan = item.get("tool") or "", item.get("project") or "", item.get("plan") or ""
    quoted = f"“{project}”" if project else "this project"
    danger = False
    if "PROTECTED" in plan:
        title, sub, danger = f"Allow access to the protected project {quoted}?", plan, True
    elif tool == "enter":
        title = f"Let Claude work in {quoted}?"
        sub = ("It didn’t create this project. With your OK it can run commands, read and write files, "
               "and start or stop it.")
    elif tool == "lab_delete":
        title, sub, danger = f"Delete {quoted}?", plan, True
    elif tool == "lab_archive":
        title = f"Archive {quoted}?"
        sub = ("It is stopped, backed up to the archive disk and checked with a trial restore, then removed "
               "from the fast disk. It stays unavailable until restored.")
    elif tool == "lab_restore":
        title, sub = f"Restore {quoted}?", "It is restored from its archive onto the fast disk and started."
    elif tool == "lab_exec_root":
        title, danger = f"Run a command as root in {quoted}?", True
        sub = plan.split(": ", 1)[1] if ": " in plan else plan
    elif tool in ("lab_read_root", "lab_write_root"):
        verb = "Read" if tool == "lab_read_root" else "Write"
        title, sub, danger = f"{verb} a file as root in {quoted}?", plan, True
    elif tool == "lab_create":
        title, sub = f"Create {quoted} beyond the usual limits?", plan
    else:
        title, sub = "Allow this action?", plan
    minutes = max(1, int(item.get("expires_in", 0)) // 60)
    details = [("Action", tool or "-"), ("Project", project or "-"), ("Requested for", item.get("user") or "-"),
               ("Approval ID", item.get("id") or "-"), ("Expires", f"in {minutes} min")]
    return {"title": title, "subtitle": sub, "danger": danger,
            "details": [{"label": k, "value": v} for k, v in details], "plan": plan}


SAMPLES = {
    "enter": {"id": "a0c371", "tool": "enter", "project": "demo2", "user": "alex", "expires_in": 540,
              "plan": "Let this AI session work inside project 'demo2' (owner alex): run commands, read and "
                      "write files, start and stop it. It did not create it and it was not attached."},
    "delete": {"id": "7be201", "tool": "lab_delete", "project": "demo", "user": "alex", "expires_in": 590,
               "plan": "PERMANENTLY DELETE 'demo' (VM 1000) and its disk. This cannot be undone."},
    "root": {"id": "c41d9e", "tool": "lab_exec_root", "project": "demo", "user": "alex", "expires_in": 300,
             "plan": "Run as ROOT in 'demo': apt-get install -y build-essential && systemctl restart nginx"},
    "archive": {"id": "55aa10", "tool": "lab_archive", "project": "demo", "user": "alex", "expires_in": 600,
                "plan": "ARCHIVE 'demo': ..."},
    "protected": {"id": "99f0b2", "tool": "lab_exec", "project": "vault", "user": "alex", "expires_in": 600,
                  "plan": "'vault' is a PROTECTED project. Allow this AI session to run: lab_exec {\"command\": \"ls\"}"},
}


def show_popup(item: dict, alive, *, autoclose: float | None = None, selftest: str | None = None,
               expanded: bool = False, poll: float = 3.0, popen=None, script: Path = SCRIPT,
               sleep=time.sleep, monotonic=time.monotonic, backdrop: str | None = None,
               theme: str | None = None, monitor: str | None = None) -> bool | None:
    """The native popup (blurred screen, Apple-style card). True = Approve, False = Deny (Esc or
    closing also denies), None = settled elsewhere or timed out. Raises PopupError if it cannot show,
    so the caller can fall back to the plain Tk window."""
    import json
    import subprocess
    import tempfile
    exe = shutil.which("powershell") or shutil.which("pwsh")
    if os.name != "nt" or not exe or not script.exists():
        raise PopupError("the native popup needs Windows PowerShell")
    payload = {**describe(item), "id": item.get("id"), "expires_in": item.get("expires_in", 600),
               "autoclose": autoclose, "selftest": selftest, "expanded": expanded,
               "backdrop": backdrop, "theme": theme, "monitor": monitor}
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as fh:
        json.dump(payload, fh)
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = (popen or subprocess.Popen)([exe, "-NoProfile", "-NonInteractive", "-STA", "-ExecutionPolicy", "Bypass",
                                            "-File", str(script), "-Payload", fh.name],
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, creationflags=flags)
        code = _supervise(proc, alive, poll, sleep, monotonic)
        if code is None:
            return None
        out, err = proc.communicate()
    except OSError as e:
        raise PopupError(str(e)) from e
    finally:
        try:
            os.unlink(fh.name)
        except OSError:
            pass
    if code == APPROVED:
        return True
    if code == DENIED:
        return False
    if code == TIMED_OUT:
        return None
    raise PopupError((err or out or f"popup exited with code {code}").strip()[-300:])


def _supervise(proc, alive, poll: float, sleep, monotonic):
    """Wait for a dialog process. Returns its exit code, or None if the approval was settled elsewhere
    (or expired) while it was open, in which case the dialog is taken down."""
    last = monotonic()
    while proc.poll() is None:
        sleep(0.25)
        if monotonic() - last >= poll:
            last = monotonic()
            if not alive():
                proc.terminate()
                proc.wait()
                return None
    return proc.returncode


def _dialog_text(item: dict) -> str:
    d = describe(item)
    minutes = max(1, int(item.get("expires_in", 0)) // 60)
    return "\n".join([d["title"], "", d["subtitle"], "",
                      f"Project: {item.get('project') or '-'}   Approval ID: {item.get('id') or '-'}   Expires in {minutes} min"])


def macos_script(item: dict) -> str:
    """AppleScript for the native macOS dialog: Return = Approve (the default button), Esc = Deny."""
    d = describe(item)
    text = _dialog_text(item).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    timeout = max(30, int(item.get("expires_in", 600)))
    return (f'display dialog "{text}" with title "Lab: approval needed" buttons {{"Deny", "Approve"}} '
            f'default button "Approve" cancel button "Deny" with icon {"caution" if d["danger"] else "note"} '
            f"giving up after {timeout}")


def show_macos(item: dict, alive, *, popen=None, poll: float = 3.0, sleep=time.sleep, monotonic=time.monotonic) -> bool | None:
    import subprocess
    if shutil.which("osascript") is None:
        raise PopupError("osascript not found")
    try:
        proc = (popen or subprocess.Popen)(["osascript", "-e", macos_script(item)], stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE, text=True)
        code = _supervise(proc, alive, poll, sleep, monotonic)
        if code is None:
            return None
        out, err = proc.communicate()
    except OSError as e:
        raise PopupError(str(e)) from e
    if code == 0:
        if "gave up:true" in out:
            return None
        return "button returned:Approve" in out
    if "-128" in err:  # the user pressed Esc / the Deny (cancel) button
        return False
    raise PopupError((err or out or f"osascript exited with {code}").strip()[-300:])


def linux_command(item: dict) -> list:
    """The first available native dialog (zenity, then kdialog), as an argv list."""
    d = describe(item)
    text = _dialog_text(item)
    timeout = max(30, int(item.get("expires_in", 600)))
    if shutil.which("zenity"):
        return ["zenity", "--question", "--no-markup", "--title", "Lab: approval needed", "--text", text,
                "--ok-label", "Approve", "--cancel-label", "Deny", "--width", "460", "--timeout", str(timeout),
                "--icon-name", "dialog-warning" if d["danger"] else "dialog-password"]
    if shutil.which("kdialog"):
        return ["kdialog", "--title", "Lab: approval needed", "--yes-label", "Approve", "--no-label", "Deny", "--yesno", text]
    raise PopupError("neither zenity nor kdialog is installed")


def show_linux(item: dict, alive, *, popen=None, poll: float = 3.0, sleep=time.sleep, monotonic=time.monotonic) -> bool | None:
    import subprocess
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        raise PopupError("no graphical session (DISPLAY / WAYLAND_DISPLAY is not set)")
    cmd = linux_command(item)
    try:
        proc = (popen or subprocess.Popen)(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        code = _supervise(proc, alive, poll, sleep, monotonic)
        if code is None:
            return None
        out, err = proc.communicate()
    except OSError as e:
        raise PopupError(str(e)) from e
    if code == 0:
        return True
    if code == 5 and cmd[0] == "zenity":  # zenity's --timeout elapsed
        return None
    if code in (1, 2):  # Deny, or Esc / window closed
        return False
    raise PopupError((err or out or f"{cmd[0]} exited with {code}").strip()[-300:])


def show_best(item: dict, alive) -> bool | None:
    """The best popup this machine can show: Windows card, macOS dialog, Linux zenity/kdialog, then Tk."""
    native = {"win32": show_popup, "darwin": show_macos}.get(sys.platform, show_linux)
    try:
        return native(item, alive)
    except PopupError as e:
        log(f"native popup unavailable ({e}); trying the plain window")
    try:
        return show_dialog(item, alive)
    except Exception as e:  # Tk missing, or no display
        raise PopupError(f"no way to show a window here ({e})") from e


def show_dialog(item: dict, alive) -> bool | None:
    """Plain Tk fallback. True = approved, False = denied (closing the window denies),
    None = it was settled elsewhere or expired while the window was open."""
    import tkinter as tk
    result = {"v": None}
    root = tk.Tk()
    root.title("Lab: approval needed")
    root.attributes("-topmost", True)
    root.resizable(False, False)
    frame = tk.Frame(root, padx=18, pady=14)
    frame.pack()
    tk.Label(frame, text="An AI session is asking for your permission", font=("Segoe UI", 11, "bold")).pack(anchor="w")
    tk.Label(frame, text=item["plan"], wraplength=460, justify="left", font=("Segoe UI", 10)).pack(anchor="w", pady=(10, 6))
    tk.Label(frame, text=f"Project: {item.get('project') or '-'}   |   expires in {max(1, item.get('expires_in', 0) // 60)} min",
             fg="#666666", font=("Segoe UI", 9)).pack(anchor="w")
    row = tk.Frame(frame)
    row.pack(anchor="e", pady=(14, 0))

    def done(value):
        result["v"] = value
        root.destroy()

    approve = tk.Button(row, text="Approve", width=12, command=lambda: done(True))
    deny = tk.Button(row, text="Deny", width=12, command=lambda: done(False))
    approve.pack(side="left", padx=(0, 8))
    deny.pack(side="left")
    deny.focus_set()  # a stray Enter or Space must never approve
    root.protocol("WM_DELETE_WINDOW", lambda: done(False))

    def check():
        if alive():
            root.after(3000, check)
        else:
            done(None)
    root.after(3000, check)
    root.update_idletasks()
    x = (root.winfo_screenwidth() - root.winfo_reqwidth()) // 2
    y = (root.winfo_screenheight() - root.winfo_reqheight()) // 3
    root.geometry(f"+{x}+{y}")
    root.lift()
    root.focus_force()
    root.bell()
    root.mainloop()
    return result["v"]


def run(cfg: dict, *, show=show_best, poll: float = POLL, sleep=time.sleep, once: bool = False,
        logger=log, pending_fn=None, decide_fn=None) -> None:
    pending_fn = pending_fn or (lambda: pending(cfg))
    decide_fn = decide_fn or (lambda aid, ok: decide(cfg, aid, ok))
    seen: set = set()
    logger("watching for approvals")
    while True:
        try:
            items = pending_fn()
        except (ClientError, OSError, ValueError) as e:
            logger(f"cannot reach the host: {e}")
            if once:
                raise ClientError(f"cannot reach the host: {e}") from e
            sleep(10)
            continue
        for item in items:
            if item["id"] in seen:
                continue
            seen.add(item["id"])

            def alive(aid=item["id"]):
                try:
                    return any(p["id"] == aid for p in pending_fn())
                except (ClientError, OSError, ValueError):
                    return True

            try:
                answer = show(item, alive)
            except PopupError as e:
                logger(f"{item['id']}: cannot show a popup ({e}); approve it with `lab approve {item['id']}`")
                continue
            if answer is None:
                logger(f"{item['id']}: settled elsewhere")
                continue
            try:
                decide_fn(item["id"], answer)
                logger(f"{item['id']}: {'approved' if answer else 'denied'} ({item['plan'][:80]})")
            except ClientError as e:
                logger(f"{item['id']}: could not record the decision: {e}")
        if once:
            return
        sleep(poll)


# autostart ---------------------------------------------------------------------------------------------
LAUNCH_LABEL = "io.github.madmaxvoltro.proxmox-lab.watch"


def startup_dir() -> Path:
    return Path(os.environ.get("APPDATA", str(Path.home()))) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def launcher_args() -> list:
    """How to start the watcher again: the packaged executable, or `python -m lab.entry`."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "watch"]
    return [sys.executable, "-m", "lab.entry", "watch"]


def startup_script(python: str | None = None) -> str:
    """Windows: a tiny script for the Startup folder that runs the watcher with no console window."""
    if python is None and getattr(sys, "frozen", False):
        return f'CreateObject("WScript.Shell").Run """{sys.executable}"" watch", 0, False\r\n'
    py = Path(python or sys.executable)
    pyw = py.with_name("pythonw.exe")
    exe = pyw if pyw.exists() else py
    return f'CreateObject("WScript.Shell").Run """{exe}"" -m lab.entry watch", 0, False\r\n'


def launch_agent_plist(args: list | None = None, home: Path | None = None) -> str:
    """macOS: a LaunchAgent that starts the watcher at login and keeps it running."""
    from xml.sax.saxutils import escape
    args = args or launcher_args()
    items = "".join(f"\n    <string>{escape(str(a))}</string>" for a in args)
    err = escape(str((home or Path.home()) / ".lab" / "watch.err"))
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n<dict>\n'
        f"  <key>Label</key>\n  <string>{LAUNCH_LABEL}</string>\n"
        f"  <key>ProgramArguments</key>\n  <array>{items}\n  </array>\n"
        "  <key>RunAtLoad</key>\n  <true/>\n"
        "  <key>KeepAlive</key>\n  <true/>\n"
        "  <key>LimitLoadToSessionType</key>\n  <string>Aqua</string>\n"
        f"  <key>StandardErrorPath</key>\n  <string>{err}</string>\n"
        "</dict>\n</plist>\n"
    )


def desktop_entry(args: list | None = None) -> str:
    """Linux: an XDG autostart entry (works in GNOME, KDE, XFCE and friends)."""
    def q(a) -> str:
        s = str(a).replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`")
        return f'"{s}"'
    return (
        "[Desktop Entry]\nType=Application\nName=proxmox-lab approvals\n"
        "Comment=Shows a window when an AI session needs your permission\n"
        f"Exec={' '.join(q(a) for a in (args or launcher_args()))}\n"
        "Terminal=false\nNoDisplay=true\nX-GNOME-Autostart-enabled=true\n"
    )


def _autostart_name(platform: str) -> str:
    return {"win32": STARTUP_FILE, "darwin": f"{LAUNCH_LABEL}.plist"}.get(platform, "lab-watch.desktop")


def autostart_path(platform: str | None = None, home: Path | None = None) -> Path:
    platform, home = platform or sys.platform, home or Path.home()
    if platform == "win32":
        return startup_dir() / STARTUP_FILE
    if platform == "darwin":
        return home / "Library" / "LaunchAgents" / _autostart_name(platform)
    return home / ".config" / "autostart" / _autostart_name(platform)


def _autostart_text(platform: str) -> str:
    return {"win32": startup_script, "darwin": launch_agent_plist}.get(platform, desktop_entry)()


def _launchctl(*args: str) -> None:
    import subprocess
    try:
        subprocess.run(["launchctl", *args], capture_output=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        pass  # it will simply load at the next login


def install(folder: Path | None = None, platform: str | None = None) -> Path:
    """Start the approval helper at login. `folder` is for tests: write there instead of the real place."""
    platform = platform or sys.platform
    target = folder / _autostart_name(platform) if folder else autostart_path(platform)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_autostart_text(platform), encoding="utf-8", newline="")
    if platform == "darwin" and not folder:
        _launchctl("bootstrap", f"gui/{os.getuid()}", str(target))
    return target


def uninstall(folder: Path | None = None, platform: str | None = None) -> bool:
    platform = platform or sys.platform
    target = folder / _autostart_name(platform) if folder else autostart_path(platform)
    if platform == "darwin" and not folder and target.exists():
        _launchctl("bootout", f"gui/{os.getuid()}/{LAUNCH_LABEL}")
    if target.exists():
        target.unlink()
        return True
    return False


def main(cfg: dict, argv: list) -> int:
    if "--install" in argv:
        print(f"will start at login: {install()}\nStart it now with: lab watch")
        return 0
    if "--uninstall" in argv:
        print("removed from startup" if uninstall() else "was not installed")
        return 0
    if "--preview" in argv:
        i = argv.index("--preview")
        kind = argv[i + 1] if i + 1 < len(argv) and not argv[i + 1].startswith("-") else "enter"
        if kind not in SAMPLES:
            print(f"unknown sample {kind!r}; choose from: {', '.join(SAMPLES)}")
            return 2
        opt = lambda name, default=None: argv[argv.index(name) + 1] if name in argv else default
        auto = opt("--autoclose")
        if sys.platform == "win32":
            result = show_popup(SAMPLES[kind], lambda: True, autoclose=float(auto) if auto else None,
                                selftest=opt("--selftest"), expanded="--expanded" in argv,
                                backdrop=opt("--backdrop"), theme=opt("--theme"), monitor=opt("--monitor"))
        else:
            result = (show_macos if sys.platform == "darwin" else show_linux)(SAMPLES[kind], lambda: True)
        print({True: "Approve", False: "Deny", None: "closed without an answer"}[result])
        return 0
    once = "--once" in argv
    if not once:
        print("Watching for approvals. A popup appears when an AI session needs your permission. Ctrl-C to stop.")
    try:
        run(cfg, once=once)
    except KeyboardInterrupt:
        return 0
    return 0
