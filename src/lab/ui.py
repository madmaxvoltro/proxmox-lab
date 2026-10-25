"""Terminal look and feel: the banner, a tree of steps, and menus you drive with the arrow keys.

Standard library only. Menus work on Windows (msvcrt) and macOS/Linux (termios). When there is no real
terminal (a pipe, CI, a test) callers fall back to plain numbered questions: see `interactive()`."""

import os
import sys

TITLE = "proxmox-lab"
TAGLINE = "disposable project VMs on Proxmox, safe for AI"

RESET, BOLD, DIM, UNDER = "\033[0m", "\033[1m", "\033[2m", "\033[4m"
CYAN, GREEN, RED, YELLOW = "\033[36m", "\033[32m", "\033[31m", "\033[33m"


def interactive(stdin=None, stdout=None) -> bool:
    """True when both input and output are a real terminal, so a menu can be drawn and driven by keys."""
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    try:
        return bool(stdin.isatty() and stdout.isatty()) and os.environ.get("TERM") != "dumb"
    except (AttributeError, ValueError):
        return False


def unicode_ok(stream=None) -> bool:
    return "utf" in (getattr(stream or sys.stdout, "encoding", "") or "").lower()


def enable_ansi() -> bool:
    """Turn on escape-sequence handling in the Windows console (a no-op elsewhere)."""
    if os.name != "nt":
        return True
    try:
        import ctypes
        kernel = ctypes.windll.kernel32
        handle = kernel.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not kernel.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel.SetConsoleMode(handle, mode.value | 0x0004))
    except (AttributeError, OSError):
        return False


class Style:
    """Colours and glyphs, with plain fallbacks for terminals that cannot show them."""

    def __init__(self, color: bool = True, utf: bool = True):
        self.color, self.utf = color, utf
        self.pointer = "❯" if utf else ">"
        self.tick, self.cross, self.bullet = ("✔", "✘", "◆") if utf else ("+", "x", "*")
        self.bar, self.end = ("│", "└") if utf else ("|", "`")

    def c(self, code: str, text: str) -> str:
        return f"{code}{text}{RESET}" if self.color else text


def default_style(stream=None) -> Style:
    stream = stream or sys.stdout
    color = interactive() and not os.environ.get("NO_COLOR")
    if color:
        enable_ansi()
    return Style(color, unicode_ok(stream))


# ---- the banner and the tree of steps ------------------------------------------------------------------
def banner(st: Style | None = None, subtitle: str = TAGLINE) -> str:
    if os.environ.get("LAB_BANNER_SHOWN"):  # the installer that started us already drew it
        return ""
    st = st or default_style()
    width = max(len(TITLE) + 4, len(subtitle)) + 4
    if st.utf:
        top, bot, side = "╭" + "─" * width + "╮", "╰" + "─" * width + "╯", "│"
    else:
        top = bot = "+" + "-" * width + "+"
        side = "|"
    rows = [top,
            f"{side}  {st.c(BOLD + CYAN, TITLE)}{' ' * (width - len(TITLE) - 2)}{side}",
            f"{side}  {st.c(DIM, subtitle)}{' ' * (width - len(subtitle) - 2)}{side}",
            bot]
    return "\n".join("  " + r for r in rows)


def step_title(title: str, st: Style | None = None) -> str:
    st = st or default_style()
    return f"\n  {st.c(DIM, st.bar)}\n  {st.c(CYAN, st.bullet)} {st.c(BOLD, title)}"


def line_ok(text: str, st: Style | None = None) -> str:
    st = st or default_style()
    return f"  {st.c(DIM, st.bar)}  {st.c(GREEN, st.tick)} {text}"


def line_fail(text: str, st: Style | None = None) -> str:
    st = st or default_style()
    return f"  {st.c(DIM, st.bar)}  {st.c(RED, st.cross)} {text}"


def line_note(text: str, st: Style | None = None) -> str:
    st = st or default_style()
    return f"  {st.c(DIM, st.bar)}  {st.c(DIM, text)}"


def closing(text: str, st: Style | None = None) -> str:
    st = st or default_style()
    return f"  {st.c(DIM, st.bar)}\n  {st.c(DIM, st.end)} {st.c(BOLD, text)}"


# ---- reading keys ----------------------------------------------------------------------------------------
def read_key() -> str:
    """One key press as a name: up, down, left, right, tab, shift_tab, enter, esc, ctrl_c, or the character."""
    if os.name == "nt":
        return _read_key_windows()
    return _read_key_posix()


def _read_key_windows() -> str:
    import msvcrt
    ch = msvcrt.getwch()
    if ch in ("\x00", "\xe0"):
        return {"H": "up", "P": "down", "K": "left", "M": "right", "\x0f": "shift_tab"}.get(msvcrt.getwch(), "")
    return {"\r": "enter", "\n": "enter", "\t": "tab", "\x1b": "esc", "\x03": "ctrl_c"}.get(ch, ch)


def _read_key_posix() -> str:
    import select
    import termios
    import tty
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        ch = os.read(fd, 1).decode("utf-8", "ignore")
        if ch == "\x1b":
            seq = ""
            while select.select([fd], [], [], 0.05)[0] and len(seq) < 4:
                seq += os.read(fd, 1).decode("utf-8", "ignore")
                if seq[-1:].isalpha() or seq[-1:] == "~":
                    break
            return {"[A": "up", "[B": "down", "[C": "right", "[D": "left", "[Z": "shift_tab",
                    "OA": "up", "OB": "down", "OC": "right", "OD": "left"}.get(seq, "esc")
        return {"\r": "enter", "\n": "enter", "\t": "tab", "\x03": "ctrl_c", "\x04": "ctrl_c"}.get(ch, ch)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


# ---- menus -----------------------------------------------------------------------------------------------
def render_menu(label: str, options: list, index: int, st: Style) -> list:
    """The lines of a menu. The selected option is cyan, bold and underlined with a pointer in front."""
    lines = [f"  {st.c(BOLD, label)}  {st.c(DIM, '(arrow keys or Tab to move, Enter to choose)')}"]
    for i, (value, desc) in enumerate(options):
        text = str(value)
        if i == index:
            row = f"  {st.c(CYAN, st.pointer)} {st.c(BOLD + CYAN + UNDER, text)}"
        else:
            row = f"    {text}"
        if desc:
            row += "  " + st.c(DIM, str(desc))
        lines.append(row)
    return lines


def _redraw(out, lines: list, previous: int) -> None:
    if previous:
        out.write(f"\033[{previous}A")
    for line in lines:
        out.write("\r\033[2K" + line + "\n")
    out.flush()


def select(label: str, options: list, default=None, *, readkey=None, out=None, st: Style | None = None):
    """Choose one of [(value, description)] with the arrow keys or Tab, confirm with Enter. Returns the value.
    Ctrl-C and Esc raise KeyboardInterrupt (nothing is chosen)."""
    out, readkey = out or sys.stdout, readkey or read_key
    st = st or default_style(out)
    values = [o[0] for o in options]
    index = values.index(default) if default in values else 0
    shown = 0
    out.write("\033[?25l")  # hide the cursor while the menu is up
    try:
        while True:
            lines = render_menu(label, options, index, st)
            _redraw(out, lines, shown)
            shown = len(lines)
            key = readkey()
            if key in ("down", "tab", "j"):
                index = (index + 1) % len(options)
            elif key in ("up", "shift_tab", "k"):
                index = (index - 1) % len(options)
            elif key == "enter":
                break
            elif key in ("ctrl_c", "esc"):
                raise KeyboardInterrupt
    finally:
        out.write("\033[?25h")
    # collapse the menu to one line that records the answer
    out.write(f"\033[{shown}A")
    out.write("\r\033[2K" + f"  {st.c(GREEN, st.tick)} {label}  {st.c(BOLD + CYAN, str(values[index]))}\n")
    for _ in range(shown - 1):
        out.write("\033[2K\n")
    out.write(f"\033[{shown - 1}A")
    out.flush()
    return values[index]


def render_confirm(message: str, yes: bool, st: Style) -> str:
    def pick(text, on):
        return st.c(BOLD + CYAN + UNDER, text) if on else st.c(DIM, text)
    return f"  {st.c(BOLD, message)}  {pick('Yes', yes)}  {pick('No', not yes)}  {st.c(DIM, '(left/right or Tab, Enter)')}"


def confirm(message: str, default: bool = False, *, readkey=None, out=None, st: Style | None = None) -> bool:
    """Yes / No on one line: left/right/Tab move the underline, Enter accepts, y and n answer directly."""
    out, readkey = out or sys.stdout, readkey or read_key
    st = st or default_style(out)
    yes = default
    out.write("\033[?25l")
    try:
        while True:
            out.write("\r\033[2K" + render_confirm(message, yes, st))
            out.flush()
            key = readkey()
            if key in ("left", "right", "tab", "shift_tab", "up", "down"):
                yes = not yes
            elif key in ("y", "Y"):
                yes = True
                break
            elif key in ("n", "N"):
                yes = False
                break
            elif key == "enter":
                break
            elif key in ("ctrl_c", "esc"):
                raise KeyboardInterrupt
    finally:
        out.write("\033[?25h")
    out.write("\r\033[2K" + f"  {st.c(GREEN, st.tick)} {message}  {st.c(BOLD + CYAN, 'Yes' if yes else 'No')}\n")
    out.flush()
    return yes
