import io
import re
import sys
import unittest
import urllib.request

from lab import progress
from lab.errors import PreconditionError
from lab.hostio import HostIO
from lab.progress import Reporter, TtyReporter, bytes_cb, human_bytes, human_time
from lab.proxmox import Proxmox, ProxmoxError
from lab.runner import DryRunRunner, SubprocessRunner

from .fake_io import FakeIO
from .fake_proxmox import FakeProxmox
from .test_m1 import CATALOGUE, make_cfg, make_io
from .test_m2 import Base
from lab.db import DB
from lab.template import TemplateBuilder

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t

    def tick(self, s):
        self.t += s


class PlainReporterTest(unittest.TestCase):
    def setUp(self):
        self.out, self.clock = io.StringIO(), Clock()
        self.r = Reporter(self.out, self.clock)

    def lines(self):
        return self.out.getvalue().splitlines()

    def test_steps_are_numbered_once_planned(self):
        self.r("before any plan")
        self.r.plan(3)
        self.r("one")
        self.r("two")
        self.assertEqual(self.lines(), ["before any plan", "[1/3] one", "[2/3] two"])

    def test_percent_lines_print_once_per_ten_percent(self):
        self.r("backup")
        for p in (1, 3, 9, 11, 14, 19, 21, 55, 56, 100):
            self.r.line(f"INFO: {p}% (x of y)")
        printed = [l for l in self.lines() if "%" in l]
        self.assertEqual(len(printed), 5)  # 0-9, 10-19, 20-29, 50-59, 100
        self.assertEqual(printed[-1].strip(), "INFO: 100% (x of y)")

    def test_other_output_lines_always_print_and_blank_ones_do_not(self):
        self.r("restore")
        self.r.line("   ")
        self.r.line("INFO: starting restore")
        self.assertEqual(self.lines(), ["restore", "    INFO: starting restore"])

    def test_waits_print_at_most_every_30_seconds(self):
        self.r("boot")
        for elapsed in range(0, 100, 3):
            self.clock.tick(3)
            self.r.wait("Waiting for the guest agent", elapsed, 300)
        self.assertLessEqual(len([l for l in self.lines() if "Waiting" in l]), 4)
        self.assertIn("gives up after 5:00", self.out.getvalue())

    def test_failure_is_marked(self):
        self.r("risky")
        self.r.finish(ok=False)
        self.assertIn("FAILED: risky", self.out.getvalue())
        self.r.finish(ok=False)  # idempotent
        self.assertEqual(self.out.getvalue().count("FAILED"), 1)

    def test_percent_ignores_nonsense_values(self):
        self.r("x")
        self.r.line("CPU 4500% busy")
        self.assertIsNone(self.r.pct)


class TtyReporterTest(unittest.TestCase):
    def setUp(self):
        self.out, self.clock = io.StringIO(), Clock()
        self.r = TtyReporter(self.out, self.clock, width=60, color=False, utf=True)

    def screen(self):
        """Replay the cursor-up / clear sequences like a terminal would; return the visible lines."""
        screen = []
        data = self.out.getvalue()
        for m in re.finditer(r"\x1b\[(\d+)A\x1b\[J|([^\x1b]+)", data):
            if m.group(1):
                del screen[-int(m.group(1)):]
            else:
                screen += m.group(2).split("\n")[:-1] if m.group(2).endswith("\n") else m.group(2).split("\n")
        return screen

    def test_running_step_shows_header_bar_and_a_four_line_window(self):
        self.r.plan(2)
        self.r("Backing up api")
        for i in range(10):
            self.clock.tick(1)
            self.r.detail(f"line {i}")
        self.clock.tick(1)
        self.r.percent(42, "INFO: 42% (3 GiB of 7 GiB)", key="out")
        screen = self.screen()
        self.assertIn("[1/2] Backing up api", screen[0])
        self.assertIn("42%", screen[0])
        self.assertEqual(len(screen), 5)  # header + the window of 4
        self.assertEqual([s.strip() for s in screen[1:]], ["line 7", "line 8", "line 9", "INFO: 42% (3 GiB of 7 GiB)"])

    def test_keyed_lines_replace_instead_of_scrolling(self):
        self.r("boot")
        for s in range(0, 60, 5):
            self.clock.tick(5)
            self.r.wait("Waiting for the guest agent", s, 300)
        waits = [l for l in self.screen() if "Waiting" in l]
        self.assertEqual(len(waits), 1)
        self.assertIn("55s", waits[0])

    def test_finished_steps_collapse_to_one_line_with_time(self):
        self.r.plan(2)
        self.r("Cloning")
        self.r.detail("transferred 1 GiB")
        self.clock.tick(12)
        self.r("Booting")
        screen = self.screen()
        self.assertEqual(screen[0].strip(), "✔ [1/2] Cloning (12s)")
        self.assertIn("[2/2] Booting", screen[1])
        self.assertNotIn("transferred", "\n".join(screen))

    def test_failure_keeps_the_last_output_on_screen(self):
        self.r("Trial restore")
        self.r.detail("first")
        self.r.detail("restore exploded: no space")
        self.r.finish(ok=False)
        screen = self.screen()
        self.assertTrue(screen[0].startswith("✘ Trial restore"))
        self.assertIn("restore exploded: no space", "\n".join(screen))

    def test_long_lines_are_cut_to_the_terminal_width(self):
        self.r("x")
        self.r.detail("y" * 500)
        for line in self.screen():
            self.assertLessEqual(len(line), 59)

    def test_ascii_fallback_without_utf8(self):
        r = TtyReporter(self.out, self.clock, width=60, color=False, utf=False)
        r("step")
        r.percent(50)
        r.finish()
        self.assertFalse([c for c in self.out.getvalue() if ord(c) > 127])

    def test_redraws_are_throttled(self):
        self.r("step")
        before = self.out.getvalue().count("\x1b[")
        for i in range(200):
            self.r.detail(f"d{i}")  # no clock movement
        self.assertLess(self.out.getvalue().count("\x1b[") - before, 3)

    def test_color_can_be_turned_off(self):
        self.r("step")
        self.r.finish()
        self.assertFalse(re.search(r"\x1b\[\d+m", self.out.getvalue()))
        r = TtyReporter(io.StringIO(), self.clock, width=60, color=True)
        r("step")
        r.finish()
        self.assertTrue(re.search(r"\x1b\[32m", r.stream.getvalue()))

    def test_make_chooses_by_terminal(self):
        class Tty(io.StringIO):
            def isatty(self):
                return True
        self.assertIsInstance(progress.make(Tty()), TtyReporter) if sys.platform != "none" else None
        self.assertNotIsInstance(progress.make(io.StringIO()), TtyReporter)
        self.assertNotIsInstance(progress.make(Tty(), plain=True), TtyReporter)


class HelperTest(unittest.TestCase):
    def test_plain_callables_are_accepted_everywhere(self):
        say = lambda m: None
        progress.plan(say, 3)
        progress.detail(say, "x")
        self.assertIsNone(progress.line_cb(say))
        self.assertIsNone(progress.wait_cb(say))
        self.assertIsNone(bytes_cb(say, "downloaded"))

    def test_formatting(self):
        self.assertEqual(human_bytes(512), "512 B")
        self.assertEqual(human_bytes(1536), "1.5 KiB")
        self.assertEqual(human_bytes(5 * 1024 ** 3), "5.0 GiB")
        self.assertEqual((human_time(7), human_time(75), human_time(3600)), ("7s", "1:15", "60:00"))

    def test_bytes_callback_reports_percent_and_sizes(self):
        out = io.StringIO()
        r = Reporter(out)
        r("download")
        cb = bytes_cb(r, "downloaded")
        cb(1 << 29, 1 << 30)
        self.assertEqual(r.pct, 50.0)
        self.assertIn("downloaded 512.0 MiB of 1.0 GiB", out.getvalue())
        cb(10, None)  # unknown size: text only
        self.assertEqual(r.pct, 50.0)


class StreamingRunnerTest(unittest.TestCase):
    def test_lines_arrive_live_and_output_is_still_returned(self):
        code = ("import sys,time\n"
                "for i in (10,50,100):\n"
                "    print(f'INFO: {i}% done'); sys.stdout.flush(); time.sleep(0.05)\n"
                "print('warning', file=sys.stderr)\n"
                "sys.exit(3)\n")
        seen = []
        res = SubprocessRunner().run([sys.executable, "-c", code], check=False, on_line=seen.append)
        self.assertEqual(res.returncode, 3)
        self.assertEqual(seen.count("INFO: 50% done"), 1)
        self.assertIn("warning", seen)
        self.assertEqual(res.stdout.splitlines(), ["INFO: 10% done", "INFO: 50% done", "INFO: 100% done"])
        self.assertEqual(res.stderr.strip(), "warning")

    def test_carriage_return_progress_counts_as_lines(self):
        code = "import sys\nsys.stdout.write('1%\\r50%\\r100%\\n'); sys.stdout.flush()\n"
        seen = []
        SubprocessRunner().run([sys.executable, "-c", code], on_line=seen.append)
        self.assertEqual([s for s in seen if s], ["1%", "50%", "100%"])

    def test_stdin_and_failure_still_work_when_streaming(self):
        res = SubprocessRunner().run([sys.executable, "-c", "import sys; print(sys.stdin.read().upper())"],
                                     input="secret", on_line=lambda l: None)
        self.assertEqual(res.stdout.strip(), "SECRET")
        from lab.errors import CommandError
        with self.assertRaises(CommandError):
            SubprocessRunner().run([sys.executable, "-c", "raise SystemExit(2)"], on_line=lambda l: None)

    def test_broken_callback_never_breaks_the_command(self):
        def boom(line):
            raise RuntimeError("display bug")
        res = SubprocessRunner().run([sys.executable, "-c", "print('hi')"], on_line=boom)
        self.assertEqual(res.stdout.strip(), "hi")

    def test_timeout_kills_the_command(self):
        import subprocess
        with self.assertRaises(subprocess.TimeoutExpired):
            SubprocessRunner().run([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.5,
                                   on_line=lambda l: None)

    def test_dry_run_forwards_the_callback_only_for_read_only_commands(self):
        class Inner:
            def __init__(self):
                self.got = []

            def run(self, argv, **kw):
                self.got.append(kw.get("on_line"))
                from lab.runner import Result
                return Result(argv)
        inner = Inner()
        dry = DryRunRunner(inner)
        dry.run(["qm", "config", "1"], on_line=print)
        dry.run(["qm", "clone", "1", "2"], on_line=print)
        self.assertEqual(inner.got, [print])
        self.assertEqual(len(dry.skipped), 1)


class DownloadAndHashProgressTest(unittest.TestCase):
    def test_download_reports_progress_and_hash_reports_progress(self):
        import hashlib
        import tempfile
        from pathlib import Path
        payload = b"x" * (3 * (1 << 20) + 5)

        class Resp(io.BytesIO):
            headers = {"Content-Length": str(len(payload))}

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        real = urllib.request.urlopen
        urllib.request.urlopen = lambda req, timeout=None: Resp(payload)
        try:
            with tempfile.TemporaryDirectory() as d:
                dest, ticks = str(Path(d) / "img"), []
                HostIO().fetch_file("http://x/img", dest, on_progress=lambda a, b: ticks.append((a, b)))
                self.assertEqual(Path(dest).read_bytes(), payload)
                self.assertEqual(ticks[-1], (len(payload), len(payload)))
                self.assertEqual(len(ticks), 4)
                self.assertFalse(Path(dest + ".part").exists())
                hticks = []
                digest = HostIO().sha256(dest, on_progress=lambda a, b: hticks.append(a / b))
                self.assertEqual(digest, hashlib.sha256(payload).hexdigest())
                self.assertEqual(hticks[-1], 1.0)
        finally:
            urllib.request.urlopen = real


class WaitingTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeProxmox()
        self.fake.add_vm(100, "vm", status="running")
        self.px = Proxmox(self.fake)
        self.t = [0.0]

    def clock(self):
        return self.t[0]

    def sleeper(self, s):
        self.t[0] += s

    def test_agent_wait_reports_elapsed_time(self):
        self.fake.guest_agent_ready = False
        ticks = []
        with self.assertRaises(ProxmoxError):
            self.px.wait_agent(100, 20, 5, sleep=self.sleeper, monotonic=self.clock,
                               on_wait=lambda what, e, t: ticks.append((what, e, t)))
        self.assertEqual([t[1] for t in ticks], [0, 5, 10, 15, 20])
        self.assertEqual(ticks[0][2], 20)

    def test_cloud_init_wait_shows_the_log_tail_until_done(self):
        states = iter(["status: running", "status: running", "status: done"])
        self.fake.guest_results.insert(0, (lambda c: c.startswith("cloud-init status"),
                                           {"exitcode": 0, "out-data": next(states)}))
        calls = {"n": 0}

        def pred(c):
            if c.startswith("cloud-init status"):
                calls["n"] += 1
            return False
        # script the replies: running, running, done
        self.fake.guest_results.clear()
        replies = ["status: running", "status: running", "status: done"]
        self.fake.guest_results.append((lambda c: c.startswith("cloud-init status"),
                                        None))

        class Seq(list):
            pass
        seq = list(replies)
        orig = self.fake._qm

        def qm(a):
            cmd = " ".join(a[a.index("--") + 1:]) if "--" in a else ""
            if cmd.startswith("cloud-init status"):
                import json
                from lab.runner import Result
                out = seq.pop(0) if len(seq) > 1 else seq[0]
                return Result(a, 0, json.dumps({"exited": 1, "exitcode": 0, "out-data": out}))
            if cmd.startswith("tail -n 1"):
                import json
                from lab.runner import Result
                return Result(a, 0, json.dumps({"exited": 1, "exitcode": 0, "out-data": "Setting up git (1:2.43)\n"}))
            return orig(a)
        self.fake._qm = qm
        self.fake.guest_results.clear()
        details, waits = [], []
        self.px.wait_cloud_init(100, 60, 5, sleep=self.sleeper, monotonic=self.clock,
                                on_wait=lambda *a: waits.append(a), on_detail=lambda t, k: details.append((t, k)))
        self.assertEqual(details[0], ("Setting up git (1:2.43)", "log"))
        self.assertEqual(len(waits), 2)

    def test_cloud_init_error_and_timeout(self):
        self.fake.guest_results.insert(0, (lambda c: c.startswith("cloud-init status"),
                                           {"exitcode": 1, "out-data": "status: error", "err-data": "boom"}))
        with self.assertRaisesRegex(ProxmoxError, "boom"):
            self.px.wait_cloud_init(100, 60, 5, sleep=self.sleeper, monotonic=self.clock)
        self.fake.guest_results.clear()
        self.fake.guest_results.insert(0, (lambda c: c.startswith("cloud-init status"),
                                           {"exitcode": 0, "out-data": "status: running"}))
        with self.assertRaisesRegex(ProxmoxError, "did not finish within 30s"):
            self.px.wait_cloud_init(100, 30, 5, sleep=self.sleeper, monotonic=self.clock)

    def test_degraded_cloud_init_counts_as_finished(self):
        self.fake.guest_results.insert(0, (lambda c: c.startswith("cloud-init status"),
                                           {"exitcode": 2, "out-data": "status: degraded done"}))
        self.px.wait_cloud_init(100, 30, 5, sleep=self.sleeper, monotonic=self.clock)


class WiringTest(Base):
    """The real operations feed the reporter: steps, percentages from command output, timings."""

    def setUp(self):
        super().setUp()
        self.out = io.StringIO()
        self.rep = Reporter(self.out)

    def lab(self, caller=None):
        lab = super().lab(caller)
        lab.say = self.rep
        lab.templates.say = self.rep
        return lab

    def text(self):
        self.rep.finish()
        return self.out.getvalue()

    def test_create_steps_and_clone_progress(self):
        self.fake.line_script.append((lambda a: a[:2] == ["qm", "clone"],
                                      ["transferred 3.2 GiB of 32.0 GiB (10.0%)", "transferred 32.0 GiB of 32.0 GiB (100.0%)"]))
        self.lab().create("api")
        text = self.text()
        for step in ("[1/5] Cloning the template", "[2/5] Configuring the VM", "[3/5] Booting",
                     "[4/5] First-boot setup", "[5/5] Joining the tailnet"):
            self.assertIn(step, text)
        self.assertIn("100.0%", text)
        self.assertIn("lab-api.tail1234.ts.net", text)
        self.assertTrue(self.fake.streamed[[c[:2] for c in self.fake.calls].index(["qm", "clone"])])

    def test_without_a_reporter_nothing_streams(self):
        super().lab().create("api")  # plain lambda say: the quick non-streaming path
        clone = [i for i, c in enumerate(self.fake.calls) if c[:2] == ["qm", "clone"]][0]
        self.assertFalse(self.fake.streamed[clone])

    def test_archive_and_restore_steps_with_backup_percentages(self):
        self.lab().create("api")
        self.fake.line_script.append((lambda a: a[0] == "vzdump", ["INFO: 25% (8.0 GiB of 32.0 GiB) in 10s", "INFO: 100% done"]))
        self.fake.line_script.append((lambda a: a[0] == "qmrestore", ["progress 50% (read 1 bytes)"]))
        self.rep.finish()
        self.out.truncate(0)
        self.out.seek(0)
        self.lab().archive("api")
        text = self.text()
        for step in ("[1/6] Stopping api", "[2/6] Backing up api", "[3/6] Verifying the archive",
                     "[4/6] Trial restore", "[5/6] Boot test", "[6/6] Removing api from the fast disk"):
            self.assertIn(step, text)
        self.assertIn("INFO: 25%", text)
        self.assertIn("progress 50%", text)
        self.out.truncate(0)
        self.out.seek(0)
        self.lab().restore("api")
        text = self.text()
        self.assertIn("[1/4] Restoring api", text)
        self.assertIn("[4/4] Rejoining the tailnet", text)

    def test_failed_archive_shows_the_failing_step(self):
        self.lab().create("api")
        self.fake.fail("qmrestore", stderr="no space")
        self.rep.finish()
        self.out.truncate(0)
        self.out.seek(0)
        with self.assertRaises(Exception):
            self.lab().archive("api")
        self.rep.finish(ok=False)  # what the CLI does when a command raises
        self.assertIn("FAILED: Trial restore", self.out.getvalue())


class InterruptTest(unittest.TestCase):
    def test_ctrl_c_exits_cleanly_without_a_traceback(self):
        from lab import cli
        real = cli._run
        cli._run = lambda ctx, args: (_ for _ in ()).throw(KeyboardInterrupt())
        err = io.StringIO()
        old = sys.stderr
        sys.stderr = err
        try:
            rc = cli.main(["--config", "does-not-exist.toml", "version"])
        finally:
            sys.stderr = old
            cli._run = real
        self.assertEqual(rc, 130)
        self.assertIn("interrupted", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())


class TemplateWiringTest(unittest.TestCase):
    def test_template_build_reports_every_stage_with_download_progress(self):
        fake, fio = FakeProxmox(), make_io()
        out = io.StringIO()
        rep = Reporter(out)
        tb = TemplateBuilder(make_cfg(), Proxmox(fake), fio, DB(":memory:"), catalogue=CATALOGUE,
                             progress=rep, sleep=lambda s: None)
        tb.build("ubuntu-24.04")
        rep.finish()
        text = out.getvalue()
        for n, step in enumerate(("Fetching the published checksum", "Getting the cloud image", "Creating the build VM",
                                  "Importing the image into nvme", "Configuring cloud-init", "Booting the build VM",
                                  "Installing packages", "Checking the toolchain", "Generalising the image",
                                  "Shutting down", "Converting to a template"), start=1):
            self.assertIn(f"[{n}/11] {step}", text)
        self.assertIn("downloaded", text)
        self.assertIn("is ready", text)


if __name__ == "__main__":
    unittest.main()
