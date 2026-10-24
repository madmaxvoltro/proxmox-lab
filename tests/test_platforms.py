"""macOS and Linux behaviour of the client: dialogs, autostart, Claude's config location. These run on every
operating system (they use fakes), so a Windows developer exercises the macOS code and vice versa."""

import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

from lab import client, connect, watch

ENTER = watch.SAMPLES["enter"]
DELETE = watch.SAMPLES["delete"]


class FakeProc:
    def __init__(self, code, out="", err="", polls=0):
        self.final, self.out, self.err, self.n = code, out, err, polls
        self.returncode, self.terminated = None, False

    def poll(self):
        self.n -= 1
        if self.n < 0 and not self.terminated:
            self.returncode = self.final
        return self.returncode

    def terminate(self):
        self.terminated, self.returncode = True, -15

    def wait(self):
        pass

    def communicate(self):
        return self.out, self.err


def popen_returning(proc, seen=None):
    def popen(cmd, **kw):
        if seen is not None:
            seen.append(cmd)
        return proc
    return popen


class MacDialogTest(unittest.TestCase):
    def test_script_makes_return_approve_and_escape_deny(self):
        s = watch.macos_script(ENTER)
        self.assertIn('buttons {"Deny", "Approve"}', s)
        self.assertIn('default button "Approve"', s)
        self.assertIn('cancel button "Deny"', s)
        self.assertIn("with icon note", s)
        self.assertIn("giving up after 540", s)
        self.assertIn("with icon caution", watch.macos_script(DELETE))  # dangerous requests get the warning icon

    def test_quotes_backslashes_and_newlines_cannot_break_out_of_the_script(self):
        item = {**ENTER, "plan": 'x" & (do shell script "id") & "', "project": 'a\\b"c'}
        s = watch.macos_script(item)
        body = s.split('display dialog "', 1)[1].split('" with title', 1)[0]
        self.assertNotRegex(body, r'(?<!\\)"')  # every quote inside the text is escaped
        self.assertNotIn("\n", s)  # one physical line for `osascript -e`

    def run_mac(self, proc, **kw):
        with mock.patch.object(watch.shutil, "which", return_value="/usr/bin/osascript"):
            return watch.show_macos(ENTER, kw.pop("alive", lambda: True), popen=popen_returning(proc),
                                    sleep=lambda s: None, **kw)

    def test_answers(self):
        self.assertIs(self.run_mac(FakeProc(0, "button returned:Approve, gave up:false")), True)
        self.assertIs(self.run_mac(FakeProc(0, "button returned:Deny, gave up:false")), False)
        self.assertIsNone(self.run_mac(FakeProc(0, "button returned:, gave up:true")))
        self.assertIs(self.run_mac(FakeProc(1, "", "execution error: User canceled. (-128)")), False)  # Esc
        with self.assertRaises(watch.PopupError):
            self.run_mac(FakeProc(1, "", "execution error: something else (-1)"))

    def test_taken_down_when_settled_elsewhere(self):
        proc = FakeProc(0, polls=10_000)
        t = [0.0]
        result = watch.show_macos(ENTER, lambda: False, popen=popen_returning(proc), poll=3.0,
                                  sleep=lambda s: t.__setitem__(0, t[0] + s), monotonic=lambda: t[0]) \
            if watch.shutil.which("osascript") else None
        if result is None and not proc.terminated:  # osascript absent here: run it with the lookup faked
            with mock.patch.object(watch.shutil, "which", return_value="/usr/bin/osascript"):
                proc = FakeProc(0, polls=10_000)
                t[0] = 0.0
                result = watch.show_macos(ENTER, lambda: False, popen=popen_returning(proc), poll=3.0,
                                          sleep=lambda s: t.__setitem__(0, t[0] + s), monotonic=lambda: t[0])
        self.assertIsNone(result)
        self.assertTrue(proc.terminated)

    def test_missing_osascript(self):
        with mock.patch.object(watch.shutil, "which", return_value=None):
            with self.assertRaises(watch.PopupError):
                watch.show_macos(ENTER, lambda: True)


class LinuxDialogTest(unittest.TestCase):
    def which(self, *present):
        return lambda name: f"/usr/bin/{name}" if name in present else None

    def test_zenity_is_preferred_and_labelled(self):
        with mock.patch.object(watch.shutil, "which", self.which("zenity", "kdialog")):
            cmd = watch.linux_command(DELETE)
        self.assertEqual(cmd[0], "zenity")
        for part in ("--no-markup", "--ok-label", "Approve", "--cancel-label", "Deny"):
            self.assertIn(part, cmd)
        self.assertIn("dialog-warning", cmd)  # delete is dangerous

    def test_kdialog_is_the_second_choice_and_nothing_is_an_error(self):
        with mock.patch.object(watch.shutil, "which", self.which("kdialog")):
            self.assertEqual(watch.linux_command(ENTER)[0], "kdialog")
        with mock.patch.object(watch.shutil, "which", self.which()):
            with self.assertRaises(watch.PopupError):
                watch.linux_command(ENTER)

    def run_linux(self, proc, tool="zenity"):
        with mock.patch.object(watch.shutil, "which", self.which(tool)), \
                mock.patch.dict(watch.os.environ, {"DISPLAY": ":0"}):
            return watch.show_linux(ENTER, lambda: True, popen=popen_returning(proc), sleep=lambda s: None)

    def test_exit_codes(self):
        self.assertIs(self.run_linux(FakeProc(0)), True)
        self.assertIs(self.run_linux(FakeProc(1)), False)
        self.assertIsNone(self.run_linux(FakeProc(5)))  # zenity --timeout elapsed
        self.assertIs(self.run_linux(FakeProc(1), tool="kdialog"), False)
        self.assertIs(self.run_linux(FakeProc(2), tool="kdialog"), False)  # Esc
        with self.assertRaises(watch.PopupError):
            self.run_linux(FakeProc(255, err="cannot open display"))

    def test_no_graphical_session(self):
        with mock.patch.dict(watch.os.environ, {}, clear=True):
            with self.assertRaisesRegex(watch.PopupError, "graphical"):
                watch.show_linux(ENTER, lambda: True)


class DispatchTest(unittest.TestCase):
    def test_each_platform_uses_its_own_dialog_then_falls_back_to_tk(self):
        calls = []
        patches = {name: mock.patch.object(watch, name, side_effect=lambda *a, _n=name, **k: calls.append(_n) or True)
                   for name in ("show_popup", "show_macos", "show_linux")}
        with patches["show_popup"], patches["show_macos"], patches["show_linux"]:
            for plat, want in (("win32", "show_popup"), ("darwin", "show_macos"), ("linux", "show_linux"), ("freebsd14", "show_linux")):
                calls.clear()
                with mock.patch.object(sys, "platform", plat):
                    self.assertIs(watch.show_best(ENTER, lambda: True), True)
                self.assertEqual(calls, [want], plat)

    def test_tk_is_the_fallback_and_total_failure_is_a_popup_error(self):
        with mock.patch.object(sys, "platform", "linux"), mock.patch.object(watch, "log"), \
                mock.patch.object(watch, "show_linux", side_effect=watch.PopupError("no zenity")), \
                mock.patch.object(watch, "show_dialog", return_value=False):
            self.assertIs(watch.show_best(ENTER, lambda: True), False)
        with mock.patch.object(sys, "platform", "linux"), mock.patch.object(watch, "log"), \
                mock.patch.object(watch, "show_linux", side_effect=watch.PopupError("no zenity")), \
                mock.patch.object(watch, "show_dialog", side_effect=RuntimeError("no display")):
            with self.assertRaises(watch.PopupError):
                watch.show_best(ENTER, lambda: True)

    def test_a_machine_that_cannot_show_anything_leaves_the_approval_pending(self):
        logs, decided = [], []
        watch.run({}, show=lambda item, alive: (_ for _ in ()).throw(watch.PopupError("no display")), once=True,
                  sleep=lambda s: None, logger=logs.append, pending_fn=lambda: [dict(ENTER)],
                  decide_fn=lambda a, b: decided.append((a, b)))
        self.assertEqual(decided, [])
        self.assertTrue(any("lab approve a0c371" in l for l in logs))  # tells you how to answer it instead


class AutostartTest(unittest.TestCase):
    def test_launch_agent_is_valid_xml_and_starts_at_login(self):
        text = watch.launch_agent_plist(["/Users/x/.local/bin/lab", "watch"], home=Path("/Users/x"))
        root = ET.fromstring(text.split("?>", 1)[1].replace(
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">', ""))
        strings = [e.text for e in root.iter("string")]
        self.assertIn(watch.LAUNCH_LABEL, strings)
        self.assertEqual(strings[1:3], ["/Users/x/.local/bin/lab", "watch"])
        self.assertEqual(len(list(root.iter("true"))), 2)  # RunAtLoad and KeepAlive

    def test_paths_with_special_characters_are_escaped(self):
        text = watch.launch_agent_plist(["/Users/a&b/<lab>", "watch"])
        self.assertIn("a&amp;b/&lt;lab&gt;", text)
        ET.fromstring(text.split("?>", 1)[1].replace(
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">', ""))

    def test_desktop_entry_quotes_the_command(self):
        text = watch.desktop_entry(["/home/me/My Apps/lab", "watch"])
        self.assertIn('Exec="/home/me/My Apps/lab" "watch"', text)
        self.assertIn("NoDisplay=true", text)
        self.assertIn('"a\\"b\\$c"', watch.desktop_entry(['a"b$c']))

    def test_where_each_platform_starts_things(self):
        home = Path("/home/u")
        self.assertEqual(watch.autostart_path("linux", home), home / ".config" / "autostart" / "lab-watch.desktop")
        self.assertEqual(watch.autostart_path("darwin", home),
                         home / "Library" / "LaunchAgents" / f"{watch.LAUNCH_LABEL}.plist")
        self.assertTrue(str(watch.autostart_path("win32", home)).endswith("Startup\\lab-approvals.vbs")
                        or str(watch.autostart_path("win32", home)).endswith("Startup/lab-approvals.vbs"))

    def test_install_and_uninstall_each_platform(self):
        for plat, needle in (("win32", "WScript.Shell"), ("darwin", "RunAtLoad"), ("linux", "[Desktop Entry]")):
            with tempfile.TemporaryDirectory() as d:
                with mock.patch.object(watch, "_launchctl") as launchctl:
                    path = watch.install(Path(d), platform=plat)
                    self.assertIn(needle, path.read_text(encoding="utf-8"), plat)
                    self.assertTrue(watch.uninstall(Path(d), platform=plat))
                    self.assertFalse(watch.uninstall(Path(d), platform=plat))
                    launchctl.assert_not_called()  # a test folder never touches the real launchd

    def test_macos_install_registers_with_launchd(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(watch, "_launchctl") as launchctl, \
                mock.patch.object(watch.Path, "home", return_value=Path(d)), \
                mock.patch.object(watch.os, "getuid", create=True, return_value=501):
            path = watch.install(platform="darwin")
            self.assertEqual(path.parent, Path(d) / "Library" / "LaunchAgents")
            launchctl.assert_called_once_with("bootstrap", "gui/501", str(path))
            watch.uninstall(platform="darwin")
            launchctl.assert_called_with("bootout", f"gui/501/{watch.LAUNCH_LABEL}")
            self.assertFalse(path.exists())


class ClaudeDesktopLocationTest(unittest.TestCase):
    def test_macos_and_linux(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            self.assertIsNone(connect.find_claude_desktop_config({}, "darwin", home))  # not installed
            (home / "Library" / "Application Support" / "Claude").mkdir(parents=True)
            self.assertEqual(connect.find_claude_desktop_config({}, "darwin", home),
                             home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json")
            self.assertIsNone(connect.find_claude_desktop_config({}, "linux", home))
            (home / ".config" / "Claude").mkdir(parents=True)
            self.assertEqual(connect.find_claude_desktop_config({}, "linux", home),
                             home / ".config" / "Claude" / "claude_desktop_config.json")


class CheckRowTest(unittest.TestCase):
    def row(self, plat, found):
        with mock.patch.object(sys, "platform", plat), mock.patch.object(client.shutil, "which", self.which(found)):
            return client.popup_row(watch)

    @staticmethod
    def which(found):
        return lambda name: f"/usr/bin/{name}" if name in found else None

    def test_each_platform_describes_how_approvals_will_appear(self):
        name, text, ok, required = self.row("darwin", ["osascript"])
        self.assertEqual((ok, required, "macOS" in text), (True, False, True))
        self.assertFalse(self.row("darwin", [])[2])
        self.assertIn("zenity", self.row("linux", ["zenity"])[1])
        name, text, ok, required = self.row("linux", [])
        self.assertEqual((ok, required), (False, False))  # a warning, never a failure: `lab approve` still works
        self.assertEqual(self.row("win32", [])[3], True)  # on Windows the bundled script is required


if __name__ == "__main__":
    unittest.main()
