"""Data-format contract: schema_version handling and the capability id rule."""
import contextlib
import io
import json
import os
import sys
import threading
import http.client
import http.server
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_devconsole import Env, AVAIL, jdump, jload  # noqa: E402

from devconsole_pkg import cli, config, engine, lifecycle, model, server  # noqa: E402
from devconsole_pkg.adapters import claude_code as cc  # noqa: E402


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = cli.main(list(argv))
    return rc, out.getvalue(), err.getvalue()


class TestSchemaVersion(Env):
    def setUp(self):
        super().setUp()
        model.WARNINGS[:] = []

    def p(self, name):
        return os.path.join(self.data, name)

    def snap(self, name):
        try:
            with open(self.p(name)) as f:
                return f.read()
        except OSError:
            return None

    def test_missing_version_reads_as_current_and_writes_v2(self):
        cfg = config.load_config()
        self.assertEqual(model.file_version(jload(self.p("profiles.json"))), 2)
        profs, meta = model.load_profiles(cfg)
        model.save_profiles(cfg, profs, meta)
        d = jload(self.p("profiles.json"))
        self.assertEqual(d["schema_version"], 2)
        self.assertEqual(list(d)[:2], ["_about", "schema_version"])  # _about kept, version right after
        self.assertEqual(d["default_profiles"], ["base"])
        model.save_projects(cfg, model.load_projects(cfg))
        d = jload(self.p("projects.json"))
        self.assertEqual((d["_about"], d["schema_version"]), ("c", 2))

    def test_explicit_v2_and_v1_shapes_read(self):
        jdump({"schema_version": 2, "profiles": {"a": {"plugins": ["p@x"], "connectors": []}}}, self.p("profiles.json"))
        profs, _ = model.load_profiles(config.load_config())
        self.assertEqual(profs["a"]["plugins"], ["p@x"])
        self.assertEqual(model.WARNINGS, [])

    def test_capabilities_written_with_version(self):
        engine.scan(config.load_config())
        self.assertEqual(jload(self.p("capabilities.json"))["schema_version"], 2)

    def test_newer_version_refuses_mutating_commands(self):
        for fname, other in (("profiles.json", None), ("projects.json", None), ("capabilities.json", None)):
            with self.subTest(fname):
                if fname == "capabilities.json":
                    jdump({"schema_version": 3, "capabilities": {}}, self.p(fname))
                else:
                    d = jload(self.p(fname))
                    d["schema_version"] = 3
                    jdump(d, self.p(fname))
                before = {f: self.snap(f) for f in ("profiles.json", "projects.json", "capabilities.json")}
                rc, out, err = run_cli("attach", "other", "py", "--no-apply")
                self.assertEqual(rc, 3)
                self.assertIn("newer devconsole", err)
                self.assertIn("upgrade", err)
                rc, _, err = run_cli("profile-new", "zzz")
                self.assertEqual(rc, 3)
                after = {f: self.snap(f) for f in before}
                self.assertEqual(before, after)  # nothing written
                jdump(PROFILES_OK, self.p("profiles.json"))
                jdump(PROJECTS_OK, self.p("projects.json"))
                if os.path.exists(self.p("capabilities.json")):
                    os.unlink(self.p("capabilities.json"))
                model.WARNINGS[:] = []

    def test_newer_version_warns_on_read_only_commands(self):
        d = jload(self.p("projects.json"))
        d["schema_version"] = 7
        jdump(d, self.p("projects.json"))
        raw = self.snap("projects.json")
        rc, out, err = run_cli("scan", "--json")
        self.assertEqual(rc, 0)
        self.assertIn("warning", err)
        self.assertIn("newer devconsole", err)
        self.assertEqual(json.loads(out)["warnings"][0].split(" has ")[1][:16], "schema_version 7")
        self.assertEqual(self.snap("projects.json"), raw)  # scan never rewrites it
        rc, _, err = run_cli("capability", "list")
        self.assertEqual(rc, 0)

    def test_scan_does_not_overwrite_newer_capabilities(self):
        jdump({"schema_version": 5, "capabilities": {"x@y": {"kind": "plugin", "status": "active",
                                                              "last_seen": "2020-01-01T00:00:00+00:00"}}},
              self.p("capabilities.json"))
        raw = self.snap("capabilities.json")
        engine.scan(config.load_config())
        self.assertEqual(self.snap("capabilities.json"), raw)

    def test_hook_stays_silent_with_newer_files(self):
        d = jload(self.p("projects.json"))
        d["schema_version"] = 9
        jdump(d, self.p("projects.json"))
        self.assertNotIn("Traceback", self.check(os.path.join(self.root, "app")))


class TestServerSchema(Env):
    def test_post_refused_with_409_when_newer(self):
        d = jload(os.path.join(self.data, "profiles.json"))
        d["schema_version"] = 3
        jdump(d, os.path.join(self.data, "profiles.json"))
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(config.load_config(), "tok"))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=20)
            c.request("POST", "/api/profile-new", json.dumps({"name": "zz"}),
                      {"Content-Type": "application/json", "X-Devconsole-Token": "tok"})
            r = c.getresponse()
            body = json.loads(r.read())
            self.assertEqual(r.status, 409)
            self.assertIn("newer devconsole", body["error"])
        finally:
            srv.shutdown()
            srv.server_close()


PROFILES_OK = {"_about": "comment", "default_profiles": ["base"], "plugin_accounts": {"a@x": "any"},
               "profiles": {"base": {"group": "default", "description": "d", "plugins": [], "connectors": ["Docs"]},
                            "py": {"group": "code", "description": "python", "plugins": ["a@x"], "connectors": []}}}
PROJECTS_OK = {"_about": "c", "projects": {"app": {"profiles": ["py"]}, "other": {"profiles": []}}}


class TestIdRule(Env):
    def setUp(self):
        super().setUp()
        model.WARNINGS[:] = []

    def test_foreign_tool(self):
        for cid, tool in (("codex:mcp:github", "codex"), ("gemini:ext:foo", "gemini"), ("a1-b:x", "a1-b")):
            self.assertEqual(model.foreign_tool(cid), tool, cid)
        for cid in ("pyright-lsp@claude-plugins-official", "Google Drive", "Docs", "eng@synced", "Codex:x",
                    "has space:x", "", "x@y"):
            self.assertIsNone(model.foreign_tool(cid), cid)

    def _with_foreign(self):
        d = jload(os.path.join(self.data, "profiles.json"))
        d["profiles"]["py"]["connectors"] = ["codex:mcp:github"]
        d["profiles"]["py"]["plugins"] = ["a@x", "gemini:ext:foo"]
        d["profiles"]["ts"]["deny_connectors"] = ["codex:mcp:slack"]
        jdump(d, os.path.join(self.data, "profiles.json"))

    def test_resolve_ignores_foreign_ids(self):
        self._with_foreign()
        profs, meta = model.load_profiles(config.load_config())
        r = model.resolve(profs, meta["default_profiles"], {"profiles": ["py", "ts"]}, AVAIL)
        self.assertNotIn("gemini:ext:foo", r["plugins"])
        self.assertNotIn("codex:mcp:github", r["connectors_on"] + r["connectors_off"])
        self.assertNotIn("codex:mcp:slack", r["connectors_off"])
        self.assertEqual(sorted(f["id"] for f in r["foreign"]),
                         ["codex:mcp:github", "codex:mcp:slack", "gemini:ext:foo"])
        self.assertEqual([f for f in r["foreign"] if f["id"] == "gemini:ext:foo"][0]["kind"], "plugin")

    def test_scan_does_not_count_foreign_as_missing_and_never_installs(self):
        self._with_foreign()
        self.lists[os.path.join(self.root, "app")] = [{"id": "a@x", "enabled": True}, {"id": "b@x", "enabled": True}]
        st = engine.scan(config.load_config(), fresh=True)
        app = [p for p in st["projects"] if p["path"] == "app"][0]
        self.assertEqual(app["missing"], [])
        self.assertEqual(st["missing"], [])
        self.assertEqual(sorted(f["id"] for f in app["foreign"]), ["codex:mcp:github", "codex:mcp:slack", "gemini:ext:foo"])
        self.assertNotIn("gemini:ext:foo", [p["id"] for p in app["plugins"]])
        self.assertNotIn("codex:mcp:github", [c["name"] for c in app["connectors"]])
        self.assertNotIn("gemini:ext:foo", st["known_plugins"])
        rc = engine.do_apply(config.load_config(), "app", out=lambda m: None)
        self.assertEqual(rc, 0)
        flat = " ".join(" ".join(c) for c, _ in self.cmds)
        self.assertNotIn("gemini", flat)
        self.assertNotIn("codex", flat)
        self.assertEqual(cc.ClaudeCodeAdapter().plan(app), engine.A().plan(app))
        self.assertFalse(cc.ClaudeCodeAdapter().owns("codex:mcp:github"))
        self.assertTrue(cc.ClaudeCodeAdapter().owns("a@x"))

    def test_registry_skips_foreign_ids(self):
        self._with_foreign()
        engine.scan(config.load_config(), fresh=True)
        ids = jload(os.path.join(self.data, "capabilities.json"))["capabilities"]
        self.assertFalse([i for i in ids if model.foreign_tool(i)])
        # later scans: still not in the inbox
        engine.scan(config.load_config(), fresh=True)
        self.assertFalse([i for i in lifecycle.load_caps(config.load_config())["capabilities"] if model.foreign_tool(i)])

    def test_foreign_ids_can_be_stored_but_not_inlined(self):
        cfg = config.load_config()
        rc, affected = engine.profile_edit(cfg, "py", "add", "connector", "codex:mcp:github", apply=False,
                                           out=lambda m: None)
        self.assertEqual(rc, 0)
        self.assertIn("codex:mcp:github", self.profiles()["py"]["connectors"])
        msgs = []
        rc = engine.do_inline(cfg, "app", "plugin", "gemini:ext:foo", "on", out=msgs.append)
        self.assertEqual(rc, 2)
        self.assertIn("not supported yet", msgs[0])
        self.assertNotIn("inline", self.projects()["app"])

    def test_hook_check_ignores_foreign(self):
        self._with_foreign()
        out = self.check(os.path.join(self.root, "app"))
        self.assertNotIn("codex", out)
        self.assertNotIn("gemini", out)


if __name__ == "__main__":
    unittest.main()
