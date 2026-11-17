"""Command line: argparse and subcommand dispatch."""
import argparse
import json
import os
import sys

from . import model
from .config import config_path, load_config
from .engine import (apply_all, check_path, do_apply, do_attach, do_inline, profile_default,
                     profile_delete, profile_edit, profile_new, render_table, scan, snapshot)
from .lifecycle import (capability_adopt, capability_ignore, capability_retire, list_caps,
                        show_inbox)
from .model import SchemaTooNew
from .server import serve


# ----------------------------------------------------------------------- cli
def need_config():
    cfg = load_config()
    if cfg is None:
        sys.exit("ntk: no config at %s. Create it with {\"root\": \"~/code\", \"data_dir\": \"~/code/_ntk\"}." % config_path())
    return cfg


def build_parser():
    ap = argparse.ArgumentParser(prog="ntk")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan")
    s.add_argument("--json", action="store_true")
    s.add_argument("--fresh", action="store_true")
    s.add_argument("-v", "--verbose", action="store_true", help="list drift items, inline exceptions, missing hints")
    c = sub.add_parser("check")
    c.add_argument("path", nargs="?", default=None, help="folder to check (default: the current directory)")
    c.add_argument("--hook", action="store_true")
    a = sub.add_parser("apply", help="reconcile one project, or --all")
    a.add_argument("relpath", nargs="?")
    a.add_argument("--all", action="store_true")
    a.add_argument("--dry-run", action="store_true")
    for nm in ("attach", "detach"):
        t = sub.add_parser(nm)
        t.add_argument("relpath")
        t.add_argument("profile")
        t.add_argument("--no-apply", action="store_true")
    p = sub.add_parser("profile", help="profile <name> add|remove plugin|connector <id>")
    p.add_argument("name")
    p.add_argument("op", choices=["add", "remove", "deny", "undeny"])
    p.add_argument("kind", choices=["plugin", "connector"])
    p.add_argument("ident")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-apply", action="store_true")
    pdf = sub.add_parser("profile-default", help="profile-default add|remove <name>")
    pdf.add_argument("op", choices=["add", "remove"])
    pdf.add_argument("name")
    pdf.add_argument("--dry-run", action="store_true")
    pdf.add_argument("--no-apply", action="store_true")
    sub.add_parser("inbox", help="list new and gone capabilities")
    cp = sub.add_parser("capability", help="adopt|ignore|retire|list")
    csub = cp.add_subparsers(dest="capcmd", required=True)
    ca = csub.add_parser("adopt")
    ca.add_argument("ident")
    ca.add_argument("--profiles", required=True, help="comma-separated profile names")
    ca.add_argument("--dry-run", action="store_true")
    ci = csub.add_parser("ignore")
    ci.add_argument("ident")
    cr = csub.add_parser("retire")
    cr.add_argument("ident")
    cr.add_argument("--dry-run", action="store_true")
    cl = csub.add_parser("list")
    cl.add_argument("--status", choices=["new", "active", "ignored", "retired", "gone"])
    pn = sub.add_parser("profile-new")
    pn.add_argument("name")
    pn.add_argument("--description", default="")
    pn.add_argument("--group", default="")
    pn.add_argument("--from-project")
    pd = sub.add_parser("profile-delete")
    pd.add_argument("name")
    i = sub.add_parser("inline", help="inline <path> plugin|connector <id> on|off|clear")
    i.add_argument("relpath")
    i.add_argument("kind", choices=["plugin", "connector"])
    i.add_argument("ident")
    i.add_argument("value", choices=["on", "off", "clear"])
    i.add_argument("--no-apply", action="store_true")
    sub.add_parser("snapshot")
    v = sub.add_parser("serve")
    v.add_argument("--port", type=int, default=8765)
    v.add_argument("--no-open", action="store_true")
    return ap


# commands that only read: a data file from a newer ntk is a warning, not an error
READ_ONLY = ("scan", "inbox", "snapshot", "serve")


def main(argv=None):
    ap = build_parser()
    args = ap.parse_args(argv)

    if args.cmd == "check":
        try:
            check_path(args.path or os.getcwd(), args.hook)
        except Exception:
            pass
        if not args.hook:
            _warn()
        return 0
    try:
        rc = _dispatch(ap, args)
    except SchemaTooNew as e:
        sys.stderr.write("ntk: refusing to write: %s\n" % e)
        return 3
    if args.cmd in READ_ONLY or args.cmd == "capability":
        _warn()
    return rc


def _warn():
    for w in model.WARNINGS:
        sys.stderr.write("ntk: warning: %s\n" % w)


def _dispatch(ap, args):
    cfg = need_config()
    if args.cmd not in READ_ONLY and not (args.cmd == "capability" and args.capcmd == "list"):
        model.assert_writable(cfg)
    if args.cmd == "scan":
        st = scan(cfg, fresh=args.fresh)
        if args.json:
            print(json.dumps(st, indent=2))
            return 0
        print("Org: %s (%s)  root: %s" % (st["org"]["name"] or "unknown", st["org"]["id8"] or "-", st["root"]))
        print(render_table(st))
        print("\n" + "  ".join("%s=%d" % kv for kv in st["counts"].items() if kv[1]))
        if args.verbose:
            for p in st["projects"]:
                for d in p["drift"]:
                    print("  %s: drift: %s" % (p["path"], d))
                for it in p["inline_items"]:
                    print("  %s: inline %s %s %s" % (p["path"], it["kind"], it["id"], it["value"]))
                for i, h in p["missing_hints"].items():
                    print("  %s: missing %s: %s" % (p["path"], i, h))
                if p.get("error"):
                    print("  %s: error: %s" % (p["path"], p["error"]))
        for c in st["capabilities"]:
            if c["status"] == "retired" and c["seen_now"]:
                print("  retired but seen again: %s %s" % (c["kind"], c["id"]))
        for m in st["missing"]:
            print("missing: %s in %s: %s" % (m["id"], ", ".join(m["projects"]), m["hint"]))
        for n in st.get("notes", []):
            print("note: " + n)
        for k in st["stale"]:
            print("stale ~/.claude.json entry (folder gone): %s" % k)
        return 0
    if args.cmd == "apply":
        if args.all:
            return apply_all(cfg, dry=args.dry_run)
        if not args.relpath:
            ap.error("apply needs a path or --all")
        return do_apply(cfg, args.relpath, dry=args.dry_run)
    if args.cmd in ("attach", "detach"):
        return do_attach(cfg, args.relpath, args.profile, apply=not args.no_apply, attach=args.cmd == "attach")
    if args.cmd == "profile":
        return profile_edit(cfg, args.name, args.op, args.kind, args.ident, dry=args.dry_run, apply=not args.no_apply)[0]
    if args.cmd == "profile-default":
        return profile_default(cfg, args.op, args.name, dry=args.dry_run, apply=not args.no_apply)[0]
    if args.cmd == "inbox":
        return show_inbox(cfg)
    if args.cmd == "capability":
        if args.capcmd == "adopt":
            return capability_adopt(cfg, args.ident, [x for x in args.profiles.split(",") if x], dry=args.dry_run)[0]
        if args.capcmd == "ignore":
            return capability_ignore(cfg, args.ident)
        if args.capcmd == "retire":
            return capability_retire(cfg, args.ident, dry=args.dry_run)[0]
        return list_caps(cfg, args.status)
    if args.cmd == "profile-new":
        return profile_new(cfg, args.name, args.description, args.group, args.from_project)
    if args.cmd == "profile-delete":
        return profile_delete(cfg, args.name)
    if args.cmd == "inline":
        return do_inline(cfg, args.relpath, args.kind, args.ident, args.value, apply=not args.no_apply)
    if args.cmd == "snapshot":
        snapshot(cfg)
        print("saved %s and %s" % (os.path.join(cfg.data_dir, "CONSOLE.md"), os.path.join(cfg.data_dir, "CONSOLE.html")))
        return 0
    if args.cmd == "serve":
        serve(cfg, args.port, not args.no_open)
        return 0


if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except SystemExit:
        raise
    except Exception as e:
        if len(sys.argv) > 1 and sys.argv[1] == "check":
            sys.exit(0)
        raise
