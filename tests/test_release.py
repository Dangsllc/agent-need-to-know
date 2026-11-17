"""Release hygiene: the SessionStart hook (file-only, fast, quiet), a clean-install
walk-through with a stand-in `claude` executable, and the leak checker."""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_devconsole import Env, jdump  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENTRY = os.path.join(REPO, "scripts", "devconsole.py")
sys.path.insert(0, os.path.join(REPO, "scripts"))
import check_leaks  # noqa: E402


def fake_claude(bindir, log):
    """A `claude` stand-in: logs every call; no plugins, no connectors."""
    os.makedirs(bindir)
    p = os.path.join(bindir, "claude")
    with open(p, "w") as f:
        f.write('#!/bin/sh\necho "$@" >> "%s"\n'
                'case "$1 $2" in "plugin list") echo "[]";; esac\nexit 0\n' % log)
    os.chmod(p, os.stat(p).st_mode | stat.S_IXUSR)


class Sandbox(object):
    """Environment that cannot reach real user data."""
    def __init__(self, t):
        self.t = t
        self.home = os.path.join(t, "home")
        os.makedirs(os.path.join(self.home, ".claude", "devconsole"))
        self.log = os.path.join(t, "claude-calls.log")
        fake_claude(os.path.join(t, "bin"), self.log)
        self.config = os.path.join(self.home, ".claude", "devconsole", "config.json")
        self.env = dict(os.environ, HOME=self.home, DEVCONSOLE_CONFIG=self.config,
                        DEVCONSOLE_CLAUDE_JSON=os.path.join(self.home, ".claude.json"),
                        DEVCONSOLE_CLAUDE_HOME=os.path.join(self.home, ".claude"),
                        DEVCONSOLE_USER_SETTINGS=os.path.join(self.home, ".claude", "settings.json"),
                        PATH=os.path.join(t, "bin") + os.pathsep + os.environ.get("PATH", ""))

    def run(self, *args, **kw):
        return subprocess.run([sys.executable, ENTRY] + list(args), env=self.env, capture_output=True,
                              text=True, timeout=60, **kw)

    def calls(self):
        try:
            with open(self.log) as f:
                return f.read().splitlines()
        except OSError:
            return []


class TestHook(Env):
    def sandbox_env(self):
        e = dict(os.environ)
        e["PATH"] = os.path.join(self.t, "bin") + os.pathsep + e.get("PATH", "")
        return e

    def hook(self, cwd, *extra):
        t0 = time.time()
        r = subprocess.run([sys.executable, ENTRY, "check", "--hook"] + list(extra), cwd=cwd,
                           env=self.sandbox_env(), capture_output=True, text=True, timeout=30)
        return r, time.time() - t0

    def setUp(self):
        super().setUp()
        self.log = os.path.join(self.t, "claude-calls.log")
        fake_claude(os.path.join(self.t, "bin"), self.log)

    def test_hook_is_fast_file_only_and_valid_json(self):
        app = os.path.join(self.root, "app")
        r, secs = self.hook(app)  # no path argument: uses the current directory
        self.assertEqual(r.returncode, 0)
        self.assertLess(secs, 0.5)
        out = json.loads(r.stdout)
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "SessionStart")
        self.assertIn("differs from its profiles", out["hookSpecificOutput"]["additionalContext"])
        self.assertFalse(os.path.exists(self.log), "the hook must not run `claude`")

    def test_hook_on_a_tree_of_projects_is_still_fast(self):
        for i in range(60):
            os.makedirs(os.path.join(self.root, "p%02d" % i, "sub"))
        r, secs = self.hook(os.path.join(self.root, "p07", "sub"))
        self.assertEqual(r.returncode, 0)
        self.assertLess(secs, 0.5)
        self.assertFalse(os.path.exists(self.log))

    def test_hook_prints_nothing_outside_the_root(self):
        outside = os.path.join(self.t, "elsewhere")
        os.makedirs(outside)
        r, _ = self.hook(outside)
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))

    def test_hook_never_fails_loudly(self):
        for name in ("profiles.json", "projects.json", ".devconsole-cache.json", "capabilities.json"):
            with open(os.path.join(self.data, name), "w") as f:
                f.write("{ not json")
        r, _ = self.hook(os.path.join(self.root, "app"))
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stderr, "")
        if r.stdout:
            json.loads(r.stdout)
        with open(self.cfgfile, "w") as f:
            f.write("garbage")
        r, _ = self.hook(os.path.join(self.root, "app"))
        self.assertEqual((r.returncode, r.stderr), (0, ""))

    def test_hooks_json_uses_only_the_plugin_root_variable(self):
        with open(os.path.join(REPO, "hooks", "hooks.json")) as f:
            h = json.load(f)
        self.assertEqual(list(h["hooks"]), ["SessionStart"])
        hook = h["hooks"]["SessionStart"][0]["hooks"][0]
        self.assertLessEqual(hook["timeout"], 10)
        import re
        self.assertEqual(set(re.findall(r"\$\{?(\w+)", hook["command"])), {"CLAUDE_PLUGIN_ROOT"})
        self.assertIn("check --hook", hook["command"])


class TestCleanInstall(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "uses a sh stand-in for claude")
    def test_new_user_walkthrough(self):
        with tempfile.TemporaryDirectory() as t:
            t = os.path.realpath(t)
            sb = Sandbox(t)
            root = os.path.join(t, "code")
            data = os.path.join(t, "code", "_devconsole")
            for n in ("my-api", "my-web-app"):  # names used by the shipped example projects.json
                os.makedirs(os.path.join(root, n))
            # 1. no config: the hook says how to start, exactly once
            r = sb.run("check", "--hook", cwd=os.path.join(root, "my-api"))
            self.assertEqual(r.returncode, 0)
            self.assertIn("no config found", json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"])
            r = sb.run("check", "--hook", cwd=os.path.join(root, "my-api"))
            self.assertEqual((r.returncode, r.stdout), (0, ""))
            # other commands tell the user what to create
            r = sb.run("scan")
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("no config", r.stderr)
            # 2. what `/project-setup init` does: write the config, copy the templates
            jdump({"root": root, "data_dir": data}, sb.config)
            os.makedirs(data)
            for src, dst in (("profiles.example.json", "profiles.json"), ("projects.example.json", "projects.json")):
                shutil.copy(os.path.join(REPO, "templates", src), os.path.join(data, dst))
            # 3. scan works
            r = sb.run("scan")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("my-api", r.stdout)
            self.assertIn("missing=2", r.stdout)  # the example profiles want plugins nothing has installed
            self.assertIn("new=1", r.stdout)      # the root folder itself has no entry
            self.assertEqual(r.stderr, "")
            with open(os.path.join(data, "capabilities.json")) as f:
                self.assertEqual(json.load(f)["schema_version"], 2)
            # 4. a dry run plans installs but runs none
            r = sb.run("apply", "--all", "--dry-run")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("install", r.stdout)
            self.assertFalse([c for c in sb.calls() if c.startswith("plugin install")])
            # 5. the hook now reports the folder state, still without running claude
            n = len(sb.calls())
            r = sb.run("check", "--hook", cwd=os.path.join(root, "my-api"))
            self.assertEqual(r.returncode, 0)
            self.assertEqual(len(sb.calls()), n)

    def test_templates_are_valid_v2(self):
        for name in ("profiles.example.json", "projects.example.json"):
            with open(os.path.join(REPO, "templates", name)) as f:
                self.assertEqual(json.load(f)["schema_version"], 2)


class TestLeakCheck(unittest.TestCase):
    def kinds(self, text):
        return [k for _, k, _ in check_leaks.scan_text(text)]

    def test_flags(self):
        home = "/" + "Users/" + "bob/work"
        self.assertEqual(self.kinds("see " + home), ["absolute home path"])
        self.assertEqual(self.kinds("at /" + "home/" + "al/x"), ["absolute home path"])
        self.assertEqual(self.kinds("mail bob" + "@corp.io now"), ["email address"])
        self.assertEqual(self.kinds("key " + "sk-" + "a" * 24), ["openai/anthropic-style key"])
        self.assertEqual(self.kinds("t " + "ghp" + "_" + "A" * 30), ["github token"])
        self.assertEqual(self.kinds("id " + "AKIA" + "A" * 16), ["aws access key id"])

    def test_allows(self):
        for ok in ("Co-Authored-By: Claude <noreply@anthropic.com>", "me@example.com", "x@mail.example.com",
                   "pyright-lsp@claude-plugins-official", "eng@synced", "task-list risk-based", "~/code",
                   "/Users" + "/ alone, and /home" + "/ alone"):
            self.assertEqual(self.kinds(ok), [], ok)

    def test_repository_is_clean(self):
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            rc = check_leaks.main([])
        self.assertEqual(rc, 0, buf.getvalue())


if __name__ == "__main__":
    unittest.main()
