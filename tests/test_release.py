import json
import os
import re
import stat
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from lab import client, config as config_mod, connect, setup as setup_mod, tsacl, watch
from lab.db import DB
from lab.errors import PreconditionError
from lab.hostio import HostIO
from lab.progress import Reporter
from lab.prompt import ScriptedPrompter
from lab.proxmox import Proxmox
from lab.template import TemplateBuilder

from .fake_io import FakeIO
from .fake_proxmox import FakeProxmox
from .test_m1 import CATALOGUE, make_io

ROOT = Path(__file__).resolve().parents[1]
OAUTH_FILE = "/etc/project-lab/ts-oauth"
CREDENTIAL = {"Ready to paste": True, "OAuth client ID": "cid-123", "OAuth client secret": "secret-xyz"}


def make_ctx(fake=None, io=None):
    fake, io = fake or FakeProxmox(), io or make_io()
    io.files.pop(OAUTH_FILE, None)  # a fresh host has no credential yet
    cfg = config_mod.Config()
    ctx = types.SimpleNamespace(px=Proxmox(fake), io=io, runner=fake, cfg=cfg, dry_run=False, args=None,
                                rep=Reporter(__import__("io").StringIO()))
    db = DB(":memory:")
    ctx.db = lambda create=False: db
    ctx.templates = lambda say=None: TemplateBuilder(ctx.cfg, ctx.px, io, db, catalogue=CATALOGUE,
                                                     sleep=lambda s: None, progress=say or ctx.rep)
    return ctx, fake, io


class SetupWizardTest(unittest.TestCase):
    ANSWERS = {"Fast disk": "nvme", "Archive disk": "bigdisk", "Your tailnet login": "alex@example.com",
               "Have you saved the policy": True, "Build it now": True, **CREDENTIAL}

    def run_setup(self, ctx, answers=None, **kw):
        ui = ScriptedPrompter({**self.ANSWERS, **(answers or {})}, assume_yes=True)
        with mock.patch.object(setup_mod, "_is_root", return_value=True):
            rc = setup_mod.run(ctx, ui, config_file="/etc/project-lab/config.toml", **kw)
        return rc, "\n".join(ui.log)

    def test_happy_path_goes_from_nothing_to_a_working_lab(self):
        ctx, fake, io = make_ctx()
        rc, log = self.run_setup(ctx)
        self.assertEqual(rc, 0, log)
        self.assertIn("/etc/project-lab/config.toml", io.files)
        self.assertEqual(json.loads(io.files[OAUTH_FILE]), {"client_id": "cid-123", "client_secret": "secret-xyz"})
        self.assertTrue(any(v.get("template") for v in fake.vms.values()), "the base image was built")
        self.assertIn("tag:lab", log)  # the policy it prints uses the dedicated tag
        self.assertNotIn("secret-xyz", log)  # the secret is never echoed
        self.assertIn("All set", log)
        self.assertEqual(ctx.db().list_users()[0]["role"], "admin")

    def test_credential_is_written_private_and_validated_with_the_right_tags(self):
        ctx, _, io = make_ctx()
        self.run_setup(ctx)
        key_req = [p for p in io.posts if p["url"].endswith("/keys")][0]
        self.assertEqual(key_req["json"]["capabilities"]["devices"]["create"]["tags"], ["tag:lab", "tag:user-alex"])

    def test_rejected_credential_is_removed_and_the_user_can_retry(self):
        ctx, _, io = make_ctx()
        from lab.hostio import FetchError
        attempts = {"n": 0}
        original = io.post

        def flaky(url, **kw):
            if url.endswith("/oauth/token"):
                attempts["n"] += 1
                if attempts["n"] == 1:
                    raise FetchError("401 unauthorized")
            return original(url, **kw)
        io.post = flaky
        rc, log = self.run_setup(ctx)
        self.assertEqual(rc, 0, log)
        self.assertGreaterEqual(attempts["n"], 2)
        self.assertIn("rejected it", log)
        self.assertIn(OAUTH_FILE, io.files)

    def test_three_failures_stop_without_leaving_a_bad_credential(self):
        ctx, _, io = make_ctx()
        from lab.hostio import FetchError
        io.post_replies["https://api.tailscale.com/api/v2/oauth/token"] = FetchError("401")
        with self.assertRaisesRegex(PreconditionError, "could not validate"):
            self.run_setup(ctx)
        self.assertNotIn(OAUTH_FILE, io.files)

    def test_an_existing_working_credential_is_reused_without_asking(self):
        ctx, _, io = make_ctx()
        io.files[OAUTH_FILE] = b'{"client_id": "a", "client_secret": "b"}'
        rc, log = self.run_setup(ctx, answers={"OAuth client ID": None})
        self.assertEqual(rc, 0, log)
        self.assertIn("the stored Tailscale credential works", log)

    def test_blocking_problems_are_listed_and_nothing_is_changed(self):
        ctx, fake, io = make_ctx()
        fake.tailscale_up = False
        ui = ScriptedPrompter(self.ANSWERS, assume_yes=True)
        with mock.patch.object(setup_mod, "_is_root", return_value=False):
            rc = setup_mod.run(ctx, ui)
        self.assertEqual(rc, 3)
        log = "\n".join(ui.log)
        self.assertIn("running as root", log)
        self.assertIn("Tailscale", log)
        self.assertNotIn("/etc/project-lab/config.toml", io.files)

    def test_skip_template_and_policy_not_saved_yet(self):
        ctx, fake, _ = make_ctx()
        rc, log = self.run_setup(ctx, skip_template=True)
        self.assertEqual(rc, 0)
        self.assertFalse(any(v.get("template") for v in fake.vms.values()))
        ctx2, _, io2 = make_ctx()
        rc, log = self.run_setup(ctx2, answers={"Have you saved the policy": False})
        self.assertEqual(rc, 0)
        self.assertNotIn(OAUTH_FILE, io2.files)  # stops before the credential step


class SmallPiecesTest(unittest.TestCase):
    def test_secret_prompt_never_lands_in_the_info_log(self):
        ui = ScriptedPrompter({"OAuth client secret": "s3cret"})
        self.assertEqual(ui.secret("OAuth client secret"), "s3cret")
        self.assertEqual(ui.log, [])

    @unittest.skipIf(os.name == "nt", "POSIX permissions")
    def test_credential_file_is_private_even_if_it_existed_with_looser_permissions(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "ts-oauth"
            p.write_text("old")
            os.chmod(p, 0o644)
            HostIO().write_text(str(p), "new", 0o600)
            self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)

    def test_default_tag_is_the_dedicated_one(self):
        self.assertEqual(config_mod.Config().tailscale.base_tag, "tag:lab")
        self.assertEqual(tsacl.DEFAULT_BASE_TAG, "tag:lab")

    def test_remove_from_path(self):
        self.assertEqual(client.remove_from_path(r"C:\a;C:\Users\x\lab\;C:\b", r"C:\USERS\x\lab"), r"C:\a;C:\b")
        self.assertEqual(client.remove_from_path(r"C:\a", r"C:\zzz"), r"C:\a")

    def test_version_is_answered_locally_on_the_pc(self):
        with mock.patch("builtins.print") as p, mock.patch.object(client, "load_config", side_effect=AssertionError):
            self.assertEqual(client.main(["version"]), 0)
        self.assertIn("this PC", p.call_args[0][0])

    def test_launcher_knows_about_the_packaged_exe(self):
        with mock.patch.object(sys, "frozen", True, create=True), mock.patch.object(sys, "executable", r"C:\x\lab.exe"):
            self.assertEqual(watch.launcher_args(), [r"C:\x\lab.exe", "watch"])
            self.assertIn('""C:\\x\\lab.exe"" watch', watch.startup_script())
        self.assertEqual(watch.launcher_args()[1:], ["-m", "lab.entry", "watch"])


class ClaudeConfigTest(unittest.TestCase):
    ENTRY = {"command": "ssh", "args": ["-T", "root@pve", "lab", "mcp"]}

    def test_adds_updates_and_keeps_everything_else(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "Claude" / "claude_desktop_config.json"
            p.parent.mkdir()
            original = {"theme": "dark", "mcpServers": {"other": {"command": "x"}}}
            p.write_text(json.dumps(original), encoding="utf-8")
            self.assertEqual(connect.merge_mcp_server(p, self.ENTRY), "added")
            data = json.loads(p.read_text(encoding="utf-8"))
            self.assertEqual(data["mcpServers"]["other"], {"command": "x"})
            self.assertEqual(data["mcpServers"]["lab"], self.ENTRY)
            self.assertEqual(data["theme"], "dark")
            self.assertEqual(json.loads(p.with_name(p.name + ".lab-backup").read_text()), original)  # backed up
            self.assertEqual(connect.merge_mcp_server(p, self.ENTRY), "unchanged")
            self.assertEqual(connect.merge_mcp_server(p, {**self.ENTRY, "args": ["-T"]}), "updated")
            self.assertEqual(json.loads(p.with_name(p.name + ".lab-backup").read_text()), original)  # only the first state

    def test_creates_the_file_when_claude_has_not_written_one_yet(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "Claude" / "claude_desktop_config.json"
            self.assertEqual(connect.merge_mcp_server(p, self.ENTRY), "added")
            self.assertEqual(json.loads(p.read_text())["mcpServers"]["lab"], self.ENTRY)
            self.assertFalse(p.with_name(p.name + ".lab-backup").exists())  # nothing to back up

    def test_refuses_to_touch_files_it_cannot_understand(self):
        with tempfile.TemporaryDirectory() as d:
            for bad in ("{ not json", "[1, 2]", '{"mcpServers": []}'):
                p = Path(d) / "c.json"
                p.write_text(bad)
                with self.assertRaises(client.ClientError):
                    connect.merge_mcp_server(p, self.ENTRY)
                self.assertEqual(p.read_text(), bad)  # untouched

    def test_finds_the_classic_and_the_store_install(self):
        with tempfile.TemporaryDirectory() as d:
            appdata, local = Path(d) / "Roaming", Path(d) / "Local"
            env = {"APPDATA": str(appdata), "LOCALAPPDATA": str(local)}
            self.assertIsNone(connect.find_claude_desktop_config(env))
            store = local / "Packages" / "Claude_abc123" / "LocalCache" / "Roaming" / "Claude"
            store.mkdir(parents=True)
            self.assertEqual(connect.find_claude_desktop_config(env), store / "claude_desktop_config.json")  # folder only
            (store / "claude_desktop_config.json").write_text("{}")
            (appdata / "Claude").mkdir(parents=True)
            (appdata / "Claude" / "claude_desktop_config.json").write_text("{}")
            self.assertEqual(connect.find_claude_desktop_config(env), appdata / "Claude" / "claude_desktop_config.json")

    def test_failures_are_explained_in_plain_words(self):
        self.assertIn("Tailscale", connect.explain_failure("ssh: Could not resolve hostname pve"))
        self.assertIn("policy", connect.explain_failure("root@pve: Permission denied (publickey)"))
        self.assertIn("lab user", connect.explain_failure("error: x is not a lab user"))
        self.assertIn("installed", connect.explain_failure("bash: lab: command not found"))

    def test_mcp_entry_prefers_the_full_ssh_path(self):
        with tempfile.TemporaryDirectory() as d:
            exe = Path(d) / "ssh.exe"
            exe.write_text("")
            with mock.patch.object(connect, "WINDOWS_SSH", exe):
                e = connect.mcp_entry({"host": "pve", "ssh_user": "root"})
            self.assertEqual(e["command"], str(exe))
            self.assertEqual(e["args"][-2:], ["lab", "mcp"])
            self.assertIn("BatchMode=yes", e["args"])


class ConnectWizardTest(unittest.TestCase):
    def test_full_flow_writes_everything_once_and_asks_before_each_change(self):
        with tempfile.TemporaryDirectory() as d:
            claude = Path(d) / "Roaming" / "Claude"
            claude.mkdir(parents=True)
            saved, said, asked = {}, [], []

            def ask(prompt):
                asked.append(prompt)
                return "pve.example.ts.net" if "Tailscale name" in prompt else "y"
            with mock.patch.object(connect, "test_connection", return_value=(True, "lab 0.1.0")), \
                 mock.patch.object(client, "cmd_ssh_config") as sshcfg, \
                 mock.patch.object(connect.shutil, "which", return_value=None), \
                 mock.patch.object(connect.watch, "install", return_value=Path("lab-approvals.vbs")), \
                 mock.patch.object(connect.subprocess, "Popen") as popen:
                rc = connect.run([], ask=ask, say=said.append, env={"APPDATA": str(Path(d) / "Roaming")},
                                 load=lambda: {}, save=lambda h, u: saved.update(host=h, user=u))
            self.assertEqual(rc, 0)
            self.assertEqual(saved, {"host": "pve.example.ts.net", "user": "root"})
            data = json.loads((claude / "claude_desktop_config.json").read_text())
            self.assertEqual(data["mcpServers"]["lab"]["args"][-3:], ["root@pve.example.ts.net".replace("root@", "root@"), "lab", "mcp"][-3:])
            sshcfg.assert_called_once()
            popen.assert_called_once()  # the approval helper was started
            self.assertTrue(any("Add the lab server" in q for q in asked))  # it asked before touching Claude's file
            self.assertIn("`.mcp.json`", "\n".join(said))  # no claude CLI: it prints the snippet instead

    def test_failure_to_connect_explains_and_changes_nothing_unless_told(self):
        said, saved = [], {}
        with mock.patch.object(connect, "test_connection", return_value=(False, "ssh: Could not resolve hostname x")):
            rc = connect.run(["x"], ask=lambda p: "n", say=said.append, load=lambda: {},
                             save=lambda h, u: saved.update(host=h))
        self.assertEqual(rc, 3)
        self.assertEqual(saved, {})
        self.assertIn("Tailscale", "\n".join(said))

    def test_host_is_required(self):
        with self.assertRaises(client.ClientError):
            connect.run([], ask=lambda p: "", say=lambda m: None, load=lambda: {})


class RepositoryFilesTest(unittest.TestCase):
    """Things that are easy to break by accident and expensive to find out on release day."""

    def read(self, rel):
        return (ROOT / rel).read_bytes()

    def test_scripts_have_no_byte_order_mark_and_unix_line_endings(self):
        for rel in ("install.sh", "tests/installer_smoke.sh", "pyproject.toml", "install.ps1",
                    "src/lab/data/approve_popup.ps1"):
            data = self.read(rel)
            self.assertFalse(data.startswith(b"\xef\xbb\xbf"), f"{rel} starts with a BOM")
        for rel in ("install.sh", "tests/installer_smoke.sh"):
            data = self.read(rel)
            self.assertTrue(data.startswith(b"#!/usr/bin/env bash\n"), rel)
            self.assertNotIn(b"\r\n", data, f"{rel} has Windows line endings")

    def test_version_is_the_same_everywhere(self):
        pyproject = re.search(r'^version = "([^"]+)"', self.read("pyproject.toml").decode(), re.M).group(1)
        init = re.search(r'__version__ = "([^"]+)"', self.read("src/lab/__init__.py").decode()).group(1)
        self.assertEqual(pyproject, init)
        self.assertIn(f"## {init}", self.read("CHANGELOG.md").decode())

    def test_every_image_in_the_readme_exists(self):
        readme = self.read("README.md").decode()
        refs = re.findall(r'src="(docs/assets/[^"]+)"', readme)
        self.assertGreaterEqual(len(refs), 8)
        for ref in refs:
            self.assertTrue((ROOT / ref).is_file(), f"README references {ref}")

    def test_readme_install_commands_point_at_this_repo_and_real_files(self):
        readme = self.read("README.md").decode()
        self.assertIn("raw.githubusercontent.com/madmaxvoltro/proxmox-lab/main/install.sh | bash", readme)
        self.assertIn("raw.githubusercontent.com/madmaxvoltro/proxmox-lab/main/install.ps1 | iex", readme)
        for rel in ("install.sh", "install.ps1", "LICENSE", "CONTRIBUTING.md", "SECURITY.md", "docs/SPEC.md"):
            self.assertTrue((ROOT / rel).is_file(), rel)

    def test_installers_agree_with_the_release_workflow_on_file_names(self):
        workflow = self.read(".github/workflows/release.yml").decode()
        sh, ps = self.read("install.sh").decode(), self.read("install.ps1").decode()
        self.assertIn("project-lab-${GITHUB_REF_NAME}.tar.gz", workflow)
        self.assertIn("project-lab-${VERSION}.tar.gz", sh)
        for arch in ("x64", "arm64"):
            self.assertIn(f"lab-windows-{arch}", workflow)
        self.assertIn("lab-windows-$arch.exe", ps)
        self.assertIn("SHA256SUMS", workflow + sh + ps)
        self.assertIn("--add-data", workflow)  # the popup script and distro data are bundled into lab.exe

    def test_no_personal_data_in_tracked_files(self):
        """Generic patterns, so this file itself contains nothing private: no personal e-mail addresses,
        no real tailnet names, no auth keys, no public IP addresses (documentation and private ranges are fine)."""
        import ipaddress
        patterns = {
            "a personal e-mail address": r"[\w.+-]+@(?:gmail|outlook|hotmail|yahoo|icloud|proton)\.[a-z.]+",
            "a real tailnet name": r"\btail[0-9a-f]{5,}\.ts\.net",
            "a Tailscale key": r"tskey-(?:auth|api|client)-[A-Za-z0-9]{12,}",
        }
        allowed_public = {"1.1.1.1", "9.9.9.9", "8.8.8.8", "1.2.3.4", "5.6.7.8"}  # public DNS resolvers and obvious placeholders
        files = [p for pat in ("src/**/*", "docs/*.md", "tests/*", ".github/**/*") for p in ROOT.glob(pat)]
        files += [ROOT / "README.md", ROOT / "install.sh", ROOT / "install.ps1"]
        for path in files:
            if path.suffix not in (".py", ".md", ".toml", ".ps1", ".sh", ".svg", ".yml", ".json"):
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            rel = path.relative_to(ROOT)
            for what, pat in patterns.items():
                self.assertIsNone(re.search(pat, text), f"{rel} contains {what}")
            for m in re.finditer(r"(?<![\w.])(\d{1,3}(?:\.\d{1,3}){3})(?![\w.]*\d)", text):
                try:
                    ip = ipaddress.ip_address(m.group(1))
                except ValueError:
                    continue
                self.assertFalse(ip.is_global and m.group(1) not in allowed_public, f"{rel} contains the public address {m.group(1)}")
    def test_package_data_ships_the_popup_script(self):
        self.assertIn("data/*.ps1", self.read("pyproject.toml").decode())
        self.assertTrue(watch.SCRIPT.is_file())


if __name__ == "__main__":
    unittest.main()
