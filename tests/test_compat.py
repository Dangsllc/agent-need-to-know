"""Rename compatibility: the old config directory, DEVCONSOLE_* variables, and the
old cache and log file names keep working for the one existing install."""
import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_core import AVAIL, jdump, jload  # noqa: E402
from test_release import ENTRY, Sandbox  # noqa: E402

from ntk_pkg import config, engine, lifecycle  # noqa: E402
from ntk_pkg.adapters import claude_code as cc  # noqa: E402

VARS = ("NTK_CONFIG", "DEVCONSOLE_CONFIG", "NTK_CLAUDE_JSON", "DEVCONSOLE_CLAUDE_JSON", "NTK_CLAUDE_HOME",
        "DEVCONSOLE_CLAUDE_HOME", "NTK_USER_SETTINGS", "DEVCONSOLE_USER_SETTINGS", "HOME")


class Home(unittest.TestCase):
    """A fake HOME with no overrides, so the default locations are exercised."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.t = os.path.realpath(self.tmp.name)
        self.saved = {k: os.environ.get(k) for k in VARS}
        for k in VARS:
            os.environ.pop(k, None)
        os.environ["HOME"] = self.t
        self.old = os.path.join(self.t, ".claude", "devconsole")
        self.new = os.path.join(self.t, ".claude", "agent-need-to-know")
        self.data = os.path.join(self.t, "data")
        os.makedirs(self.data)

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def put_config(self, d):
        os.makedirs(d, exist_ok=True)
        jdump({"root": self.t, "data_dir": self.data}, os.path.join(d, "config.json"))


class TestConfigLocation(Home):
    def test_new_default_when_nothing_exists(self):
        self.assertEqual(config.config_path(), os.path.join(self.new, "config.json"))
        self.assertIsNone(config.load_config())
        self.assertEqual(config.legacy_notes(), [])

    def test_old_config_is_read_and_flagged_but_not_moved(self):
        self.put_config(self.old)
        self.assertEqual(config.config_path(), os.path.join(self.old, "config.json"))
        self.assertEqual(config.load_config().data_dir, self.data)
        notes = config.legacy_notes()
        self.assertEqual(len(notes), 1)
        self.assertIn("agent-need-to-know", notes[0])
        self.assertFalse(os.path.exists(self.new))  # nothing moved or created by reading

    def test_new_config_wins_over_old(self):
        self.put_config(self.old)
        self.put_config(self.new)
        self.assertEqual(config.config_path(), os.path.join(self.new, "config.json"))
        self.assertEqual(config.legacy_notes(), [])

    def test_state_files_read_old_write_new(self):
        self.put_config(self.old)
        jdump({"date": "2000-01-01"}, os.path.join(self.old, ".capabilities-hint.json"))
        self.assertEqual(config.state_read(".capabilities-hint.json"), os.path.join(self.old, ".capabilities-hint.json"))
        self.assertEqual(config.state_write(".capabilities-hint.json"), os.path.join(self.new, ".capabilities-hint.json"))
        config.atomic_write(config.state_write(".capabilities-hint.json"), '{"date": "2001-01-01"}')
        self.assertEqual(jload(config.state_read(".capabilities-hint.json"))["date"], "2001-01-01")

    def test_scan_state_carries_the_note(self):
        self.put_config(self.old)
        jdump({"projects": {}}, os.path.join(self.t, ".claude.json"))
        old = (cc.fetch_plugin_list, cc.fetch_connectors)
        cc.fetch_plugin_list, cc.fetch_connectors = (lambda p: []), (lambda r: list(AVAIL))
        try:
            st = engine.scan(config.load_config())
        finally:
            cc.fetch_plugin_list, cc.fetch_connectors = old
        self.assertEqual(len(st["notes"]), 1)


class TestEnvFallback(Home):
    def test_old_variable_still_works_and_new_one_wins(self):
        os.environ["DEVCONSOLE_CONFIG"] = "/x/old.json"
        self.assertEqual(config.config_path(), "/x/old.json")
        os.environ["NTK_CONFIG"] = "/x/new.json"
        self.assertEqual(config.config_path(), "/x/new.json")

    def test_claude_file_overrides(self):
        os.environ["DEVCONSOLE_CLAUDE_JSON"] = "/x/c.json"
        os.environ["DEVCONSOLE_CLAUDE_HOME"] = "/x/home"
        self.assertEqual(cc.claude_json_path(), "/x/c.json")
        self.assertEqual(cc.claude_home(), "/x/home")
        os.environ["NTK_CLAUDE_JSON"] = "/y/c.json"
        self.assertEqual(cc.claude_json_path(), "/y/c.json")

    def test_explicit_config_keeps_state_files_beside_it(self):
        os.environ["DEVCONSOLE_CONFIG"] = os.path.join(self.t, "cfg", "config.json")
        self.assertEqual(config.state_write("a"), os.path.join(self.t, "cfg", "a"))


class TestDataDirNames(Home):
    def cfg(self):
        return config.Config(self.t, self.data)

    def test_cache_reads_old_name_and_writes_new(self):
        c = self.cfg()
        self.assertEqual(c.cache_path, os.path.join(self.data, ".ntk-cache.json"))
        jdump({"projects": {"/p": {"ts": 1, "plugins": []}}}, os.path.join(self.data, ".devconsole-cache.json"))
        self.assertIn("/p", engine.load_cache(c)["projects"])
        engine.save_cache(c, engine.load_cache(c))
        self.assertTrue(os.path.exists(os.path.join(self.data, ".ntk-cache.json")))
        self.assertEqual(c.cache_read_path, c.cache_path)

    def test_log_keeps_appending_to_old_file_only_if_present(self):
        c = self.cfg()
        self.assertTrue(c.log_path.endswith("ntk.log"))
        old = os.path.join(self.data, "devconsole.log")
        with open(old, "w") as f:
            f.write("old line\n")
        config.log_change(c, "p", "x", "a", "b")
        self.assertEqual(c.log_path, old)
        with open(old) as f:
            self.assertEqual(len(f.read().splitlines()), 2)
        self.assertFalse(os.path.exists(os.path.join(self.data, "ntk.log")))

    def test_both_plugin_names_count_as_self(self):
        for pid in ("agent-need-to-know@agent-need-to-know", "agent-need-to-know@other", "devconsole@devconsole"):
            self.assertTrue(cc.ClaudeCodeAdapter().is_self(pid), pid)
        self.assertFalse(cc.ClaudeCodeAdapter().is_self("ntk@x"))


class TestOldInstallEndToEnd(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "uses a sh stand-in for claude")
    def test_scan_with_old_config_dir_and_old_env_names(self):
        with tempfile.TemporaryDirectory() as t:
            t = os.path.realpath(t)
            sb = Sandbox(t)
            env = {k: v for k, v in sb.env.items() if not k.startswith("NTK_")}
            old = os.path.join(sb.home, ".claude", "devconsole")
            os.makedirs(old)
            root, data = os.path.join(t, "code"), os.path.join(t, "data")
            os.makedirs(os.path.join(root, "a"))
            os.makedirs(data)
            jdump({"root": root, "data_dir": data}, os.path.join(old, "config.json"))
            env["DEVCONSOLE_CLAUDE_JSON"] = os.path.join(sb.home, ".claude.json")
            env["DEVCONSOLE_CLAUDE_HOME"] = os.path.join(sb.home, ".claude")
            env["DEVCONSOLE_USER_SETTINGS"] = os.path.join(sb.home, ".claude", "settings.json")
            r = subprocess.run([sys.executable, ENTRY, "scan"], env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("new=", r.stdout)
            self.assertIn("note: Config is read from ~/.claude/devconsole/", r.stdout)
            self.assertFalse(os.path.exists(os.path.join(sb.home, ".claude", "agent-need-to-know", "config.json")))
            self.assertTrue(os.path.exists(os.path.join(data, ".ntk-cache.json")))


if __name__ == "__main__":
    unittest.main()
