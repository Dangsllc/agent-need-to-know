"""Antigravity adapter: fixtures live under NTK_GEMINI_HOME (an empty temp dir set by
test_core.Env); the real ~/.gemini and the real `agy` are never touched."""
import json
import os
import sys
import unittest
from urllib.parse import quote

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
from ntk_pkg import adapters, config, engine, history  # noqa: E402
from ntk_pkg.adapters import antigravity as ag  # noqa: E402
from test_core import Env, PROFILES, jdump, jload, slurp  # noqa: E402

TOK1, TOK2, TOK3 = "SECRET-TOKEN-plug-91f3", "SECRET-HEADER-glob-77aa", "SECRET-ENV-xx-5521"
PLUG = "chrome-devtools-plugin"
S1 = "antigravity:mcp:%s_chrome-devtools" % PLUG   # from a plugin: internal name
S2 = "antigravity:mcp:memory"                       # from the global mcp_config.json
R1 = "mcp(%s_chrome-devtools/*)" % PLUG
R2 = "mcp(memory/*)"


def slurp_any(path):
    with open(path, errors="replace") as f:
        return f.read()


def uri(path):
    return "file://" + quote(path)


class AgEnv(Env):
    def setUp(self):
        super().setUp()
        self.home = os.environ["NTK_GEMINI_HOME"]
        self.cdir = os.path.join(self.home, "config")
        self._orig_ag = (ag.fetch_plugin_list, ag.run)
        self.ag_plugins = []
        self.ag_cmds = []
        ag.fetch_plugin_list = lambda p: list(self.ag_plugins)
        ag.run = self._ag_run
        self.app = os.path.join(self.root, "app")
        self.other = os.path.join(self.root, "other")
        self.lists[self.app] = [{"id": "a@x", "enabled": True, "scope": "local"},
                                {"id": "b@x", "enabled": True, "scope": "local"}]

    def tearDown(self):
        ag.fetch_plugin_list, ag.run = self._orig_ag
        super().tearDown()

    def _ag_run(self, cmd, cwd, timeout=60):
        self.ag_cmds.append((cmd, cwd))

        class R:
            returncode, stdout, stderr = 0, "", ""
        return R()

    # ---- fixtures
    def fixture_home(self, tokens=True):
        os.makedirs(os.path.join(self.cdir, "plugins", PLUG))
        os.makedirs(os.path.join(self.cdir, "projects"))
        jdump({"mcpServers": {"chrome-devtools": {"command": "x", "env": {"TOKEN": TOK1 if tokens else ""},
                                                  "headers": {"Authorization": "Bearer " + TOK3}}}},
              os.path.join(self.cdir, "plugins", PLUG, "mcp_config.json"))
        jdump({"description": "Chrome tools"}, os.path.join(self.cdir, "plugins", PLUG, "plugin.json"))
        jdump({"mcpServers": {"memory": {"url": "https://x", "headers": {"Authorization": "Bearer " + TOK2}}}},
              os.path.join(self.cdir, "mcp_config.json"))

    def project(self, pid, folders, deny=None, grants=True, name=None, raw=None):
        d = {"id": pid, "name": name or pid, "isWorkspaceOnly": False,
             "projectResources": {"resources": [{"folderUri": uri(f)} if i % 2 == 0 else
                                                {"gitFolder": {"defaultBranch": "main", "folderUri": uri(f)}}
                                                for i, f in enumerate(folders)]}}
        if grants:
            d["permissionGrants"] = {"permissionGrants": {"allow": ["command(git)"], "deny": list(deny or [])},
                                     "v2Migrated": True}
        d["settings"] = {"theme": "dark", "n": 3}
        d["updatedAt"] = "2026-01-01T00:00:00Z"
        if raw:
            d.update(raw)
        p = os.path.join(self.cdir, "projects", pid + ".json")
        with open(p, "w") as f:
            json.dump(d, f, indent=2)
            f.write("\n")
        return p

    def profile(self, grants, to=("app",)):
        prof = dict(PROFILES, profiles=dict(PROFILES["profiles"],
                                            ag={"plugins": [], "connectors": list(grants)}))
        jdump(prof, os.path.join(self.data, "profiles.json"))
        pr = jload(os.path.join(self.data, "projects.json"))
        for r in to:
            pr["projects"][r]["profiles"] = pr["projects"][r].get("profiles", []) + ["ag"]
        jdump(pr, os.path.join(self.data, "projects.json"))

    def deny(self, path):
        return jload(path)["permissionGrants"]["permissionGrants"]["deny"]

    def cfg(self):
        return config.load_config()

    def apply_all(self):
        out = []
        rc = engine.apply_all(self.cfg(), out=out.append)
        return rc, out

    def row(self, rel):
        st = engine.scan(self.cfg(), fresh=True)
        return next(r for r in st["projects"] if r["path"] == rel)

    def log(self):
        return slurp(self.cfg().log_path)


class Basics(AgEnv):
    def test_available_follows_config_dir(self):
        a = ag.AntigravityAdapter()
        self.assertFalse(a.available())
        self.assertEqual([x.name for x in adapters.all()], ["claude"])
        os.makedirs(self.home)
        self.assertFalse(a.available())
        os.makedirs(self.cdir)
        self.assertTrue(a.available())
        self.assertEqual([x.name for x in adapters.all()], ["claude", "antigravity"])

    def test_gemini_home_override(self):
        self.assertEqual(ag.gemini_home(), os.environ["NTK_GEMINI_HOME"])

    def test_discovery_ids(self):
        self.fixture_home()
        a = ag.AntigravityAdapter()
        self.assertEqual(a.list_available(self.root), sorted([S1, S2]))
        self.assertEqual(set(a.discover_capabilities([[{"id": "antigravity:plugin:p", "enabled": True}, {"id": "x@y"}]],
                                                     {}, [S1, "Docs"])),
                         {"antigravity:plugin:p", S1})

    def test_empty_and_broken_mcp_config(self):
        self.fixture_home()
        open(os.path.join(self.cdir, "mcp_config.json"), "w").close()   # 0 bytes
        with open(os.path.join(self.cdir, "plugins", PLUG, "mcp_config.json"), "w") as f:
            f.write("{not json")
        self.assertEqual(ag.AntigravityAdapter().list_available(self.root), [])

    def test_describe_and_hints(self):
        self.fixture_home()
        a = ag.AntigravityAdapter()
        self.assertEqual(a.describe("antigravity:plugin:" + PLUG, {}), "Chrome tools")
        self.assertEqual(a.describe(S1, {}), "")
        self.assertIn("agy plugin install foo", a.missing_hint("antigravity:plugin:foo", {}))
        self.assertTrue(a.is_global("antigravity:plugin:foo"))
        self.assertFalse(a.is_global(S1))
        self.assertFalse(a.installable("antigravity:plugin:foo"))

    def test_fetch_plugin_list_filters_and_uses_list_command(self):
        self.fixture_home()
        ag.fetch_plugin_list = self._orig_ag[0]
        pdir = os.path.join(self.cdir, "plugins", PLUG)
        out = json.dumps([{"name": PLUG, "enabled": True, "path": pdir},
                          {"name": "ws", "enabled": True, "path": os.path.join(self.app, ".agents", "plugins", "ws")}])

        def run(cmd, cwd, timeout=60):
            self.ag_cmds.append((cmd, cwd))

            class R:
                returncode, stdout, stderr = 0, out, ""
            return R()
        ag.run = run
        self.assertEqual(ag.fetch_plugin_list(self.app),
                         [{"id": "antigravity:plugin:" + PLUG, "enabled": True, "scope": "user"}])
        self.assertEqual(self.ag_cmds[0][0][1:], ["plugin", "list", "--json"])
        self.assertEqual(self.ag_cmds[0][1], self.app)
        ag.run = lambda *a, **k: (_ for _ in ()).throw(OSError("no agy"))
        self.assertIsNone(ag.fetch_plugin_list(self.app))


class Matching(AgEnv):
    def test_percent_encoded_folder_matches(self):
        self.fixture_home()
        odd = os.path.join(self.root, "Say, What?")
        os.makedirs(odd)
        self.project("p1", [odd], deny=[R1])
        a = ag.AntigravityAdapter()
        st = a.load_state()
        self.assertEqual(st["projects"][0]["folders"], [odd])
        self.assertEqual(a.connectors_off(st, odd), [S1])
        self.assertEqual(a.connectors_off(st, self.app), None)

    def test_mcp_star_means_everything_off(self):
        self.fixture_home()
        self.project("p1", [self.app], deny=["mcp(*)"])
        a = ag.AntigravityAdapter()
        self.assertEqual(a.connectors_off(a.load_state(), self.app), sorted([S1, S2]))

    def test_no_project_is_a_note_not_drift(self):
        self.fixture_home()
        self.project("p1", [self.app])
        self.profile([S1])
        r = self.row("other")
        self.assertIn(ag.AntigravityAdapter().uncontrolled_hint(), r["tool_notes"])
        self.assertFalse(any(c["name"].startswith("antigravity:") for c in r["connectors"]))
        self.assertFalse(any("antigravity" in d for d in r["drift"]))

    def test_ancestor_and_outside_projects_never_edited(self):
        self.fixture_home()
        outside = os.path.join(self.t, "elsewhere")
        os.makedirs(outside)
        anc = self.project("anc", [self.root])
        out = self.project("out", [outside])
        mine = self.project("mine", [self.app])
        b = (slurp(anc), slurp(out))
        self.profile([S1])
        rc, o = self.apply_all()
        self.assertEqual(rc, 0, o)
        self.assertEqual((slurp(anc), slurp(out)), b)
        self.assertEqual(self.deny(mine), [R2])

    def test_stale_entries(self):
        self.fixture_home()
        gone = os.path.join(self.root, "gone")
        self.project("p1", [gone], name="Old")
        self.project("p2", [gone, self.app], name="Mixed")
        self.project("p3", [os.path.join(self.t, "outside-gone")], name="Out")
        a = ag.AntigravityAdapter()
        self.assertEqual(a.stale_entries(a.load_state(), self.root), ["%s (Antigravity project Old)" % gone])


class EndToEnd(AgEnv):
    def test_apply_writes_only_the_deny(self):
        self.fixture_home()
        p = self.project("p1", [self.app])
        before = jload(p)
        orig_keys = list(before)
        self.profile([S1])
        rc, o = self.apply_all()
        self.assertEqual(rc, 0, o)
        after = jload(p)
        self.assertEqual(self.deny(p), [R2])
        self.assertEqual(list(after), orig_keys)
        after["permissionGrants"]["permissionGrants"]["deny"] = []
        self.assertEqual(after, before)
        self.assertTrue(slurp(p).endswith("\n"))
        self.assertIn("  \"id\"", slurp(p))  # indent kept
        bk = os.path.join(self.data, "backups", "antigravity")
        files = os.listdir(bk)
        self.assertEqual(len(files), 1)
        self.assertTrue(files[0].startswith("p1.json."))
        self.assertEqual(os.stat(os.path.join(bk, files[0])).st_mode & 0o777, 0o600)
        self.assertEqual(jload(os.path.join(bk, files[0])), before)
        self.assertIn("app\tconnector:%s\ton -> off" % S2, self.log())
        # idempotent
        rc, o2 = self.apply_all()
        self.assertEqual(rc, 0)
        self.assertEqual(len(os.listdir(bk)), 1)
        self.assertTrue(any("app: ok" in l for l in o2), o2)
        out = []
        engine.do_apply(self.cfg(), "app", out=out.append)
        self.assertEqual(out, ["app: nothing to change"])
        r = self.row("app")
        self.assertFalse(any("antigravity" in d for d in r["drift"]), r["drift"])
        c = {c["name"]: c for c in r["connectors"]}
        self.assertTrue(c[S2]["actual_off"] and not c[S1]["actual_off"])

    def test_scan_shows_drift_before_apply(self):
        self.fixture_home()
        self.project("p1", [self.app])
        self.profile([S1])
        r = self.row("app")
        self.assertIn("connector %s is on, profiles want it off" % S2, r["drift"])

    def test_creates_nested_deny_when_grants_absent(self):
        self.fixture_home()
        p = self.project("p1", [self.app], grants=False)
        self.profile([S1])
        self.apply_all()
        self.assertEqual(self.deny(p), [R2])
        self.assertEqual(jload(p)["settings"], {"theme": "dark", "n": 3})
        self.assertIs(jload(p)["permissionGrants"]["v2Migrated"], True)

    def test_shared_project_deny_wins_and_other_is_mixed(self):
        self.fixture_home()
        p = self.project("shared", [self.app, self.other])
        self.profile([S1, S2], to=("app",))
        pr = jload(os.path.join(self.data, "projects.json"))
        pr["projects"]["other"]["profiles"] = ["ag2"]
        prof = jload(os.path.join(self.data, "profiles.json"))
        prof["profiles"]["ag2"] = {"plugins": [], "connectors": [S2]}
        jdump(prof, os.path.join(self.data, "profiles.json"))
        jdump(pr, os.path.join(self.data, "projects.json"))
        rc, o = self.apply_all()
        self.assertEqual(rc, 0, o)
        self.assertEqual(self.deny(p), [R1])   # other wants S1 off; app grants it, but deny wins
        own = jload(os.path.join(self.data, "antigravity-owned.json"))
        self.assertEqual(own, {"shared": {self.other: [R1]}})
        r = self.row("app")
        c = {c["name"]: c for c in r["connectors"]}
        self.assertTrue(c[S1]["mixed"])
        self.assertFalse(any("antigravity" in d for d in r["drift"]), r["drift"])
        r2 = self.row("other")
        self.assertFalse(any("antigravity" in d for d in r2["drift"]))
        # a second apply changes nothing
        self.apply_all()
        self.assertEqual(self.deny(p), [R1])

    def test_user_rule_kept_and_removing_grant_removes_only_ours(self):
        self.fixture_home()
        p = self.project("p1", [self.app], deny=[R1, "command(rm)"])   # user's own rule for S1
        self.profile([S1])   # S1 wanted... but user denied it: stays off, never claimed
        self.apply_all()
        self.assertEqual(self.deny(p), [R1, "command(rm)", R2])
        own = jload(os.path.join(self.data, "antigravity-owned.json"))
        self.assertEqual(own, {"p1": {self.app: [R2]}})
        # grant S2 as well: ntk's rule goes, the user's stays
        cfg = self.cfg()
        engine.profile_edit(cfg, "ag", "add", "connector", S2, out=lambda m: None)
        self.assertEqual(self.deny(p), [R1, "command(rm)"])
        # now neither is granted back: S1 (user's) keeps, nothing is claimed
        engine.profile_edit(cfg, "ag", "remove", "connector", S1, out=lambda m: None)
        self.assertEqual(self.deny(p), [R1, "command(rm)"])
        self.assertEqual(jload(os.path.join(self.data, "antigravity-owned.json")), {})
        # granting S1 cannot remove the user's rule: said so, not logged
        out = []
        engine.profile_edit(cfg, "ag", "add", "connector", S1, out=out.append)
        self.assertEqual(self.deny(p), [R1, "command(rm)"])
        self.assertTrue(any("not one ntk added" in l for l in out), out)

    def test_malformed_shape_is_skipped_untouched(self):
        self.fixture_home()
        p = self.project("bad", [self.app], raw={"permissionGrants": "oops"})
        b = slurp(p)
        self.profile([S1])
        rc, o = self.apply_all()
        self.assertEqual(rc, 1)
        self.assertEqual(slurp(p), b)
        out = []
        self.assertEqual(engine.do_apply(self.cfg(), "app", out=out.append), 1)
        self.assertIn("skipped %s: unexpected shape" % p, out)
        self.assertEqual(slurp(p), b)
        self.assertFalse(os.path.exists(os.path.join(self.data, "backups", "antigravity")))
        for bad in ({"permissionGrants": {"permissionGrants": []}},
                    {"permissionGrants": {"permissionGrants": {"deny": "x"}}}):
            self.project("bad", [self.app], raw=bad)
            b = slurp(p)
            out = []
            self.assertEqual(engine.do_apply(self.cfg(), "app", out=out.append), 1)
            self.assertEqual(slurp(p), b)

    def test_only_one_connector(self):
        self.fixture_home()
        p = self.project("p1", [self.app])
        self.profile([])
        out = []
        engine.do_apply(self.cfg(), "app", only=("connector", S1), out=out.append)
        self.assertEqual(self.deny(p), [R1])
        engine.do_apply(self.cfg(), "app", only=("connector", S2), out=out.append)
        self.assertEqual(self.deny(p), [R1, R2])


class Secrets(AgEnv):
    def test_no_token_reaches_ntk_files_or_output(self):
        self.fixture_home()
        self.project("p1", [self.app])
        self.profile([S1])
        cfg = self.cfg()
        seen = []
        seen.append(json.dumps(engine.scan(cfg, fresh=True)))
        rc, o = self.apply_all()
        seen += o
        engine.snapshot(cfg)
        with history.recording(cfg, "grant"):
            engine.profile_edit(cfg, "ag", "add", "connector", S2, out=seen.append)
        out = []
        engine.undo_change(cfg, out=out.append)
        seen += out
        seen.append(json.dumps(engine.scan(cfg, fresh=True)))
        seen.append(self.log())
        blob = "\n".join(seen)
        files = 0
        for d, _, fs in os.walk(self.data):
            for f in fs:
                files += 1
                blob += slurp_any(os.path.join(d, f))
        self.assertGreater(files, 5)
        for t in (TOK1, TOK2, TOK3):
            self.assertNotIn(t, blob)
        self.assertTrue(os.path.exists(os.path.join(self.data, "antigravity-owned.json")))


class Globals(AgEnv):
    def test_global_plugin_goes_through_agy_list_command(self):
        self.fixture_home()
        self.ag_plugins = [{"id": "antigravity:plugin:foo", "enabled": True, "scope": "user"}]
        out = []
        rc = engine.reconcile_globals(self.cfg(), out.append)
        self.assertEqual(rc, 0, out)
        self.assertEqual(len(self.ag_cmds), 1)
        cmd = self.ag_cmds[0][0]
        self.assertIsInstance(cmd, list)
        self.assertTrue(cmd[0].endswith("agy"))
        self.assertEqual(cmd[1:], ["plugin", "disable", "foo"])
        self.assertIn("plugin:antigravity:plugin:foo\ton -> off", self.log())

    def test_set_global_failure_prints_failed(self):
        def run(cmd, cwd, timeout=60):
            class R:
                returncode, stdout, stderr = 1, "", "boom"
            return R()
        ag.run = run
        out = []
        self.assertEqual(ag.AntigravityAdapter().set_global("antigravity:plugin:foo", True, out.append), 1)
        self.assertTrue(out[0].startswith("FAILED"))


class Undo(AgEnv):
    def test_undo_reverts_both_tools(self):
        self.fixture_home()
        p = self.project("p1", [self.app])
        self.profile([])
        cfg = self.cfg()
        rc, o = self.apply_all()
        self.assertEqual(rc, 0, o)
        self.assertEqual(self.deny(p), [R1, R2])
        self.assertIn("claude.ai Gmail", jload(self.cj)["projects"][self.app]["disabledMcpServers"])
        with history.recording(cfg, "grant two"):
            engine.profile_edit(cfg, "ag", "add", "connector", S1, out=lambda m: None)
            engine.profile_edit(cfg, "ag", "add", "connector", "Gmail", out=lambda m: None)
        self.assertEqual(self.deny(p), [R2])
        self.assertNotIn("claude.ai Gmail", jload(self.cj)["projects"][self.app]["disabledMcpServers"])
        out = []
        rc = engine.undo_change(cfg, out=out.append)
        self.assertEqual(rc, 0, out)
        self.assertEqual(sorted(self.deny(p)), sorted([R1, R2]))
        self.assertIn("claude.ai Gmail", jload(self.cj)["projects"][self.app]["disabledMcpServers"])
        self.assertEqual(jload(p)["permissionGrants"]["permissionGrants"]["allow"], ["command(git)"])


if __name__ == "__main__":
    unittest.main()
