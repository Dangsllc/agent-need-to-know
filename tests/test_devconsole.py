import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import devconsole as dc  # noqa: E402


def jdump(obj, path):
    with open(path, "w") as f:
        json.dump(obj, f)


def jload(path):
    with open(path) as f:
        return json.load(f)


def slurp(path):
    with open(path) as f:
        return f.read()

PROFILES = {
    "profiles": {
        "base": {"plugins": [], "connectors_off": ["Alpha", "Beta"]},
        "mid": {"extends": "base", "plugins": ["a@x"], "connectors_off": ["Gamma"]},
        "kid": {"extends": "mid", "plugins": ["b@x"], "connectors_on": ["Beta"], "suggested": ["s@x", "a@x"]},
    }
}


class Env(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = os.path.realpath(self.tmp.name)
        self.root = os.path.join(t, "root")
        self.data = os.path.join(t, "data")
        os.makedirs(os.path.join(self.root, "app"))
        os.makedirs(os.path.join(self.root, "newone"))
        os.makedirs(self.data)
        self.cfgfile = os.path.join(t, "config.json")
        self.cj = os.path.join(t, "claude.json")
        jdump({"root": self.root, "data_dir": self.data}, self.cfgfile)
        jdump(PROFILES, os.path.join(self.data, "profiles.json"))
        jdump({"projects": {"app": {"profile": "kid"}}}, os.path.join(self.data, "projects.json"))
        jdump({}, os.path.join(t, "user-settings.json"))
        self.saved = dict(os.environ)
        os.environ.update({"DEVCONSOLE_CONFIG": self.cfgfile, "DEVCONSOLE_CLAUDE_JSON": self.cj,
                           "DEVCONSOLE_USER_SETTINGS": os.path.join(t, "user-settings.json"),
                           "DEVCONSOLE_CLAUDE_HOME": os.path.join(t, "home")})

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.saved)
        self.tmp.cleanup()

    def check(self, path):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            dc.check_path(path, hook=True)
        return buf.getvalue()


class TestProfiles(unittest.TestCase):
    def test_extends_union_and_connectors_on_wins(self):
        r = dc.resolve_profile(PROFILES["profiles"], "kid")
        self.assertEqual(r["plugins"], ["a@x", "b@x"])
        self.assertEqual(r["connectors_off"], ["Alpha", "Gamma"])
        self.assertEqual(r["suggested"], ["s@x"])

    def test_cycle_and_unknown(self):
        p = {"a": {"extends": "b", "plugins": ["1"]}, "b": {"extends": "a", "plugins": ["2"]}}
        self.assertEqual(sorted(dc.resolve_profile(p, "a")["plugins"]), ["1", "2"])
        self.assertFalse(dc.resolve_profile(p, "nope")["known"])

    def test_desired_with_extras(self):
        e = {"profile": "kid", "plugins_on": ["z@x"], "plugins_off": ["a@x"],
             "connectors_on": ["claude.ai Alpha"], "connectors_off": ["Delta"]}
        d = dc.desired_state(PROFILES["profiles"], e)
        self.assertEqual(d["plugins"], ["b@x", "z@x"])
        self.assertEqual(d["connectors_off"], ["Gamma", "Delta"])


class TestCheck(Env):
    def test_new_project(self):
        out = json.loads(self.check(os.path.join(self.root, "newone")))
        hs = out["hookSpecificOutput"]
        self.assertEqual(hs["hookEventName"], "SessionStart")
        self.assertIn("no plugin profile", hs["additionalContext"])
        self.assertIn("/project-setup", hs["additionalContext"])

    def test_nonexistent_under_root_is_new(self):
        self.assertIn("no plugin profile", self.check(os.path.join(self.root, "zzz")))

    def test_outside_root_silent(self):
        self.assertEqual(self.check(self.tmp.name), "")

    def test_drift_then_ok(self):
        out = json.loads(self.check(os.path.join(self.root, "app")))
        self.assertIn("/devconsole", out["hookSpecificOutput"]["additionalContext"])
        os.makedirs(os.path.join(self.root, "app", ".claude"))
        jdump({"enabledPlugins": {"a@x": True, "b@x": True}},
              os.path.join(self.root, "app", ".claude", "settings.local.json"))
        jdump({"projects": {os.path.join(self.root, "app"):
                            {"disabledMcpServers": ["claude.ai Alpha", "claude.ai Gamma"], "other": 1}}}, self.cj)
        self.assertEqual(self.check(os.path.join(self.root, "app")), "")

    def test_subdir_matches_project(self):
        os.makedirs(os.path.join(self.root, "app", "src"))
        self.assertIn("app (profile kid)", self.check(os.path.join(self.root, "app", "src")))

    def test_missing_config_hints_once(self):
        os.environ["DEVCONSOLE_CONFIG"] = os.path.join(self.tmp.name, "nope", "config.json")
        self.assertIn("no config", self.check("/tmp"))
        self.assertEqual(self.check("/tmp"), "")


class TestClaudeJsonWrite(Env):
    def test_preserves_keys_and_indent(self):
        app = os.path.join(self.root, "app")
        orig = {"numStartups": 5, "oauthAccount": {"organizationUuid": "abc"},
                "projects": {app: {"allowedTools": ["x"], "disabledMcpServers": ["plugin:thing", "claude.ai Old"]},
                             "/other": {"k": 1}}}
        with open(self.cj, "w") as f:
            f.write(json.dumps(orig, indent=4) + "\n")
        os.chmod(self.cj, 0o600)
        self.assertTrue(dc.write_disabled_servers(app, ["Alpha", "Gamma"]))
        text = slurp(self.cj)
        self.assertTrue(text.startswith('{\n    "numStartups"'))
        self.assertTrue(text.endswith("\n"))
        d = json.loads(text)
        self.assertEqual(d["numStartups"], 5)
        self.assertEqual(d["projects"]["/other"], {"k": 1})
        self.assertEqual(d["projects"][app]["allowedTools"], ["x"])
        self.assertEqual(d["projects"][app]["disabledMcpServers"], ["plugin:thing", "claude.ai Alpha", "claude.ai Gamma"])
        self.assertEqual(os.stat(self.cj).st_mode & 0o777, 0o600)
        self.assertFalse(dc.write_disabled_servers(app, ["Alpha", "Gamma"]))  # no-op

    def test_creates_project_entry(self):
        jdump({"projects": {}, "x": 1}, self.cj)
        dc.write_disabled_servers(os.path.join(self.root, "newone"), ["Alpha"])
        d = jload(self.cj)
        self.assertEqual(d["x"], 1)
        self.assertEqual(d["projects"][os.path.join(self.root, "newone")], {"disabledMcpServers": ["claude.ai Alpha"]})

    def test_set_extra_drops_when_matches_profile(self):
        out = []
        dc.fetch_plugin_list = lambda p: []  # never shell out to the real claude CLI in tests
        cfg = dc.load_config()
        # turn on a plugin already in the profile: no extra recorded (apply will fail without claude; ignore)
        try:
            dc.do_set(cfg, "app", "plugin", "a@x", "on", out=out.append)
        except Exception:
            pass
        p = jload(os.path.join(self.data, "projects.json"))["projects"]["app"]
        self.assertNotIn("plugins_on", p)
        try:
            dc.do_set(cfg, "app", "plugin", "z@x", "on", out=out.append)
        except Exception:
            pass
        p = jload(os.path.join(self.data, "projects.json"))["projects"]["app"]
        self.assertEqual(p["plugins_on"], ["z@x"])


if __name__ == "__main__":
    unittest.main()
