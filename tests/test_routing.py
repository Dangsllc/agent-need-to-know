"""Multi-adapter routing: a second (fake) adapter gets its own ids, and the
Claude Code adapter never sees them."""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
from ntk_pkg import adapters, engine, model  # noqa: E402
from ntk_pkg.adapters import claude_code as cc  # noqa: E402
from ntk_pkg.adapters.base import Adapter  # noqa: E402
from test_core import Env, PROFILES, jdump  # noqa: E402


class FakeAdapter(Adapter):
    name = "fake"
    label = "Fake CLI"
    kinds = ("mcp",)
    scopes = ("project",)
    labels = {"plugins": "servers"}
    enforce = True

    def __init__(self, on=None, avail=True):
        self.on = on or {}          # abs path -> [ids switched on]
        self.avail = avail
        self.planned, self.applied = [], []

    def available(self):
        return self.avail

    def owns(self, cid):
        return model.foreign_tool(cid) == self.name

    def list_plugins(self, path):
        ids = set(self.on.get(path, [])) | {"fake:mcp:github", "fake:mcp:jira"}
        return [{"id": i, "enabled": i in self.on.get(path, []), "scope": "local"} for i in sorted(ids)]

    def missing_hint(self, pid, account):
        return "fake: not installed"

    def plan(self, row, only=None):
        self.planned.append(row)
        acts = []
        for p in row["plugins"]:
            if p["state"] in ("needs-on", "needs-off"):
                acts.append({"kind": "plugin", "item": p["id"], "old": "off" if p["state"] == "needs-on" else "on",
                             "new": "on" if p["state"] == "needs-on" else "off", "cmd": None})
        return acts

    def apply(self, cfg, rel, row, acts, only, ctx, out):
        self.applied.append((rel, [a["item"] for a in acts]))
        return 0


class ConnFake(FakeAdapter):
    """A fake that also owns connectors: off per folder from its state; a folder
    missing from the state is one the tool can't control."""
    def __init__(self, *a, **k):
        FakeAdapter.__init__(self, *a, **k)
        self.off, self.mixed = {}, {}

    def list_available(self, root):
        return ["fake:mcp:a", "fake:mcp:b"]

    def load_state(self):
        return {"off": dict(self.off)}

    def connectors_off(self, state, path):
        return state["off"].get(path)

    def connectors_mixed(self, state, path):
        return self.mixed.get(path, [])

    def uncontrolled_hint(self):
        return "Fake CLI: no project for this folder"

    def stale_entries(self, state, root):
        return ["fake-stale"]


class FleetConnectors(Env):
    def setUp(self):
        super().setUp()
        self.fake = ConnFake()
        adapters._ADAPTERS.append(self.fake)
        prof = dict(PROFILES, profiles=dict(PROFILES["profiles"],
                                            py={"plugins": ["a@x"], "connectors": ["fake:mcp:a"]}))
        jdump(prof, os.path.join(self.data, "profiles.json"))
        self.app = os.path.join(self.root, "app")
        self.lists[self.app] = [{"id": "a@x", "enabled": True, "scope": "local"},
                                {"id": "b@x", "enabled": True, "scope": "local"}]

    def tearDown(self):
        adapters._ADAPTERS.remove(self.fake)
        super().tearDown()

    def row(self):
        st = engine.scan(engine.load_config(), fresh=True)
        return st, next(r for r in st["projects"] if r["path"] == "app")

    def test_state_is_routed_and_joined(self):
        st = adapters.get().load_state()
        self.assertIsInstance(st, dict)          # the primary state, as before
        self.assertIn("fake", st.others)
        self.assertIn("fake:mcp:a", adapters.get().list_available(self.root))
        self.assertIn("Docs", adapters.get().list_available(self.root))
        self.assertIn("fake-stale", adapters.get().stale_entries(st, self.root))

    def test_drift_and_apply_for_second_tool(self):
        self.fake.off = {self.app: []}           # controllable, everything on
        st, r = self.row()
        conns = {c["name"]: c for c in r["connectors"]}
        self.assertEqual(conns["fake:mcp:a"]["tool"], "fake")
        self.assertFalse(conns["fake:mcp:a"]["desired_off"])   # granted by py
        self.assertTrue(conns["fake:mcp:b"]["desired_off"])    # implicit deny
        self.assertIn("connector fake:mcp:b is on, profiles want it off", r["drift"])
        self.assertIn("fake-stale", st["stale"])

    def test_uncontrolled_folder_is_a_note_not_drift(self):
        self.fake.off = {}                       # no project for app
        st, r = self.row()
        self.assertFalse(any(c["name"].startswith("fake:") for c in r["connectors"]))
        self.assertIn("Fake CLI: no project for this folder", r["tool_notes"])
        self.assertFalse(any("fake:" in d for d in r["drift"]))

    def test_mixed_is_not_drift(self):
        self.fake.off = {self.app: ["fake:mcp:a", "fake:mcp:b"]}
        self.fake.mixed = {self.app: ["fake:mcp:a"]}   # another folder in the project denies it
        st, r = self.row()
        conns = {c["name"]: c for c in r["connectors"]}
        self.assertTrue(conns["fake:mcp:a"]["mixed"])
        self.assertFalse(any("fake:mcp:a" in d for d in r["drift"]))

    def test_hook_ignores_other_tools(self):
        self.fake.off = {self.app: []}
        engine.scan(engine.load_config(), fresh=True)  # fills the connector cache
        out = self.check(self.app)
        # Claude's Gmail and Stripe are not granted (2); fake:mcp:b would make it 3
        self.assertIn("2 connector(s) to turn off", out)

    def test_cache_is_stale_when_tools_change(self):
        cfg = engine.load_config()
        self.fake.off = {self.app: []}
        engine.scan(cfg)
        c = engine.load_cache(cfg)
        self.assertEqual(c["projects"][self.app]["tools"], ["claude", "fake"])
        self.assertEqual(c["connectors"]["tools"], ["claude", "fake"])
        self.fake.avail = False
        self.assertFalse(engine._same_tools(c["projects"][self.app], adapters.tool_key()))


class Routing(Env):
    def setUp(self):
        super().setUp()
        self.fake = FakeAdapter()
        adapters._ADAPTERS.append(self.fake)
        self.cc_plans = []
        orig = cc.plan_for
        self._orig_plan = orig
        cc.plan_for = lambda row, only=None: (self.cc_plans.append(row), orig(row, only))[1]
        prof = dict(PROFILES, profiles=dict(PROFILES["profiles"],
                                            py={"plugins": ["a@x", "fake:mcp:github"], "connectors": []}))
        jdump(prof, os.path.join(self.data, "profiles.json"))

    def tearDown(self):
        adapters._ADAPTERS.remove(self.fake)
        cc.plan_for = self._orig_plan
        super().tearDown()

    def test_owner_and_tools(self):
        self.assertIs(adapters.owner("a@x"), adapters.primary())
        self.assertIs(adapters.owner("Docs"), adapters.primary())
        self.assertIs(adapters.owner("fake:mcp:github"), self.fake)
        self.assertIsNone(adapters.owner("codex:mcp:github"))
        self.assertEqual([t["name"] for t in adapters.tools()], ["claude", "fake"])
        self.assertEqual(adapters.tools()[1]["kinds"], ["mcp"])

    def test_unavailable_adapter_is_skipped(self):
        self.fake.avail = False
        self.assertIsNone(adapters.owner("fake:mcp:github"))
        r = engine._resolve(*self._profiles(), {"profiles": ["py"]})
        self.assertIn("fake:mcp:github", [f["id"] for f in r["foreign"]])
        self.assertNotIn("fake:mcp:github", r["plugins"])

    def test_not_enforced_until_adapter_opts_in(self):
        self.fake.enforce = False
        try:
            self.assertIs(adapters.owner("fake:mcp:github"), self.fake)
            self.assertFalse(adapters.managed("fake:mcp:github"))
            r = engine._resolve(*self._profiles(), {"profiles": ["py"]})
            self.assertIn("fake:mcp:github", [f["id"] for f in r["foreign"]])
            self.assertNotIn("fake:mcp:github", r["plugins"])
        finally:
            self.fake.enforce = True

    def _profiles(self):
        profiles, meta = model.load_profiles(engine.load_config())
        return profiles, meta["default_profiles"]

    def test_managed_foreign_id_is_enforced_and_routed(self):
        app = os.path.join(self.root, "app")
        self.lists[app] = [{"id": "a@x", "enabled": True, "scope": "local"},
                           {"id": "b@x", "enabled": True, "scope": "local"}]
        self.fake.on = {app: ["fake:mcp:jira"]}
        out = []
        rc = engine.do_apply(engine.load_config(), "app", out=out.append)
        self.assertEqual(rc, 0, out)
        # the fake adapter got exactly its own acts
        self.assertEqual(self.fake.applied, [("app", ["fake:mcp:github", "fake:mcp:jira"])])
        # Claude's plan never saw a fake id
        for row in self.cc_plans:
            self.assertFalse(any(model.foreign_tool(p["id"]) for p in row["plugins"]))
        for row in self.fake.planned:
            self.assertTrue(all(p["id"].startswith("fake:") for p in row["plugins"]))
            self.assertEqual(row["connectors"], [])

    def test_rows_carry_tool(self):
        app = os.path.join(self.root, "app")
        self.lists[app] = [{"id": "a@x", "enabled": True, "scope": "local"}]
        st = engine.scan(engine.load_config(), fresh=True)
        row = next(r for r in st["projects"] if r["path"] == "app")
        tools = {p["id"]: p["tool"] for p in row["plugins"]}
        self.assertEqual(tools["a@x"], "claude")
        self.assertEqual(tools["fake:mcp:github"], "fake")
        self.assertTrue(all(c["tool"] == "claude" for c in row["connectors"]))
        self.assertEqual([t["name"] for t in st["tools"]], ["claude", "fake"])

    def test_single_adapter_plan_is_unchanged(self):
        adapters._ADAPTERS.remove(self.fake)
        try:
            app = os.path.join(self.root, "app")
            self.lists[app] = [{"id": "a@x", "enabled": False, "scope": "local"}]
            cfg = engine.load_config()
            row, acts, err = engine.plan_project(cfg, "app", {})
            self.assertIsNone(err)
            self.assertEqual(acts, cc.ClaudeCodeAdapter().plan(row))
        finally:
            adapters._ADAPTERS.append(self.fake)

class NeutralWordsTest(Env):
    """Core messages take their words from the primary adapter's labels."""

    def test_words_follow_labels(self):
        ad = adapters.primary()
        saved = ad.labels
        ad.labels = dict(saved, connector="widget", product="Acme Agent", account_short="team")
        try:
            hint = json.loads(self.check(os.path.join(self.root, "newone")))["hookSpecificOutput"]["additionalContext"]
            self.assertIn("every widget is on", hint)
            self.assertNotIn("claude.ai", hint)
            self.assertEqual(engine.L("product"), "Acme Agent")
            self.assertEqual(engine.L("account_short").capitalize(), "Team")
        finally:
            ad.labels = saved
        hint = json.loads(self.check(os.path.join(self.root, "newone")))["hookSpecificOutput"]["additionalContext"]
        self.assertIn("every claude.ai connector is on", hint)


if __name__ == "__main__":
    unittest.main()
