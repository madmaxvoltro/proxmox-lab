"""`lab settings`: view and change the few host settings that are safe to flip from the command line.

Ordinary settings change after one confirmation. "Special" ones loosen a safety rule, so enabling one shows
a warning box and asks a Yes/No question that defaults to No; scripts must pass --yes to enable them."""

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import config as config_mod
from . import ui as term
from .errors import PreconditionError, UsageError
from .prompt import Prompter
from .units import parse_size


@dataclass(frozen=True)
class Setting:
    key: str            # section.name, as in config.toml
    title: str
    help: str
    kind: str           # bool | int | size
    special: bool = False
    warning: str = ""   # shown before a special setting is enabled
    low: int = 1
    high: int = 64


SETTINGS = [
    Setting("mcp.root_without_approval", "AI may use root without asking",
            "Root commands and root file access inside a project VM run without a popup.", "bool", True,
            "Root can change anything inside that project's VM. The VM is disposable and cannot reach the Proxmox "
            "host, but anything stored in it is exposed to the AI. Delete, archive, restore and protected projects "
            "still ask. Every use is logged."),
    Setting("mcp.attach_without_approval", "AI may enter your projects without asking",
            "A session may work inside projects owned by its own user without a popup.", "bool", True,
            "Any AI session on your account can then run commands in all of your non-protected projects. Protected "
            "projects and other users' projects still ask. Every use is logged."),
    Setting("mcp.max_running_vms", "Most VMs the AI may run at once", "Creating more needs approval.", "int"),
    Setting("mcp.max_cpu", "Most vCPUs per AI-created VM", "Creating bigger needs approval.", "int"),
    Setting("mcp.max_ram", "Most RAM per AI-created VM", "For example 8G or 16G. Bigger needs approval.", "size"),
    Setting("mcp.approval_wait_s", "How long a call waits for your click (seconds)",
            "While the desktop helper runs, a gated call waits this long before asking you to run `lab approve`.",
            "int", low=0, high=600),
]
BY_KEY = {s.key: s for s in SETTINGS}


def current(cfg: config_mod.Config, s: Setting):
    section, name = s.key.split(".")
    return getattr(getattr(cfg, section), name)


def parse(s: Setting, raw: str):
    text = str(raw).strip()
    if s.kind == "bool":
        if text.lower() in ("on", "true", "yes", "1", "enable", "enabled"):
            return True
        if text.lower() in ("off", "false", "no", "0", "disable", "disabled"):
            return False
        raise UsageError(f"{s.key} takes on or off")
    if s.kind == "int":
        if not text.isdigit() or not s.low <= int(text) <= s.high:
            raise UsageError(f"{s.key} takes a whole number from {s.low} to {s.high}")
        return int(text)
    try:
        if parse_size(text) < (1 << 28):
            raise ValueError
    except (ValueError, UsageError):
        raise UsageError(f"{s.key} takes a size of at least 256M, like 8G") from None
    return text


def show(value) -> str:
    return ("on" if value else "off") if isinstance(value, bool) else str(value)


def table(cfg: config_mod.Config) -> str:
    rows = [(s.key, show(current(cfg, s)), s.title + ("  [special]" if s.special else "")) for s in SETTINGS]
    wk, wv = max(len(r[0]) for r in rows), max(len(r[1]) for r in rows)
    return "\n".join(f"  {k:<{wk}}  {v:<{wv}}  {t}" for k, v, t in rows)


def warning_box(s: Setting, st: term.Style | None = None) -> str:
    st = st or term.default_style()
    words, lines, line = s.warning.split(), [], ""
    for w in words:
        if len(line) + len(w) + 1 > 62:
            lines.append(line)
            line = ""
        line = f"{line} {w}".strip()
    lines.append(line)
    width = max(62, len(s.title) + 4)
    top, bot = ("╭" + "─" * (width + 2) + "╮", "╰" + "─" * (width + 2) + "╯") if st.utf else ("+" + "-" * (width + 2) + "+",) * 2
    side = "│" if st.utf else "|"
    body = [f"  {st.c(term.YELLOW, side)} {st.c(term.BOLD, s.title.ljust(width))} {st.c(term.YELLOW, side)}",
            f"  {st.c(term.YELLOW, side)} {' ' * width} {st.c(term.YELLOW, side)}"]
    body += [f"  {st.c(term.YELLOW, side)} {ln.ljust(width)} {st.c(term.YELLOW, side)}" for ln in lines]
    return "\n".join([f"  {st.c(term.YELLOW, top)}", *body, f"  {st.c(term.YELLOW, bot)}"])


def save(cfg: config_mod.Config, path: str) -> None:
    """Write the config atomically, keeping the old file as config.toml.bak and its permissions."""
    p = Path(path)
    try:
        if p.exists():
            (p.with_name(p.name + ".bak")).write_bytes(p.read_bytes())
        fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".config-", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(config_mod.dumps(cfg))
        if p.exists():
            os.chmod(tmp, p.stat().st_mode & 0o777)
        os.replace(tmp, p)
    except PermissionError as e:
        raise PreconditionError(f"cannot write {path}: run `lab settings` as root on the Proxmox host") from e


def apply(cfg: config_mod.Config, s: Setting, value, path: str) -> None:
    section, name = s.key.split(".")
    setattr(getattr(cfg, section), name, value)
    cfg.validate()
    save(cfg, path)


def confirm_change(ui: Prompter, s: Setting, value, assume_yes: bool, st=None) -> bool:
    """The confirmation step. Enabling a special setting shows the warning box and defaults to No."""
    if s.special and value is True:
        if assume_yes:
            return True
        ui.info("\n" + warning_box(s, st) + "\n")
        return ui.confirm(f"Enable '{s.title}'?", False)
    if assume_yes:
        return True
    return ui.confirm(f"Change {s.key} to {show(value)}?", True)


AFTER = "Running AI sessions read settings when they connect: restart Claude Desktop (or start a new chat) to apply it."


def set_value(cfg, key: str, raw: str, path: str, ui: Prompter, *, assume_yes: bool = False) -> bool:
    s = BY_KEY.get(key)
    if s is None:
        raise UsageError(f"unknown setting {key!r}; run `lab settings` to see them")
    value = parse(s, raw)
    if value == current(cfg, s):
        ui.info(f"{key} is already {show(value)}")
        return False
    if not confirm_change(ui, s, value, assume_yes):
        ui.info("not changed")
        return False
    apply(cfg, s, value, path)
    ui.info(f"{key} = {show(value)}\n{AFTER}")
    return True


def interactive(cfg, path: str, ui: Prompter) -> int:
    """Menu loop: pick a setting with the arrow keys, change it, repeat until Done."""
    DONE = "__done__"
    ui.info(term.banner(subtitle="host settings"))
    while True:
        options = [(s.key, f"{show(current(cfg, s))}  {s.title}" + ("  [special]" if s.special else "")) for s in SETTINGS]
        picked = ui.choose("Which setting?", options + [(DONE, "finish")], DONE)
        if picked == DONE:
            return 0
        s = BY_KEY[picked]
        ui.info(f"\n  {s.title}\n  {s.help}\n  now: {show(current(cfg, s))}")
        if s.kind == "bool":
            value = not current(cfg, s)
        else:
            raw = ui.ask(f"New value for {s.key}", str(current(cfg, s)))
            try:
                value = parse(s, raw)
            except UsageError as e:
                ui.info(f"  {e}")
                continue
        if value == current(cfg, s):
            continue
        if confirm_change(ui, s, value, False):
            apply(cfg, s, value, path)
            ui.info(f"  {s.key} = {show(value)}\n  {AFTER}\n")
        else:
            ui.info("  not changed\n")
