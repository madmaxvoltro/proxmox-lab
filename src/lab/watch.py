"""Desktop approval helper for the user's PC (`lab watch`).

It polls the host for approvals that an AI session is waiting on and shows a popup with Approve and
Deny buttons. While it runs it sends a heartbeat, so the MCP server holds a gated call open for a
click instead of failing it. Works with any MCP client (no client support for prompts needed).

Caveat worth knowing: it approves as *you*, over your SSH identity. An AI session that also has a
shell on this PC (Claude Code with Bash) could run the same commands, so keep `lab`/`ssh` out of
its auto-approved commands. The MCP tools themselves have no way to grant an approval."""

import os
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


def show_dialog(item: dict, alive) -> bool | None:
    """Popup with Approve / Deny. True = approved, False = denied (closing the window denies),
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


def run(cfg: dict, *, show=show_dialog, poll: float = POLL, sleep=time.sleep, once: bool = False,
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

            answer = show(item, alive)
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
def startup_dir() -> Path:
    return Path(os.environ.get("APPDATA", str(Path.home()))) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def startup_script(python: str | None = None) -> str:
    """A tiny script for the Startup folder that runs the watcher with no console window."""
    py = Path(python or sys.executable)
    pyw = py.with_name("pythonw.exe")
    exe = pyw if pyw.exists() else py
    return f'CreateObject("WScript.Shell").Run """{exe}"" -m lab.entry watch", 0, False\r\n'


def install(folder: Path | None = None) -> Path:
    target = (folder or startup_dir()) / STARTUP_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(startup_script(), encoding="utf-8", newline="")
    return target


def uninstall(folder: Path | None = None) -> bool:
    target = (folder or startup_dir()) / STARTUP_FILE
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
    once = "--once" in argv
    if not once:
        print("Watching for approvals. A popup appears when an AI session needs your permission. Ctrl-C to stop.")
    try:
        run(cfg, once=once)
    except KeyboardInterrupt:
        return 0
    return 0
