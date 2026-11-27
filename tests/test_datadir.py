import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
from ntk_pkg import config  # noqa: E402


class DataDirTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = os.path.realpath(self.tmp.name)
        env = {k: v for k, v in os.environ.items()
               if k not in ("NTK_CONFIG", "DEVCONSOLE_CONFIG", "XDG_CONFIG_HOME", "APPDATA")}
        env["HOME"] = self.home
        p = mock.patch.dict(os.environ, env, clear=True)
        p.start()
        self.addCleanup(p.stop)
        e = mock.patch("os.path.expanduser",
                       side_effect=lambda s: self.home + s[1:] if s.startswith("~") else s)
        e.start()
        self.addCleanup(e.stop)
        self.addCleanup(self.tmp.cleanup)
        self.xdg = os.path.join(self.home, ".config", "ntk")
        self.mid = os.path.join(self.home, ".claude", "agent-need-to-know")
        self.old = os.path.join(self.home, ".claude", "devconsole")

    def put(self, d, name="config.json"):
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, name), "w") as f:
            f.write("{}")

    def test_fresh_machine(self):
        self.assertEqual(config.config_path(), os.path.join(self.xdg, "config.json"))
        self.assertEqual(config.state_write("x"), os.path.join(self.xdg, "x"))
        self.assertEqual(config.state_read("x"), os.path.join(self.xdg, "x"))
        self.assertEqual(config.legacy_notes(), [])

    def test_only_claude_dir(self):
        self.put(self.mid)
        self.put(self.mid, "hint")
        self.assertEqual(config.config_path(), os.path.join(self.mid, "config.json"))
        self.assertEqual(config.state_read("hint"), os.path.join(self.mid, "hint"))
        self.assertEqual(config.state_write("new"), os.path.join(self.mid, "new"))
        notes = config.legacy_notes()
        self.assertEqual(len(notes), 1)
        self.assertIn("~/.claude/agent-need-to-know/", notes[0])
        self.assertIn("~/.config/ntk/", notes[0])
        self.assertFalse(os.path.exists(self.xdg))

    def test_only_devconsole(self):
        self.put(self.old)
        self.put(self.old, "hint")
        self.assertEqual(config.config_path(), os.path.join(self.old, "config.json"))
        self.assertEqual(config.state_read("hint"), os.path.join(self.old, "hint"))
        self.assertEqual(config.state_write("n"), os.path.join(self.old, "n"))
        self.assertIn("~/.claude/devconsole/", config.legacy_notes()[0])

    def test_xdg_wins(self):
        for d in (self.xdg, self.mid, self.old):
            self.put(d)
        self.assertEqual(config.config_path(), os.path.join(self.xdg, "config.json"))
        self.assertEqual(config.state_write("n"), os.path.join(self.xdg, "n"))
        self.assertEqual(config.legacy_notes(), [])

    def test_env_override(self):
        self.put(self.mid)
        cfg = os.path.join(self.home, "elsewhere", "c.json")
        with mock.patch.dict(os.environ, {"NTK_CONFIG": cfg}):
            self.assertEqual(config.config_path(), cfg)
            self.assertEqual(config.state_write("n"), os.path.join(self.home, "elsewhere", "n"))
            self.assertEqual(config.legacy_notes(), [])

    def test_xdg_config_home_absolute(self):
        x = os.path.join(self.home, "xdgcfg")
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": x}):
            self.assertEqual(config.config_path(), os.path.join(x, "ntk", "config.json"))

    def test_xdg_config_home_relative_ignored(self):
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": "relative/dir"}):
            self.assertEqual(config.config_path(), os.path.join(self.xdg, "config.json"))

    def test_windows_appdata(self):
        ad = os.path.join(self.home, "AppData")
        with mock.patch.dict(os.environ, {"APPDATA": ad}), mock.patch("os.name", "nt"):
            self.assertEqual(config._xdg_dir(), os.path.join(ad, "ntk"))
        with mock.patch("os.name", "nt"):
            self.assertEqual(config._xdg_dir(), self.xdg)


if __name__ == "__main__":
    unittest.main()
