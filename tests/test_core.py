import contextlib
import http.client
import http.server
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
from ntk_pkg import config, engine, lifecycle, model, server  # noqa: E402
from ntk_pkg.adapters import claude_code as cc  # noqa: E402


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
    "_about": "comment",
    "default_profiles": ["base"],
    "plugin_accounts": {"_about": "x", "a@x": "any"},
    "profiles": {
        "_note": "ignored",
        "base": {"group": "default", "description": "d", "plugins": [], "connectors": ["Docs"]},
        "py": {"group": "code", "description": "python", "plugins": ["a@x"], "connectors": []},
        "ts": {"group": "code", "description": "ts", "plugins": ["b@x"], "connectors": ["Slack"]},
        "sec": {"group": "code", "description": "sec", "plugins": ["c@x"], "connectors": [],
                "deny_plugins": ["b@x"], "deny_connectors": ["Slack"]},
    },
}
AVAIL = ["Docs", "Slack", "Gmail", "Stripe"]


def P(profiles=None, defaults=("base",)):
    """Normalized profiles via the real loader shape."""
    return {n: model._norm_profile(profiles or PROFILES["profiles"], n)
            for n in (profiles or PROFILES["profiles"]) if not n.startswith("_")}, list(defaults)


class Env(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = os.path.realpath(self.tmp.name)
        self.t = t
        self.root = os.path.join(t, "root")
        self.data = os.path.join(t, "data")
        os.makedirs(os.path.join(self.root, "app"))
        os.makedirs(os.path.join(self.root, "other"))
        os.makedirs(os.path.join(self.root, "newone"))
        os.makedirs(self.data)
        self.cfgfile = os.path.join(t, "config.json")
        self.cj = os.path.join(t, "claude.json")
        jdump({"root": self.root, "data_dir": self.data}, self.cfgfile)
        jdump(PROFILES, os.path.join(self.data, "profiles.json"))
        jdump({"_about": "c", "projects": {"app": {"profiles": ["py", "ts"], "notes": "n"},
                                           "other": {"profiles": []}}},
              os.path.join(self.data, "projects.json"))
        jdump({}, os.path.join(t, "user-settings.json"))
        jdump({"projects": {}}, self.cj)
        self.saved = dict(os.environ)
        os.environ.update({"NTK_CONFIG": self.cfgfile, "NTK_CLAUDE_JSON": self.cj,
                           "NTK_USER_SETTINGS": os.path.join(t, "user-settings.json"),
                           "NTK_CLAUDE_HOME": os.path.join(t, "home")})
        # never shell out to the real claude CLI
        self._orig = (cc.fetch_plugin_list, cc.fetch_connectors, cc.run)
        self.cmds = []
        self.lists = {}
        cc.fetch_plugin_list = lambda p: self.lists.get(p, [])
        cc.fetch_connectors = lambda root: list(AVAIL)
        cc.run = self._fake_run
        self.write_cache(AVAIL)

    def _fake_run(self, cmd, cwd, timeout=60):
        self.cmds.append((cmd, cwd))

        class R:
            returncode, stdout, stderr = 0, "", ""
        return R()

    def write_cache(self, names):
        c = {"projects": {}}
        if names is not None:
            c["connectors"] = {"ts": time.time(), "names": names}
        jdump(c, os.path.join(self.data, ".ntk-cache.json"))

    def tearDown(self):
        cc.fetch_plugin_list, cc.fetch_connectors, cc.run = self._orig
        os.environ.clear()
        os.environ.update(self.saved)
        self.tmp.cleanup()

    def check(self, path):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            engine.check_path(path, hook=True)
        return buf.getvalue()

    def projects(self):
        return jload(os.path.join(self.data, "projects.json"))["projects"]

    def profiles(self):
        return jload(os.path.join(self.data, "profiles.json"))["profiles"]


class TestResolve(unittest.TestCase):
    def test_union_of_profiles_and_defaults(self):
        profs, d = P()
        r = model.resolve(profs, d, {"profiles": ["py", "ts"]}, AVAIL)
        self.assertEqual(sorted(r["plugins"]), ["a@x", "b@x"])
        self.assertEqual(sorted(r["connectors_on"]), ["Docs", "Slack"])  # Docs from default
        self.assertEqual(r["sources"]["plugin"]["a@x"], ["py"])
        self.assertEqual(r["attached"], ["base", "py", "ts"])
        self.assertEqual(r["own"], ["py", "ts"])

    def test_sources_list_every_granting_profile(self):
        profs = {"x": {"plugins": ["p"], "connectors": []}, "y": {"plugins": ["p"], "connectors": []}}
        profs = {n: model._norm_profile(profs, n) for n in profs}
        r = model.resolve(profs, [], {"profiles": ["x", "y"]})
        self.assertEqual(r["sources"]["plugin"]["p"], ["x", "y"])

    def test_profile_deny_wins_over_allow(self):
        profs, d = P()
        r = model.resolve(profs, d, {"profiles": ["ts", "sec"]}, AVAIL)
        self.assertNotIn("b@x", r["plugins"])
        self.assertIn("c@x", r["plugins"])
        self.assertEqual(r["denied_by"]["plugin"]["b@x"], ["sec"])
        self.assertNotIn("Slack", r["connectors_on"])
        self.assertIn("Slack", r["connectors_off"])

    def test_inline_off_wins_and_inline_on_adds(self):
        profs, d = P()
        e = {"profiles": ["py"], "inline": {"plugins_on": ["z@x"], "plugins_off": ["a@x"],
                                            "connectors_on": ["claude.ai Gmail"], "connectors_off": ["Docs"]}}
        r = model.resolve(profs, d, e, AVAIL)
        self.assertEqual(r["plugins"], ["z@x"])
        self.assertEqual(r["sources"]["plugin"]["z@x"], ["inline"])
        self.assertEqual(r["denied_by"]["plugin"]["a@x"], ["inline"])
        self.assertEqual(r["connectors_on"], ["Gmail"])
        self.assertIn("Docs", r["connectors_off"])

    def test_inline_items_reported(self):
        profs, d = P()
        e = {"profiles": [], "inline": {"plugins_on": ["z@x"], "connectors_off": ["Docs"]}}
        r = model.resolve(profs, d, e, AVAIL)
        self.assertEqual(r["inline_items"], [{"kind": "plugin", "id": "z@x", "value": "on"},
                                             {"kind": "connector", "id": "Docs", "value": "off"}])

    def test_implicit_connector_deny(self):
        profs, d = P()
        r = model.resolve(profs, d, {"profiles": ["py"]}, AVAIL + ["NewConnector"])
        self.assertEqual(r["connectors_on"], ["Docs"])
        self.assertEqual(r["connectors_off"], ["Slack", "Gmail", "Stripe", "NewConnector"])

    def test_default_profiles_applied_with_no_entry(self):
        profs, d = P()
        r = model.resolve(profs, d, None, AVAIL)
        self.assertEqual(r["connectors_on"], ["Docs"])
        self.assertEqual(r["attached"], ["base"])

    def test_unknown_profile_reported_not_raised(self):
        profs, d = P()
        r = model.resolve(profs, d, {"profiles": ["py", "nope"]}, AVAIL)
        self.assertEqual(r["unknown"], ["nope"])
        self.assertEqual(r["plugins"], ["a@x"])


class TestCompat(Env):
    def test_v1_files_load(self):
        jdump({"plugin_accounts": {"_about": "x"}, "profiles": {
            "base": {"plugins": [], "connectors_off": ["Alpha"]},
            "mid": {"extends": "base", "plugins": ["a@x"], "connectors_off": ["Gamma"], "suggested": ["s@x"]},
            "kid": {"extends": "mid", "plugins": ["b@x"], "connectors_on": ["Beta"]}}},
            os.path.join(self.data, "profiles.json"))
        jdump({"projects": {"app": {"profile": "kid", "plugins_on": ["z@x"], "plugins_off": ["a@x"],
                                    "connectors_on": ["claude.ai Alpha"], "connectors_off": ["Delta"]}}},
              os.path.join(self.data, "projects.json"))
        cfg = config.load_config()
        profs, meta = model.load_profiles(cfg)
        self.assertEqual(profs["kid"]["plugins"], ["a@x", "b@x"])  # extends flattened
        self.assertEqual(profs["kid"]["connectors"], ["Beta"])      # connectors_off ignored
        self.assertNotIn("suggested", profs["mid"])
        self.assertEqual(meta["default_profiles"], [])
        e = model.load_projects(cfg)["projects"]["app"]
        self.assertEqual(e["profiles"], ["kid"])
        self.assertEqual(e["inline"], {"plugins_on": ["z@x"], "plugins_off": ["a@x"],
                                       "connectors_on": ["Alpha"], "connectors_off": ["Delta"]})
        r = model.resolve(profs, [], e, ["Alpha", "Beta", "Delta"])
        self.assertEqual(r["plugins"], ["b@x", "z@x"])
        # writes are v2
        model.save_projects(cfg, model.load_projects(cfg))
        w = jload(os.path.join(self.data, "projects.json"))["projects"]["app"]
        self.assertEqual(w["profiles"], ["kid"])
        self.assertNotIn("profile", w)
        self.assertNotIn("plugins_on", w)
        self.assertIn("inline", w)

    def test_underscore_keys_ignored(self):
        cfg = config.load_config()
        self.assertNotIn("_about", model.load_projects(cfg)["projects"])
        profs, meta = model.load_profiles(cfg)
        self.assertNotIn("_note", profs)
        self.assertNotIn("_about", meta["plugin_accounts"])


class TestEvaluate(Env):
    def row(self, rel="app", plugin_list=None, entry=None):
        cfg = config.load_config()
        profs, meta = model.load_profiles(cfg)
        e = entry if entry is not None else model.load_projects(cfg)["projects"].get(rel)
        return engine.evaluate(cfg, rel, e, profs, meta["default_profiles"], {}, plugin_list, AVAIL, None)

    def test_status_new_gone_error_missing_drift_ok(self):
        self.assertEqual(self.row("newone", [], entry=None)["status"], "new")
        self.assertEqual(self.row("zzz", [], entry=None)["status"], "gone")
        self.assertEqual(self.row("app", None)["status"], "error")
        bad = model.norm_entry({"profiles": ["nope"]})
        r = self.row("app", [], entry=bad)
        self.assertEqual(r["status"], "error")
        self.assertIn("nope", r["error"])
        self.assertEqual(self.row("app", [])["status"], "missing")
        pl = [{"id": "a@x", "enabled": True}, {"id": "b@x", "enabled": False}]
        self.assertEqual(self.row("app", pl)["status"], "drift")
        pl = [{"id": "a@x", "enabled": True}, {"id": "b@x", "enabled": True}]
        r = self.row("app", pl)
        self.assertEqual(r["status"], "drift")  # connectors still on
        self.assertTrue(all("connector" in d for d in r["drift"]))

    def test_inline_does_not_change_status_but_is_flagged(self):
        e = model.norm_entry({"profiles": [], "inline": {"plugins_on": ["z@x"]}})
        pl = [{"id": "z@x", "enabled": True}]
        cfg = config.load_config()
        profs, meta = model.load_profiles(cfg)
        cj = {"projects": {os.path.join(self.root, "other"): {"disabledMcpServers": ["claude.ai Slack", "claude.ai Gmail", "claude.ai Stripe"]}}}
        r = engine.evaluate(cfg, "other", e, profs, meta["default_profiles"], cj, pl, AVAIL, None)
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["inline_items"], [{"kind": "plugin", "id": "z@x", "value": "on"}])
        self.assertEqual(r["plugins"][0]["inline"], "on")

    def test_core_itself_never_drifts(self):
        pl = [{"id": "agent-need-to-know@agent-need-to-know", "enabled": True},
              {"id": "devconsole@devconsole", "enabled": True}]
        r = self.row("other", pl)
        self.assertNotIn("devconsole@devconsole", [p["id"] for p in r["plugins"]])
        self.assertNotIn("agent-need-to-know@agent-need-to-know", [p["id"] for p in r["plugins"]])

    def test_missing_hints(self):
        oi = {"orgs": [{"id8": "aaaa1111", "current": False, "plugins": ["eng"]},
                       {"id8": "bbbb2222", "current": True, "plugins": []}]}
        self.assertIn("Apply installs it", cc.missing_hint("p@claude-plugins-official", oi))
        h = cc.missing_hint("eng@synced", oi)
        self.assertIn("add eng to this claude.ai account", h)
        self.assertIn("aaaa1111", h)
        self.assertEqual(cc.missing_hint("zzz@synced", oi), "not synced to any claude.ai account here")
        self.assertEqual(cc.missing_hint("q@local", oi), "not installed — Apply installs it")  # any marketplace installs

    def test_no_connector_drift_when_list_unknown(self):
        cfg = config.load_config()
        profs, meta = model.load_profiles(cfg)
        cj = {"projects": {os.path.join(self.root, "app"): {"disabledMcpServers": ["claude.ai Whatever"]}}}
        e = model.load_projects(cfg)["projects"]["app"]
        pl = [{"id": "a@x", "enabled": True}, {"id": "b@x", "enabled": True}]
        r = engine.evaluate(cfg, "app", e, profs, meta["default_profiles"], cj, pl, [], None)
        self.assertEqual(r["status"], "ok")


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
        self.assertIn("/ntk", out["hookSpecificOutput"]["additionalContext"])
        os.makedirs(os.path.join(self.root, "app", ".claude"))
        jdump({"enabledPlugins": {"a@x": True, "b@x": True}},
              os.path.join(self.root, "app", ".claude", "settings.local.json"))
        jdump({"projects": {os.path.join(self.root, "app"):
                            {"disabledMcpServers": ["claude.ai Gmail", "claude.ai Stripe"], "other": 1}}}, self.cj)
        self.assertEqual(self.check(os.path.join(self.root, "app")), "")

    def test_no_cache_skips_connector_drift(self):
        self.write_cache(None)
        os.makedirs(os.path.join(self.root, "app", ".claude"))
        jdump({"enabledPlugins": {"a@x": True, "b@x": True}},
              os.path.join(self.root, "app", ".claude", "settings.local.json"))
        self.assertEqual(self.check(os.path.join(self.root, "app")), "")

    def test_deny_makes_enabled_plugin_drift(self):
        d = self.projects()
        d["app"]["profiles"] = ["ts", "sec"]
        jdump({"projects": d}, os.path.join(self.data, "projects.json"))
        os.makedirs(os.path.join(self.root, "app", ".claude"))
        jdump({"enabledPlugins": {"b@x": True, "c@x": True}},
              os.path.join(self.root, "app", ".claude", "settings.local.json"))
        out = json.loads(self.check(os.path.join(self.root, "app")))
        self.assertIn("1 plugin(s) to disable", out["hookSpecificOutput"]["additionalContext"])

    def test_subdir_matches_project(self):
        os.makedirs(os.path.join(self.root, "app", "src"))
        self.assertIn("app (profiles py, ts)", self.check(os.path.join(self.root, "app", "src")))

    def test_hidden_folders_silent(self):
        self.assertEqual(self.check(os.path.join(self.root, "_archive")), "")

    def test_missing_config_hints_once(self):
        os.environ["NTK_CONFIG"] = os.path.join(self.tmp.name, "nope", "config.json")
        self.assertIn("no config", self.check("/tmp"))
        self.assertEqual(self.check("/tmp"), "")

    def test_check_is_fast_and_never_shells_out(self):
        t = time.time()
        self.check(os.path.join(self.root, "app"))
        self.assertLess(time.time() - t, 0.2)
        self.assertEqual(self.cmds, [])


class TestClaudeJsonWrite(Env):
    def test_preserves_keys_and_indent(self):
        app = os.path.join(self.root, "app")
        orig = {"numStartups": 5, "oauthAccount": {"organizationUuid": "abc"},
                "projects": {app: {"allowedTools": ["x"], "disabledMcpServers": ["plugin:thing", "claude.ai Old"]},
                             "/other": {"k": 1}}}
        with open(self.cj, "w") as f:
            f.write(json.dumps(orig, indent=4) + "\n")
        os.chmod(self.cj, 0o600)
        self.assertTrue(cc.write_disabled_servers(app, ["Alpha", "Gamma"]))
        text = slurp(self.cj)
        self.assertTrue(text.startswith('{\n    "numStartups"'))
        self.assertTrue(text.endswith("\n"))
        d = json.loads(text)
        self.assertEqual(d["numStartups"], 5)
        self.assertEqual(d["projects"]["/other"], {"k": 1})
        self.assertEqual(d["projects"][app]["allowedTools"], ["x"])
        self.assertEqual(d["projects"][app]["disabledMcpServers"], ["plugin:thing", "claude.ai Alpha", "claude.ai Gamma"])
        self.assertEqual(os.stat(self.cj).st_mode & 0o777, 0o600)
        self.assertFalse(cc.write_disabled_servers(app, ["Alpha", "Gamma"]))  # no-op
        self.assertEqual([f for f in os.listdir(self.t) if f.startswith(".dc-tmp-")], [])

    def test_creates_project_entry(self):
        jdump({"projects": {}, "x": 1}, self.cj)
        cc.write_disabled_servers(os.path.join(self.root, "newone"), ["Alpha"])
        d = jload(self.cj)
        self.assertEqual(d["x"], 1)
        self.assertEqual(d["projects"][os.path.join(self.root, "newone")], {"disabledMcpServers": ["claude.ai Alpha"]})


class TestEdits(Env):
    """attach / detach / profile edits / inline write the data files (no-apply path)."""

    def setUp(self):
        super().setUp()
        self.cfg = config.load_config()
        self.out = []

    def test_attach_creates_entry_and_detach_removes(self):
        self.assertEqual(engine.do_attach(self.cfg, "newone", "py", out=self.out.append, apply=False), 0)
        self.assertEqual(self.projects()["newone"], {"profiles": ["py"]})
        self.assertEqual(engine.do_attach(self.cfg, "newone", "py", out=self.out.append, apply=False), 0)  # idempotent
        self.assertEqual(self.projects()["newone"]["profiles"], ["py"])
        self.assertEqual(engine.do_attach(self.cfg, "newone", "py", out=self.out.append, apply=False, attach=False), 0)
        self.assertEqual(self.projects()["newone"], {"profiles": []})
        self.assertNotEqual(engine.do_attach(self.cfg, "newone", "nope", out=self.out.append, apply=False), 0)
        self.assertEqual(self.cmds, [])
        self.assertIn("newone\tprofile:py", slurp(self.cfg.log_path))

    def test_attach_preserves_top_level_and_notes(self):
        engine.do_attach(self.cfg, "app", "sec", out=self.out.append, apply=False)
        raw = jload(os.path.join(self.data, "projects.json"))
        self.assertEqual(raw["_about"], "c")
        self.assertEqual(raw["projects"]["app"]["profiles"], ["py", "ts", "sec"])
        self.assertEqual(raw["projects"]["app"]["notes"], "n")

    def test_detach_default_profile_explained(self):
        rc = engine.do_attach(self.cfg, "app", "base", out=self.out.append, apply=False, attach=False)
        self.assertEqual(rc, 2)
        self.assertIn("default", self.out[-1])

    def test_attach_removes_from_recommended(self):
        d = self.projects()
        d["app"]["recommended"] = ["sec", "py"]
        jdump({"projects": d}, os.path.join(self.data, "projects.json"))
        engine.do_attach(self.cfg, "app", "sec", out=self.out.append, apply=False)
        self.assertEqual(self.projects()["app"]["recommended"], ["py"])

    def test_attach_then_apply_runs_plugin_command(self):
        self.lists[os.path.join(self.root, "newone")] = []
        d = self.projects()
        d["newone"] = {"profiles": []}
        jdump({"projects": d}, os.path.join(self.data, "projects.json"))
        pf = jload(os.path.join(self.data, "profiles.json"))
        pf["profiles"]["py"]["plugins"] = ["a@claude-plugins-official"]
        jdump(pf, os.path.join(self.data, "profiles.json"))
        engine.do_attach(self.cfg, "newone", "py", out=self.out.append)
        self.assertIn((["claude", "plugin", "install", "a@claude-plugins-official", "--scope", "local"],
                       os.path.join(self.root, "newone")), self.cmds)

    def test_profile_add_remove_plugin_and_connector(self):
        rc, _ = engine.profile_edit(self.cfg, "py", "add", "plugin", "n@x", apply=False, out=self.out.append)
        self.assertEqual(rc, 0)
        rc, _ = engine.profile_edit(self.cfg, "py", "add", "connector", "claude.ai Gmail", apply=False, out=self.out.append)
        p = self.profiles()["py"]
        self.assertEqual(p["plugins"], ["a@x", "n@x"])
        self.assertEqual(p["connectors"], ["Gmail"])
        self.assertEqual(p["group"], "code")
        raw = jload(os.path.join(self.data, "profiles.json"))
        self.assertEqual(raw["default_profiles"], ["base"])
        self.assertEqual(raw["_about"], "comment")
        self.assertEqual(raw["plugin_accounts"]["a@x"], "any")
        engine.profile_edit(self.cfg, "py", "remove", "plugin", "a@x", apply=False, out=self.out.append)
        self.assertEqual(self.profiles()["py"]["plugins"], ["n@x"])
        rc, _ = engine.profile_edit(self.cfg, "py", "remove", "plugin", "nope", apply=False, out=self.out.append)
        self.assertEqual(rc, 2)
        rc, _ = engine.profile_edit(self.cfg, "zzz", "add", "plugin", "q", apply=False, out=self.out.append)
        self.assertEqual(rc, 2)

    def test_profile_dry_run_writes_nothing_and_lists_affected(self):
        before = slurp(os.path.join(self.data, "profiles.json"))
        rc, aff = engine.profile_edit(self.cfg, "py", "add", "connector", "Gmail", dry=True, out=self.out.append)
        self.assertEqual(rc, 0)
        self.assertEqual(slurp(os.path.join(self.data, "profiles.json")), before)
        self.assertEqual([a["path"] for a in aff], ["app"])
        self.assertIsInstance(aff[0]["changes"], list)
        self.assertTrue(any("affects 1 project" in m for m in self.out))
        self.assertEqual(self.cmds, [])

    def test_default_profile_edit_affects_all_projects(self):
        rc, aff = engine.profile_edit(self.cfg, "base", "add", "connector", "Gmail", dry=True, out=self.out.append)
        self.assertEqual(sorted(a["path"] for a in aff), ["app", "other"])

    def test_profile_edit_applies_to_attached_projects_only(self):
        self.lists[os.path.join(self.root, "app")] = [{"id": "a@x", "enabled": True}, {"id": "b@x", "enabled": True},
                                                      {"id": "n@claude-plugins-official", "enabled": False}]
        engine.profile_edit(self.cfg, "py", "add", "plugin", "n@claude-plugins-official", out=self.out.append)
        cmds = [c for c, cwd in self.cmds]
        self.assertIn(["claude", "plugin", "enable", "n@claude-plugins-official", "--scope", "local"], cmds)
        self.assertTrue(all(cwd == os.path.join(self.root, "app") for c, cwd in self.cmds))

    def test_profile_new_from_project_moves_inline(self):
        d = self.projects()
        d["other"] = {"profiles": [], "inline": {"plugins_on": ["z@x"], "connectors_on": ["Gmail"], "plugins_off": ["q@x"]}}
        jdump({"projects": d}, os.path.join(self.data, "projects.json"))
        rc = engine.profile_new(self.cfg, "extra", "desc", "grp", from_project="other", out=self.out.append)
        self.assertEqual(rc, 0)
        p = self.profiles()["extra"]
        self.assertEqual((p["plugins"], p["connectors"], p["group"], p["description"]), (["z@x"], ["Gmail"], "grp", "desc"))
        o = self.projects()["other"]
        self.assertEqual(o["profiles"], ["extra"])
        self.assertEqual(o["inline"], {"plugins_off": ["q@x"]})
        self.assertNotEqual(engine.profile_new(self.cfg, "extra", out=self.out.append), 0)  # exists
        self.assertNotEqual(engine.profile_new(self.cfg, "bad name!", out=self.out.append), 0)

    def test_profile_new_empty(self):
        self.assertEqual(engine.profile_new(self.cfg, "docs", "d", "", out=self.out.append), 0)
        self.assertEqual(self.profiles()["docs"]["plugins"], [])

    def test_profile_delete_refuses_when_attached(self):
        rc = engine.profile_delete(self.cfg, "py", out=self.out.append)
        self.assertEqual(rc, 2)
        self.assertIn("app", self.out[-1])
        self.assertIn("py", self.profiles())
        self.assertEqual(engine.profile_delete(self.cfg, "base", out=self.out.append), 2)  # default
        engine.do_attach(self.cfg, "app", "py", out=self.out.append, apply=False, attach=False)
        self.assertEqual(engine.profile_delete(self.cfg, "py", out=self.out.append), 0)
        self.assertNotIn("py", self.profiles())

    def test_inline_set_and_clear(self):
        out = self.out.append
        engine.do_inline(self.cfg, "app", "plugin", "z@x", "on", out=out, apply=False)
        self.assertEqual(self.projects()["app"]["inline"], {"plugins_on": ["z@x"]})
        engine.do_inline(self.cfg, "app", "plugin", "z@x", "off", out=out, apply=False)  # not granted: no exception needed
        self.assertNotIn("inline", self.projects()["app"])
        engine.do_inline(self.cfg, "app", "plugin", "a@x", "off", out=out, apply=False)
        self.assertEqual(self.projects()["app"]["inline"], {"plugins_off": ["a@x"]})
        engine.do_inline(self.cfg, "app", "plugin", "a@x", "clear", out=out, apply=False)
        self.assertNotIn("inline", self.projects()["app"])
        engine.do_inline(self.cfg, "app", "connector", "claude.ai Gmail", "on", out=out, apply=False)
        self.assertEqual(self.projects()["app"]["inline"], {"connectors_on": ["Gmail"]})
        engine.do_inline(self.cfg, "app", "connector", "Slack", "off", out=out, apply=False)
        self.assertEqual(self.projects()["app"]["inline"], {"connectors_on": ["Gmail"], "connectors_off": ["Slack"]})
        engine.do_inline(self.cfg, "app", "plugin", "a@x", "on", out=out, apply=False)  # already on via py
        self.assertNotIn("plugins_on", self.projects()["app"]["inline"])

    def test_inline_on_refused_when_profile_denies(self):
        d = self.projects()
        d["app"]["profiles"] = ["sec"]
        jdump({"projects": d}, os.path.join(self.data, "projects.json"))
        rc = engine.do_inline(self.cfg, "app", "plugin", "b@x", "on", out=self.out.append, apply=False)
        self.assertEqual(rc, 2)
        self.assertIn("denied by sec", self.out[-1])
        self.assertNotIn("inline", self.projects()["app"])

    def test_inline_needs_entry(self):
        self.assertEqual(engine.do_inline(self.cfg, "newone", "plugin", "z@x", "on", out=self.out.append, apply=False), 2)


class TestApplyAll(Env):
    def test_dry_run_output_shape_and_no_writes(self):
        self.lists[os.path.join(self.root, "app")] = [{"id": "a@x", "enabled": True}, {"id": "b@x", "enabled": False}]
        self.lists[os.path.join(self.root, "other")] = []
        out = []
        cj_before = slurp(self.cj)
        rc = engine.apply_all(config.load_config(), dry=True, out=out.append)
        self.assertEqual(rc, 0)
        self.assertEqual(out[0], "(dry run: nothing will be changed)")
        app = [l for l in out if l.startswith("app: ")][0]
        self.assertIn("+plugin b@x", app)
        self.assertIn("connector Gmail on->off", app)
        self.assertIn("connector Stripe on->off", app)
        self.assertTrue(any(l.startswith("other: ") and "connector Slack on->off" in l for l in out))
        self.assertTrue(out[-1].startswith("totals: 2 of 2 project(s) would change"))
        self.assertEqual(slurp(self.cj), cj_before)
        self.assertEqual(self.cmds, [])
        self.assertFalse(os.path.exists(os.path.join(self.data, "backups")))

    def test_real_apply_backs_up_once_and_continues_past_failures(self):
        self.lists[os.path.join(self.root, "app")] = [{"id": "a@x", "enabled": True}, {"id": "b@x", "enabled": True}]
        self.lists[os.path.join(self.root, "other")] = None  # plugin list fails
        out = []
        rc = engine.apply_all(config.load_config(), out=out.append)
        self.assertEqual(rc, 1)
        self.assertTrue(any(l.startswith("other: FAILED") for l in out))
        self.assertTrue(any(l.startswith("failure: other") for l in out))
        backups = os.listdir(os.path.join(self.data, "backups"))
        self.assertEqual(len(backups), 1)
        d = jload(self.cj)
        self.assertEqual(sorted(d["projects"][os.path.join(self.root, "app")]["disabledMcpServers"]),
                         ["claude.ai Gmail", "claude.ai Stripe"])

    def test_unknown_profile_is_failure_not_crash(self):
        d = self.projects()
        d["app"]["profiles"].append("ghost")
        jdump({"projects": d}, os.path.join(self.data, "projects.json"))
        out = []
        rc = engine.apply_all(config.load_config(), dry=True, out=out.append)
        self.assertEqual(rc, 1)
        self.assertTrue(any("unknown profile" in l for l in out))


class TestServer(Env):
    def setUp(self):
        super().setUp()
        self.token = "tok"
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(config.load_config(), self.token))
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        super().tearDown()

    def req(self, method, path, body=None, token=True, host=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        h = {"Content-Type": "application/json"}
        if token:
            h["X-Ntk-Token"] = self.token
        if host:
            h["Host"] = host
        c.request(method, path, json.dumps(body) if body is not None else None, h)
        r = c.getresponse()
        data = json.loads(r.read() or b"{}")
        return r.status, data

    def test_guards(self):
        self.assertEqual(self.req("POST", "/api/apply", {"path": "app"}, token=False)[0], 403)
        self.assertEqual(self.req("GET", "/api/state", host="evil.example")[0], 403)

    def test_state_shape(self):
        code, st = self.req("GET", "/api/state")
        self.assertEqual(code, 200)
        self.assertEqual(st["default_profiles"], ["base"])
        self.assertEqual(st["profiles"]["py"]["projects"], ["app"])
        self.assertEqual(st["profiles"]["py"]["group"], "code")
        self.assertEqual(st["available_connectors"], AVAIL)
        self.assertIn("a@x", st["known_plugins"])
        self.assertNotIn("_about", st["known_plugins"])
        app = [p for p in st["projects"] if p["path"] == "app"][0]
        for k in ("attached", "recommended", "desired", "plugins", "connectors", "inline_items", "missing_hints"):
            self.assertIn(k, app)

    def test_profile_dry_run_over_http_writes_nothing(self):
        before = slurp(os.path.join(self.data, "profiles.json"))
        code, r = self.req("POST", "/api/profile", {"name": "py", "op": "add", "kind": "plugin", "id": "n@x", "dry_run": True})
        self.assertEqual(code, 200)
        self.assertEqual([a["path"] for a in r["affected"]], ["app"])
        self.assertNotIn("state", r)
        self.assertEqual(slurp(os.path.join(self.data, "profiles.json")), before)

    def test_attach_detach_inline_and_new_over_http(self):
        # the plugin list stub reports nothing missing for these ids, so applies are no-ops or stubbed
        code, r = self.req("POST", "/api/profile-new", {"name": "docs", "description": "d", "group": "g"})
        self.assertEqual((code, r["ok"]), (200, True))
        self.assertIn("docs", r["state"]["profiles"])
        code, r = self.req("POST", "/api/attach", {"path": "other", "profile": "docs"})
        self.assertEqual(code, 200)
        self.assertEqual(self.projects()["other"]["profiles"], ["docs"])
        code, r = self.req("POST", "/api/detach", {"path": "other", "profile": "docs"})
        self.assertEqual(self.projects()["other"]["profiles"], [])
        code, r = self.req("POST", "/api/inline", {"path": "other", "kind": "plugin", "id": "z@x", "value": "on"})
        self.assertEqual(code, 200)
        self.assertEqual(self.projects()["other"]["inline"], {"plugins_on": ["z@x"]})
        self.assertEqual(self.req("POST", "/api/attach", {"path": "../x", "profile": "docs"})[0], 400)
        self.assertEqual(self.req("POST", "/api/inline", {"path": "other", "kind": "bad", "id": "z", "value": "on"})[0], 400)
        self.assertEqual(self.req("POST", "/api/nope", {})[0], 404)


class TestCapabilities(Env):
    def setUp(self):
        super().setUp()
        self.cfg = config.load_config()
        self.out = []
        self.capf = os.path.join(self.data, "capabilities.json")
        self.app = os.path.join(self.root, "app")
        self.lists[self.app] = [{"id": "a@x", "enabled": True}, {"id": "b@x", "enabled": True}]

    def caps(self):
        return jload(self.capf)["capabilities"]

    def statuses(self):
        return {i: e["status"] for i, e in self.caps().items()}

    def manifest(self, org, names, desc=None):
        d = os.path.join(self.t, "home", "plugins", "synced", org + "_x")
        os.makedirs(d, exist_ok=True)
        jdump({"plugins": [{"name": n} for n in names]}, os.path.join(d, "manifest.json"))
        for n in names:
            os.makedirs(os.path.join(d, n, ".claude-plugin"), exist_ok=True)
            jdump({"name": n, "description": desc or ""}, os.path.join(d, n, ".claude-plugin", "plugin.json"))

    def test_first_run_seeds_everything_active(self):
        self.assertFalse(os.path.exists(self.capf))
        engine.scan(self.cfg, fresh=True)
        st = self.statuses()
        self.assertEqual(set(st.values()), {"active"})
        for i in ("a@x", "b@x", "Gmail", "Docs", "c@x"):  # seen plugins, connectors, profile refs
            self.assertIn(i, st)
        self.assertEqual(self.caps()["c@x"]["last_seen"], "")  # referenced, never seen
        self.assertEqual(self.caps()["Gmail"]["kind"], "connector")

    def test_new_detected_after_seed_and_gone(self):
        engine.scan(self.cfg, fresh=True)
        self.lists[self.app].append({"id": "n@x", "enabled": False})
        st = engine.scan(self.cfg, fresh=True)
        self.assertEqual(self.statuses()["n@x"], "new")
        self.assertEqual(st["inbox"]["new"], ["n@x"])
        # b@x disappears everywhere
        self.lists[self.app] = [{"id": "a@x", "enabled": True}]
        st = engine.scan(self.cfg, fresh=True)
        self.assertEqual(self.statuses()["b@x"], "gone")
        self.assertIn("b@x", st["inbox"]["gone"])
        self.assertEqual(self.statuses()["c@x"], "active")  # never seen: not gone
        self.assertTrue(self.caps()["b@x"]["last_seen"])
        # comes back
        self.lists[self.app].append({"id": "b@x", "enabled": True})
        engine.scan(self.cfg, fresh=True)
        self.assertEqual(self.statuses()["b@x"], "active")

    def test_org_availability_keeps_item_not_gone(self):
        self.manifest("orga1111", ["eng"], desc="Engineering skills")
        self.lists[self.app].append({"id": "eng@synced", "enabled": True})
        engine.scan(self.cfg, fresh=True)
        self.lists[self.app] = [{"id": "a@x", "enabled": True}]
        engine.scan(self.cfg, fresh=True)
        self.assertEqual(self.statuses()["eng@synced"], "active")
        self.assertEqual(self.caps()["eng@synced"]["seen_on_orgs"], ["orga1111"])

    def login(self, org):
        jdump({"projects": {}, "oauthAccount": {"organizationUuid": org}}, self.cj)

    def test_switching_accounts_merges_orgs_and_never_marks_gone(self):
        # Gmail is a connector only the first account has
        self.login("orga1111-aaaa")
        engine.scan(self.cfg, fresh=True)
        self.assertEqual(self.caps()["Gmail"]["seen_on_orgs"], ["orga1111"])
        # log into the second account: Gmail is absent there, Docs is on both
        self.login("orgb2222-bbbb")
        cc.fetch_connectors = lambda root: ["Docs"]
        self.write_cache(["Docs"])
        engine.scan(self.cfg, fresh=True)
        self.assertEqual(self.statuses()["Gmail"], "active")
        self.assertEqual(self.caps()["Docs"]["seen_on_orgs"], ["orga1111", "orgb2222"])
        # an item already marked gone by an older ntk comes back on the other login
        d = jload(self.capf)
        d["capabilities"]["Gmail"]["status"] = "gone"
        jdump(d, self.capf)
        engine.scan(self.cfg, fresh=True)
        self.assertEqual(self.statuses()["Gmail"], "active")
        # but something that vanishes from the current login's own org is gone
        cc.fetch_connectors = lambda root: ["Slack"]  # an empty list means the fetch failed
        self.write_cache(["Slack"])
        engine.scan(self.cfg, fresh=True)
        self.assertEqual(self.statuses()["Docs"], "gone")

    def test_incomplete_scan_never_marks_gone(self):
        engine.scan(self.cfg, fresh=True)
        cc.fetch_connectors = lambda root: []
        self.write_cache(None)
        self.lists[self.app] = []
        engine.scan(self.cfg, fresh=True)
        self.assertNotIn("gone", self.statuses().values())

    def test_inbox_output(self):
        self.manifest("orga1111", ["eng"], desc="Engineering skills")
        engine.scan(self.cfg, fresh=True)
        self.manifest("orga1111", ["eng", "newone"], desc="Shiny")
        engine.scan(self.cfg, fresh=True)
        self.lists[self.app] = [{"id": "a@x", "enabled": True}]
        engine.scan(self.cfg, fresh=True)
        out = []
        lifecycle.show_inbox(self.cfg, out=out.append)
        text = "\n".join(out)
        self.assertIn("New (1)", text)
        self.assertIn("newone@synced", text)
        self.assertIn("orga1111", text)
        self.assertIn("Shiny", text)
        self.assertIn("Gone (1)", text)
        self.assertIn("b@x", text)
        self.assertIn("ts", text)  # profile still referencing the gone item

    def test_ignore(self):
        engine.scan(self.cfg, fresh=True)
        self.lists[self.app].append({"id": "n@x", "enabled": False})
        engine.scan(self.cfg, fresh=True)
        lifecycle.capability_ignore(self.cfg, "n@x", out=self.out.append)
        self.assertEqual(self.statuses()["n@x"], "ignored")
        self.assertEqual(engine.scan(self.cfg, fresh=True)["inbox"]["new"], [])
        self.assertEqual(self.statuses()["n@x"], "ignored")

    def test_adopt_writes_profiles_and_dry_run_shape(self):
        engine.scan(self.cfg, fresh=True)
        self.lists[self.app].append({"id": "n@x", "enabled": False})
        engine.scan(self.cfg, fresh=True)
        before = slurp(os.path.join(self.data, "profiles.json"))
        rc, aff, profs = lifecycle.capability_adopt(self.cfg, "n@x", ["py", "base"], dry=True, out=self.out.append)
        self.assertEqual(rc, 0)
        self.assertEqual(profs, ["py", "base"])
        self.assertEqual(sorted(a["path"] for a in aff), ["app", "other"])  # base is a default: all projects
        self.assertEqual(slurp(os.path.join(self.data, "profiles.json")), before)
        self.assertEqual(self.statuses()["n@x"], "new")
        rc, _, _ = lifecycle.capability_adopt(self.cfg, "n@x", ["py"], apply=False, out=self.out.append)
        self.assertEqual(self.profiles()["py"]["plugins"], ["a@x", "n@x"])
        self.assertEqual(self.statuses()["n@x"], "active")
        self.assertEqual(lifecycle.capability_adopt(self.cfg, "n@x", ["nope"], out=self.out.append)[0], 2)

    def test_adopt_connector_global(self):
        lifecycle.capability_adopt(self.cfg, "Gmail", ["base"], apply=False, out=self.out.append)
        self.assertIn("Gmail", self.profiles()["base"]["connectors"])

    def test_retire_removes_everywhere_and_stays_retired(self):
        pf = jload(os.path.join(self.data, "profiles.json"))
        pf["profiles"]["sec"]["plugins"].append("a@x")
        jdump(pf, os.path.join(self.data, "profiles.json"))
        d = self.projects()
        d["other"]["inline"] = {"plugins_on": ["a@x"], "plugins_off": ["a@x"]}
        jdump({"projects": d}, os.path.join(self.data, "projects.json"))
        engine.scan(self.cfg, fresh=True)
        rc, aff, hit = lifecycle.capability_retire(self.cfg, "a@x", dry=True, out=self.out.append)
        self.assertEqual(sorted(hit), ["py", "sec"])
        self.assertEqual(sorted(a["path"] for a in aff), ["app", "other"])
        self.assertIn("a@x", self.profiles()["py"]["plugins"])  # dry run wrote nothing
        lifecycle.capability_retire(self.cfg, "a@x", apply=False, out=self.out.append)
        for n, p in self.profiles().items():
            self.assertNotIn("a@x", p["plugins"])
        self.assertNotIn("inline", self.projects()["other"])
        self.assertEqual(self.statuses()["a@x"], "retired")
        # still installed and listed: stays retired, shows under scan -v data
        st = engine.scan(self.cfg, fresh=True)
        self.assertEqual(self.statuses()["a@x"], "retired")
        self.assertEqual(st["inbox"], {"new": [], "gone": []})
        row = [c for c in st["capabilities"] if c["id"] == "a@x"][0]
        self.assertTrue(row["seen_now"])

    def test_retire_applies_to_affected(self):
        self.lists[self.app] = [{"id": "a@x", "enabled": True}, {"id": "b@x", "enabled": True}]
        engine.scan(self.cfg, fresh=True)
        lifecycle.capability_retire(self.cfg, "a@x", out=self.out.append)
        self.assertIn((["claude", "plugin", "disable", "a@x", "--scope", "local"], self.app), self.cmds)

    def test_list_filters(self):
        engine.scan(self.cfg, fresh=True)
        out = []
        lifecycle.list_caps(self.cfg, "retired", out=out.append)
        self.assertEqual(out, [])
        lifecycle.list_caps(self.cfg, "active", out=out.append)
        self.assertTrue(any(l.endswith("Gmail") for l in out))

    def test_hook_hint_once_per_day_and_file_only(self):
        engine.scan(self.cfg, fresh=True)
        self.assertEqual(self.check(os.path.join(self.root, "newone")).count("capabilit"), 0)
        d = jload(self.capf)
        d["capabilities"]["zz@x"] = {"kind": "plugin", "status": "new", "first_seen": "t", "last_seen": "t",
                                     "seen_on_orgs": [], "note": ""}
        jdump(d, self.capf)
        self.cmds.clear()
        first = self.check(os.path.join(self.root, "newone"))
        self.assertIn("ntk: 1 new capability waiting in the inbox (run `ntk` to review).", first)
        self.assertEqual(self.check(os.path.join(self.root, "newone")).count("waiting in the inbox"), 0)
        self.assertEqual(self.cmds, [])
        # silent path with a hint when a new day starts
        sp = os.path.join(os.path.dirname(self.cfgfile), ".capabilities-hint.json")
        jdump({"date": "2000-01-01"}, sp)
        d["capabilities"]["zz2@x"] = dict(d["capabilities"]["zz@x"])
        jdump(d, self.capf)
        os.makedirs(os.path.join(self.root, "app", ".claude"))
        jdump({"enabledPlugins": {"a@x": True, "b@x": True}},
              os.path.join(self.root, "app", ".claude", "settings.local.json"))
        jdump({"projects": {self.app: {"disabledMcpServers": ["claude.ai Gmail", "claude.ai Stripe"]}}}, self.cj)
        out = json.loads(self.check(self.app))
        self.assertEqual(out["hookSpecificOutput"]["additionalContext"],
                         "ntk: 2 new capabilities waiting in the inbox (run `ntk` to review).")


class TestDenyAndDefaults(Env):
    def setUp(self):
        super().setUp()
        self.cfg = config.load_config()
        self.out = []

    def test_deny_undeny_and_extra_keys_kept(self):
        pf = jload(os.path.join(self.data, "profiles.json"))
        pf["profiles"]["py"]["_about"] = "keep me"
        jdump(pf, os.path.join(self.data, "profiles.json"))
        engine.profile_edit(self.cfg, "py", "deny", "plugin", "q@x", apply=False, out=self.out.append)
        engine.profile_edit(self.cfg, "py", "deny", "connector", "claude.ai Gmail", apply=False, out=self.out.append)
        p = self.profiles()["py"]
        self.assertEqual((p["deny_plugins"], p["deny_connectors"], p["_about"]), (["q@x"], ["Gmail"], "keep me"))
        self.assertEqual(engine.profile_edit(self.cfg, "py", "deny", "plugin", "q@x", apply=False, out=self.out.append)[0], 0)
        engine.profile_edit(self.cfg, "py", "undeny", "plugin", "q@x", apply=False, out=self.out.append)
        self.assertNotIn("deny_plugins", self.profiles()["py"])
        self.assertEqual(engine.profile_edit(self.cfg, "py", "undeny", "plugin", "q@x", apply=False, out=self.out.append)[0], 2)

    def test_profile_default_add_remove_dry(self):
        path = os.path.join(self.data, "profiles.json")
        before = slurp(path)
        rc, aff = engine.profile_default(self.cfg, "add", "py", dry=True, out=self.out.append)
        self.assertEqual((rc, sorted(a["path"] for a in aff)), (0, ["app", "other"]))
        self.assertEqual(slurp(path), before)
        engine.profile_default(self.cfg, "add", "py", apply=False, out=self.out.append)
        self.assertEqual(jload(path)["default_profiles"], ["base", "py"])
        engine.profile_default(self.cfg, "remove", "base", apply=False, out=self.out.append)
        self.assertEqual(jload(path)["default_profiles"], ["py"])
        self.assertEqual(engine.profile_default(self.cfg, "remove", "base", apply=False, out=self.out.append)[0], 2)
        self.assertEqual(engine.profile_default(self.cfg, "add", "zzz", apply=False, out=self.out.append)[0], 2)


class TestCapabilityApi(Env):
    def setUp(self):
        super().setUp()
        self.token = "tok"
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(config.load_config(), self.token))
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        super().tearDown()

    def post(self, path, body, token=True):
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1], timeout=30)
        h = {"Content-Type": "application/json"}
        if token:
            h["X-Ntk-Token"] = self.token
        c.request("POST", path, json.dumps(body), h)
        r = c.getresponse()
        return r.status, json.loads(r.read() or b"{}")

    def test_capability_endpoints(self):
        self.assertEqual(self.post("/api/capability", {"action": "ignore", "id": "x"}, token=False)[0], 403)
        self.assertEqual(self.post("/api/capability", {"action": "adopt", "id": "x"})[0], 400)  # no profiles
        code, r = self.post("/api/capability", {"action": "adopt", "id": "n@x", "profiles": ["py"], "dry_run": True})
        self.assertEqual(code, 200)
        self.assertEqual([a["path"] for a in r["affected"]], ["app"])
        self.assertNotIn("n@x", self.profiles()["py"]["plugins"])
        code, r = self.post("/api/capability", {"action": "retire", "id": "a@x", "dry_run": True})
        self.assertEqual(r["profiles"], ["py"])
        code, r = self.post("/api/capability", {"action": "ignore", "id": "n@x"})
        self.assertEqual(code, 200)
        self.assertIn("inbox", r["state"])
        code, r = self.post("/api/profile-default", {"op": "add", "name": "py", "dry_run": True})
        self.assertEqual(code, 200)
        self.assertEqual(len(r["affected"]), 2)
        code, r = self.post("/api/profile", {"name": "py", "op": "deny", "kind": "plugin", "id": "q@x", "dry_run": True})
        self.assertEqual(code, 200)


if __name__ == "__main__":
    unittest.main()
