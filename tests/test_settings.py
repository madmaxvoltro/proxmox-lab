"""`lab settings` and the rule that every release carries its changelog."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packaging"))

import release_notes  # noqa: E402
from lab import __version__, config, settings  # noqa: E402
from lab.errors import PreconditionError, UsageError  # noqa: E402
from lab.prompt import ScriptedPrompter  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


class SettingsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = str(Path(self.dir.name) / "config.toml")
        self.cfg = config.Config()
        Path(self.path).write_text(config.dumps(self.cfg), encoding="utf-8")

    def reload(self):
        return config.load(self.path)

    def test_special_settings_are_off_by_default_and_marked(self):
        self.assertFalse(self.cfg.mcp.root_without_approval)
        self.assertFalse(self.cfg.mcp.attach_without_approval)
        self.assertTrue(settings.BY_KEY["mcp.root_without_approval"].special)
        self.assertTrue(settings.BY_KEY["mcp.root_without_approval"].warning)
        self.assertFalse(settings.BY_KEY["mcp.max_cpu"].special)

    def test_enabling_a_special_setting_shows_the_warning_and_defaults_to_no(self):
        ui = ScriptedPrompter()  # an unanswered confirm is refused
        changed = settings.set_value(self.cfg, "mcp.root_without_approval", "on", self.path, ui)
        self.assertFalse(changed)
        self.assertFalse(self.reload().mcp.root_without_approval)
        self.assertTrue(any("Root can change anything" in m for m in ui.log))  # the warning box was shown
        self.assertIn("not changed", ui.log)

    def test_confirming_enables_it_and_it_is_saved_with_a_backup(self):
        ui = ScriptedPrompter({"Enable": True})
        self.assertTrue(settings.set_value(self.cfg, "mcp.root_without_approval", "on", self.path, ui))
        self.assertTrue(self.reload().mcp.root_without_approval)
        self.assertTrue(Path(self.path + ".bak").exists())
        self.assertFalse(self.reload().mcp.attach_without_approval)  # nothing else changed

    def test_disabling_needs_no_warning(self):
        self.cfg.mcp.root_without_approval = True
        settings.save(self.cfg, self.path)
        ui = ScriptedPrompter({"Change": True})
        self.assertTrue(settings.set_value(self.cfg, "mcp.root_without_approval", "off", self.path, ui))
        self.assertFalse(self.reload().mcp.root_without_approval)
        self.assertFalse(any("Root can change anything" in m for m in ui.log))

    def test_yes_flag_is_how_scripts_enable_special_settings(self):
        ui = ScriptedPrompter(assume_yes=True)
        self.assertTrue(settings.set_value(self.cfg, "mcp.attach_without_approval", "true", self.path, ui, assume_yes=True))
        self.assertTrue(self.reload().mcp.attach_without_approval)

    def test_numbers_and_sizes_are_checked(self):
        ui = ScriptedPrompter(assume_yes=True)
        settings.set_value(self.cfg, "mcp.max_cpu", "4", self.path, ui, assume_yes=True)
        settings.set_value(self.cfg, "mcp.max_ram", "8G", self.path, ui, assume_yes=True)
        self.assertEqual((self.reload().mcp.max_cpu, self.reload().mcp.max_ram), (4, "8G"))
        for key, bad in (("mcp.max_cpu", "0"), ("mcp.max_cpu", "lots"), ("mcp.max_ram", "tiny"), ("mcp.max_ram", "1M"),
                         ("mcp.root_without_approval", "maybe"), ("mcp.nope", "1")):
            with self.assertRaises(UsageError, msg=(key, bad)):
                settings.set_value(self.cfg, key, bad, self.path, ui, assume_yes=True)

    def test_unchanged_value_writes_nothing(self):
        ui = ScriptedPrompter(assume_yes=True)
        self.assertFalse(settings.set_value(self.cfg, "mcp.max_cpu", str(self.cfg.mcp.max_cpu), self.path, ui, assume_yes=True))
        self.assertFalse(Path(self.path + ".bak").exists())

    def test_a_file_that_cannot_be_written_gives_a_clear_error(self):
        with mock.patch.object(settings.tempfile, "mkstemp", side_effect=PermissionError):
            with self.assertRaisesRegex(PreconditionError, "as root"):
                settings.save(self.cfg, self.path)

    def test_the_menu_changes_a_setting_after_confirming(self):
        class Menu(ScriptedPrompter):
            def __init__(self):
                super().__init__({"Enable": True})
                self.picks = iter(["mcp.attach_without_approval", "__done__"])

            def choose(self, label, options, default=None):
                return next(self.picks)

        with mock.patch.object(settings.term, "banner", return_value=""):
            self.assertEqual(settings.interactive(self.cfg, self.path, Menu()), 0)
        self.assertTrue(self.reload().mcp.attach_without_approval)

    def test_table_lists_every_setting(self):
        text = settings.table(self.cfg)
        for s in settings.SETTINGS:
            self.assertIn(s.key, text)
        self.assertIn("[special]", text)

    def test_the_warning_box_is_square(self):
        for st in (settings.term.Style(False, True), settings.term.Style(False, False)):
            rows = settings.warning_box(settings.BY_KEY["mcp.root_without_approval"], st).splitlines()
            self.assertEqual(len({len(r) for r in rows}), 1)


class ChangelogInReleasesTest(unittest.TestCase):
    CHANGELOG = "# Changes\n\n## 0.2.0\n\n- new thing\n- other\n\n## 0.1.1 (first release)\n\n- first\n"

    def test_the_section_is_found_by_version(self):
        self.assertEqual(release_notes.section(self.CHANGELOG, "0.2.0"), "- new thing\n- other")
        self.assertEqual(release_notes.section(self.CHANGELOG, "0.1.1"), "- first")  # a heading may have a suffix
        self.assertEqual(release_notes.section(self.CHANGELOG, "0.2"), "")  # no partial matches
        self.assertEqual(release_notes.section(self.CHANGELOG, "9.9.9"), "")

    def test_notes_start_with_the_changelog_then_the_install_commands(self):
        text = release_notes.notes(self.CHANGELOG, "v0.2.0", "o/r")
        self.assertTrue(text.startswith("## What's changed in v0.2.0"))
        self.assertLess(text.index("- new thing"), text.index("## Install"))
        self.assertIn("raw.githubusercontent.com/o/r/main/install-client.sh", text)
        self.assertNotIn("first", text.split("## Install")[0])  # only this version's entry

    def test_a_missing_entry_refuses_to_make_notes(self):
        with self.assertRaisesRegex(SystemExit, "no entry for 9.9.9"):
            release_notes.notes(self.CHANGELOG, "v9.9.9", "o/r")

    def test_the_current_version_has_a_changelog_entry(self):
        text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertTrue(release_notes.section(text, __version__), f"CHANGELOG.md needs an entry for {__version__}")

    def test_the_release_workflow_publishes_those_notes_and_checks_early(self):
        wf = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        self.assertEqual(wf.count("release_notes.py"), 2)  # a check before the builds, and the real notes
        self.assertIn("--notes-file notes.md", wf)
        self.assertNotIn("--generate-notes", wf)


if __name__ == "__main__":
    unittest.main()
