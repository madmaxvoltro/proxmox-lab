"""The arrow-key menus and the banner. Keys are fed in as names, so this runs without a terminal."""

import io
import os
import unittest
from unittest import mock

from lab import prompt, ui

OPTIONS = [("nvme", "fast, 900 GB free"), ("bigdisk", "slow, 4 TB free"), ("local", "")]
PLAIN = ui.Style(color=False, utf=True)


def keys(*names):
    it = iter(names)
    return lambda: next(it)


def run_select(*names, default=None):
    out = io.StringIO()
    value = ui.select("Archive disk", OPTIONS, default, readkey=keys(*names), out=out, st=PLAIN)
    return value, out.getvalue()


class SelectTest(unittest.TestCase):
    def test_enter_takes_the_first_option_or_the_default(self):
        self.assertEqual(run_select("enter")[0], "nvme")
        self.assertEqual(run_select("enter", default="bigdisk")[0], "bigdisk")

    def test_arrows_and_tab_move_and_wrap(self):
        self.assertEqual(run_select("down", "enter")[0], "bigdisk")
        self.assertEqual(run_select("down", "down", "up", "enter")[0], "bigdisk")
        self.assertEqual(run_select("tab", "tab", "enter")[0], "local")
        self.assertEqual(run_select("up", "enter")[0], "local")  # wraps from the top
        self.assertEqual(run_select("tab", "tab", "tab", "enter")[0], "nvme")  # and from the bottom
        self.assertEqual(run_select("down", "shift_tab", "enter")[0], "nvme")

    def test_other_keys_do_nothing(self):
        self.assertEqual(run_select("x", "left", "7", "enter")[0], "nvme")

    def test_escape_and_ctrl_c_choose_nothing(self):
        for key in ("esc", "ctrl_c"):
            with self.assertRaises(KeyboardInterrupt):
                run_select("down", key)

    def test_the_selected_option_is_underlined_and_the_cursor_comes_back(self):
        out = io.StringIO()
        ui.select("Archive disk", OPTIONS, None, readkey=keys("down", "enter"), out=out,
                  st=ui.Style(color=True, utf=True))
        text = out.getvalue()
        self.assertIn(ui.UNDER, text)
        self.assertIn(f"{ui.UNDER}bigdisk", text.replace(ui.RESET, "").replace(ui.CYAN, "").replace(ui.BOLD, ""))
        self.assertTrue(text.rstrip().endswith("\033[?25h") or "\033[?25h" in text)  # cursor shown again
        self.assertIn("Archive disk", text.splitlines()[-1] if text.endswith("\n") else text)

    def test_render_marks_exactly_one_option_with_the_pointer(self):
        lines = ui.render_menu("Pick", OPTIONS, 1, PLAIN)
        self.assertEqual(sum(l.count("❯") for l in lines), 1)
        self.assertIn("❯ bigdisk", lines[2])
        self.assertIn("fast, 900 GB free", lines[1])  # descriptions are kept
        ascii_lines = ui.render_menu("Pick", OPTIONS, 0, ui.Style(color=False, utf=False))
        self.assertIn("> nvme", ascii_lines[1])


class ConfirmTest(unittest.TestCase):
    def ask(self, *names, default=False):
        return ui.confirm("Build it now?", default, readkey=keys(*names), out=io.StringIO(), st=PLAIN)

    def test_enter_keeps_the_default(self):
        self.assertIs(self.ask("enter", default=True), True)
        self.assertIs(self.ask("enter", default=False), False)

    def test_arrows_and_tab_flip_it(self):
        self.assertIs(self.ask("right", "enter", default=False), True)
        self.assertIs(self.ask("tab", "enter", default=True), False)
        self.assertIs(self.ask("left", "left", "enter", default=True), True)

    def test_y_and_n_answer_at_once(self):
        self.assertIs(self.ask("y"), True)
        self.assertIs(self.ask("N", default=True), False)

    def test_escape_is_not_an_answer(self):
        with self.assertRaises(KeyboardInterrupt):
            self.ask("esc")


class FallbackTest(unittest.TestCase):
    def test_a_pipe_gets_the_numbered_list_and_the_menu_is_not_used(self):
        tp = prompt.TerminalPrompter()
        with mock.patch.object(ui, "interactive", return_value=False), \
                mock.patch("builtins.input", return_value="2"), mock.patch("builtins.print"):
            self.assertEqual(tp.choose("Disk", OPTIONS), "bigdisk")
        with mock.patch.object(ui, "interactive", return_value=False), mock.patch("builtins.input", return_value="y"):
            self.assertIs(tp.confirm("Sure?"), True)

    def test_a_terminal_gets_the_menu(self):
        tp = prompt.TerminalPrompter()
        with mock.patch.object(ui, "interactive", return_value=True), \
                mock.patch.object(ui, "select", return_value="local") as sel, mock.patch("builtins.print"):
            self.assertEqual(tp.choose("Disk", OPTIONS, "nvme"), "local")
            sel.assert_called_once_with("Disk", OPTIONS, "nvme")
        with mock.patch.object(ui, "interactive", return_value=True), mock.patch.object(ui, "confirm", return_value=False):
            self.assertIs(tp.confirm("Sure?", True), False)

    def test_interactive_needs_two_real_terminals(self):
        tty, pipe = mock.Mock(isatty=lambda: True), mock.Mock(isatty=lambda: False)
        with mock.patch.dict(os.environ, {"TERM": "xterm"}):
            self.assertTrue(ui.interactive(tty, tty))
            self.assertFalse(ui.interactive(pipe, tty))
            self.assertFalse(ui.interactive(tty, pipe))
        with mock.patch.dict(os.environ, {"TERM": "dumb"}):
            self.assertFalse(ui.interactive(tty, tty))


class BannerTest(unittest.TestCase):
    def test_box_is_square_and_names_the_product(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LAB_BANNER_SHOWN", None)
            for st in (PLAIN, ui.Style(color=False, utf=False)):
                rows = ui.banner(st, "set up this Proxmox host").splitlines()
                self.assertEqual(len(rows), 4)
                self.assertIn("proxmox-lab", rows[1])
                self.assertIn("set up this Proxmox host", rows[2])
                self.assertEqual(len({len(r) for r in rows}), 1, rows)  # all rows the same width

    def test_not_drawn_twice_when_an_installer_already_did(self):
        with mock.patch.dict(os.environ, {"LAB_BANNER_SHOWN": "1"}):
            self.assertEqual(ui.banner(PLAIN), "")

    def test_step_lines_form_a_tree(self):
        self.assertIn("◆ Checking the host", ui.step_title("Checking the host", PLAIN))
        self.assertIn("│  ✔ ok", ui.line_ok("ok", PLAIN))
        self.assertIn("└ Done", ui.closing("Done", PLAIN))


if __name__ == "__main__":
    unittest.main()
