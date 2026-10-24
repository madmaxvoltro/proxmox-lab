"""Progress display for slow commands (template builds, backups, restores, VM creation).

Library code reports through a *reporter* and never prints. The reporter is callable (`say("Cloning")`
starts a new step) and has `detail`, `percent`, `line`, `wait` and `plan`. Plain callables are still
accepted everywhere: the helper functions at the bottom do nothing for them.

Two renderers:
  TtyReporter    docker-build style. Finished steps collapse to one line (`✔ Cloning (12s)`); the
                 running step shows a header with a bar and elapsed time plus a window of its last
                 few detail lines, redrawn in place.
  Reporter       plain lines, for logs, pipes, `--json` runs and the MCP server. Percentages are
                 printed only when they cross a 10% step, waits at most every 30 s.
Everything goes to stderr so stdout stays clean for `--json` and for the MCP protocol."""

import os
import re
import shutil
import sys
import threading
import time
from collections import deque

_PCT = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*%")
_NOISE = re.compile(r"^(Logical volume \S+ (created|changed|successfully resized)\.?|Rounding up size .*)$")
WINDOW = 4


def human_bytes(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024 or unit == "TiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def human_time(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 60}:{s % 60:02d}" if s >= 60 else f"{s}s"


class Reporter:
    """Plain-text reporter; also the state machine the TTY renderer builds on."""

    def __init__(self, stream=None, clock=time.monotonic):
        self.stream = stream or sys.stderr
        self.clock = clock
        self.lock = threading.RLock()
        self.total = None
        self.index = 0
        self.title = None
        self.details = deque(maxlen=WINDOW)  # [(key, text)]
        self.pct = None
        self.t0 = 0.0
        self.warnings = {}  # normalised text -> first occurrence
        self._last_decile = -1
        self._last_wait_print = -1e9

    # steps -------------------------------------------------------------------------------
    def plan(self, total: int) -> None:
        """Declare how many steps follow (shown as [n/total]); restarts numbering."""
        with self.lock:
            self._end(True)
            self.total, self.index = total, 0

    def __call__(self, title: str) -> None:
        with self.lock:
            self._end(True)
            self.index += 1
            self.title, self.t0, self.pct = title, self.clock(), None
            self.details.clear()
            self._last_decile, self._last_wait_print = -1, -1e9
            self._started()

    def finish(self, ok: bool = True) -> None:
        with self.lock:
            self._end(ok)
            if self.warnings:
                for text in self.warnings.values():
                    self.stream.write(f"note: {text.removeprefix('WARNING:').strip()}\n")
                self.stream.flush()
                self.warnings = {}

    def _end(self, ok: bool) -> None:
        if self.title is None:
            return
        elapsed = self.clock() - self.t0
        self._ended(ok, elapsed)
        self.title = None

    # updates ------------------------------------------------------------------------------
    def detail(self, text: str, key: str | None = None) -> None:
        """One line of context for the running step. A repeated `key` replaces its earlier line."""
        text = (text or "").strip()
        if not text:
            return
        with self.lock:
            if self.title is None:
                self("")
            if key is not None:
                for i, (k, _) in enumerate(self.details):
                    if k == key:
                        self.details[i] = (key, text)
                        break
                else:
                    self.details.append((key, text))
            else:
                self.details.append((None, text))
            self._changed(text, key)

    def percent(self, pct: float | None, text: str | None = None, key: str = "pct") -> None:
        with self.lock:
            if pct is not None:
                self.pct = max(0.0, min(100.0, float(pct)))
            if text:
                self.detail(text, key)
            else:
                self._changed(None, key)

    def line(self, raw: str) -> None:
        """A line of command output (vzdump, qmrestore, qm clone ...). Progress lines update the
        percentage and replace each other; anything else scrolls through the window. Routine
        storage chatter is dropped and warnings are collected and shown once, at the end."""
        raw = (raw or "").strip()
        if not raw or _NOISE.match(raw):
            return
        if raw.startswith("WARNING"):
            self.warnings.setdefault(re.sub(r"[\d.]+", "#", raw), raw)
            return
        m = _PCT.search(raw)
        if m and float(m.group(1)) <= 100:
            self.percent(float(m.group(1)), raw, key="out")
        else:
            self.detail(raw)

    def wait(self, what: str, elapsed: float, timeout: float) -> None:
        self.detail(f"{what}... {human_time(elapsed)} (gives up after {human_time(timeout)})", key="wait")

    # hooks (plain output) ------------------------------------------------------------------------
    def _label(self) -> str:
        return f"[{self.index}/{self.total}] " if self.total else ""

    def _started(self) -> None:
        if self.title:
            self.stream.write(f"{self._label()}{self.title}\n")
            self.stream.flush()

    def _changed(self, text, key) -> None:
        if text is None:
            return
        if key == "wait":
            if self.clock() - self._last_wait_print < 30:
                return
            self._last_wait_print = self.clock()
        elif self.pct is not None and key in ("pct", "out"):
            decile = int(self.pct // 10)
            if decile == self._last_decile:
                return
            self._last_decile = decile
        self.stream.write(f"    {text}\n")
        self.stream.flush()

    def _ended(self, ok: bool, elapsed: float) -> None:
        if not ok and self.title:
            self.stream.write(f"    FAILED: {self.title}\n")
            self.stream.flush()


class TtyReporter(Reporter):
    SPIN_UTF = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    SPIN_ASCII = "|/-\\"

    def __init__(self, stream=None, clock=time.monotonic, width=None, color=None, utf=None):
        super().__init__(stream, clock)
        enc = (getattr(self.stream, "encoding", None) or "").lower()
        self.utf = ("utf" in enc) if utf is None else utf
        self.spin = self.SPIN_UTF if self.utf else self.SPIN_ASCII
        self.glyph = ("✔", "✘", "━", "╌") if self.utf else ("+", "x", "#", "-")
        self.color = (not os.environ.get("NO_COLOR")) if color is None else color
        self.fixed_width = width
        self._frame = 0
        self._drawn = 0
        self._last_draw = -1e9

    def _width(self) -> int:
        return self.fixed_width or shutil.get_terminal_size((100, 24)).columns

    def _c(self, code: str, s: str) -> str:
        return f"\x1b[{code}m{s}\x1b[0m" if self.color else s

    def _fit(self, s: str) -> str:
        w = self._width() - 1
        return s if len(s) <= w else s[: w - 1] + "…"

    def _bar(self, pct: float, width: int = 20) -> str:
        full = int(round(width * pct / 100))
        return self.glyph[2] * full + self.glyph[3] * (width - full)

    def _clear(self) -> None:
        if self._drawn:
            self.stream.write(f"\x1b[{self._drawn}A\x1b[J")
            self._drawn = 0

    def _draw(self, force: bool = False) -> None:
        now = self.clock()
        if not force and now - self._last_draw < 0.08:
            return
        self._last_draw = now
        self._frame += 1
        head = f"{self.spin[self._frame % len(self.spin)]} {self._label()}{self.title}"
        if self.pct is not None:
            head += f"  {self._bar(self.pct)} {self.pct:3.0f}%"
        head += f"  {human_time(now - self.t0)}"
        lines = [self._c("1", self._fit(head))] + [self._c("2", self._fit("    " + t)) for _, t in self.details]
        self._clear()
        self.stream.write("\n".join(lines) + "\n")
        self.stream.flush()
        self._drawn = len(lines)

    def _started(self) -> None:
        if self.title:
            self._draw(force=True)

    def _changed(self, text, key) -> None:
        if self.title:
            self._draw()

    def _ended(self, ok: bool, elapsed: float) -> None:
        keep = list(self.details) if not ok else []
        self._clear()
        if self.title:
            mark = self._c("32", self.glyph[0]) if ok else self._c("31", self.glyph[1])
            self.stream.write(self._fit(f"{mark} {self._label()}{self.title} ({human_time(elapsed)})") + "\n")
            for _, t in keep:  # on failure the last output stays on screen
                self.stream.write(self._c("2", self._fit("    " + t)) + "\n")
            self.stream.flush()


def make(stream=None, plain: bool = False) -> Reporter:
    """A TtyReporter when stderr is an interactive terminal, else plain lines."""
    stream = stream or sys.stderr
    interactive = (hasattr(stream, "isatty") and stream.isatty() and os.environ.get("TERM") != "dumb"
                   and not os.environ.get("LAB_PLAIN") and not plain)
    return TtyReporter(stream) if interactive else Reporter(stream)


# helpers for code that may be handed a plain callable instead of a Reporter ----------------------------
def plan(say, total: int) -> None:
    fn = getattr(say, "plan", None)
    if fn:
        fn(total)


def detail(say, text: str, key: str | None = None) -> None:
    fn = getattr(say, "detail", None)
    if fn:
        fn(text, key)


def line_cb(say):
    """Callback for Runner.run(on_line=...), or None (which keeps the quick non-streaming path)."""
    return getattr(say, "line", None)


def wait_cb(say):
    return getattr(say, "wait", None)


def bytes_cb(say, label: str = ""):
    """Callback(done, total) for downloads and hashing; None for plain callables."""
    fn = getattr(say, "percent", None)
    if fn is None:
        return None

    def cb(done: int, total: int | None) -> None:
        prefix = f"{label} " if label else ""
        if total:
            fn(100 * done / total, f"{prefix}{human_bytes(done)} of {human_bytes(total)}", "pct")
        else:
            fn(None, f"{prefix}{human_bytes(done)}", "pct")
    return cb
