"""Capability lifecycle: the registry in capabilities.json, the inbox, and
adopt / ignore / retire."""
import copy
import datetime
import json
import os

from . import adapters
from .config import atomic_write, log_change, now_iso, read_json, state_read, state_write
from .model import (INLINE_KEYS, SchemaTooNew, abs_of, foreign_tool, load_profiles,
                    load_projects, norm_conn, read_versioned, save_profiles, save_projects,
                    write_versioned)


def A():
    return adapters.get()


def _engine():
    # engine imports this module at load time, so this side imports it lazily
    from . import engine
    return engine


# ------------------------------------------------------- capability lifecycle
CAP_STATUSES = ("new", "active", "ignored", "retired", "gone")


def load_caps(cfg):
    d = read_versioned(cfg.caps_path, None)
    if not isinstance(d, dict) or not isinstance(d.get("capabilities"), dict):
        return None
    return d


def save_caps(cfg, data, strict=True):
    """Write capabilities.json (always schema_version 2). If the file on disk is from
    a newer ntk, strict writes raise SchemaTooNew; non-strict ones (the scan,
    a read-only command) skip the write and leave a warning."""
    try:
        write_versioned(cfg.caps_path, data)
    except SchemaTooNew:
        if strict:
            raise


def _new_cap(kind, status, now, orgs, seen=True):
    return {"kind": kind, "status": status, "first_seen": now, "last_seen": now if seen else "",
            "seen_on_orgs": sorted(orgs), "note": ""}


def update_capabilities(cfg, lists, oi, connectors, profiles, complete=True):
    """Maintain capabilities.json. -> (data, ids seen in this scan)."""
    existing = load_caps(cfg)
    now = now_iso()
    seen = A().discover_capabilities(lists, oi, connectors)
    if existing is None:  # first run: everything known is active, no inbox flood
        caps = {i: _new_cap(s["kind"], "active", now, s["orgs"]) for i, s in seen.items()}
        for p in profiles.values():
            for kind, ids in (("plugin", p["plugins"] + p["deny_plugins"]),
                              ("connector", p["connectors"] + p["deny_connectors"])):
                for i in ids:
                    if i not in caps and (not foreign_tool(i) or adapters.managed(i)):
                        caps[i] = _new_cap(kind, "active", now, [], seen=False)
        data = {"capabilities": caps}
        save_caps(cfg, data, strict=False)
        return data, set(seen)
    caps, changed = existing["capabilities"], False
    referenced = set()
    for p in profiles.values():
        referenced.update(p["plugins"] + p["deny_plugins"] + p["connectors"] + p["deny_connectors"])
    for i, s in seen.items():
        e = caps.get(i)
        if e is None:
            # already used by a profile = already adopted, so it isn't inbox news
            caps[i] = _new_cap(s["kind"], "active" if i in referenced else "new", now, s["orgs"])
            changed = True
            continue
        if e.get("status") == "new" and i in referenced:
            e["status"] = "active"
            changed = True
        if e.get("status") == "gone":
            e["status"] = "active"
            changed = True
        # merge, never replace: each login only sees its own org, and the
        # orgs recorded from the other login are still true
        merged = sorted(set(e.get("seen_on_orgs") or []) | set(s["orgs"]))
        if s["orgs"] and e.get("seen_on_orgs") != merged:
            e["seen_on_orgs"] = merged
            changed = True
        if (e.get("last_seen") or "")[:13] != now[:13]:
            e["last_seen"] = now
            changed = True
    if complete:
        cur = (oi or {}).get("id8") or ""
        for i, e in caps.items():
            if i in seen:
                continue
            orgs = e.get("seen_on_orgs") or []
            # only ever seen on another claude.ai account: absent here because of
            # the login, not because it went away (this also undoes earlier
            # scans that marked such items gone)
            if orgs and cur and cur not in orgs:
                if e.get("status") == "gone":
                    e["status"] = "active"
                    changed = True
                continue
            # never-seen items (a profile names them, nothing installed them yet) are not gone
            if e.get("status") == "active" and e.get("last_seen") and (not foreign_tool(i) or adapters.managed(i)):
                e["status"] = "gone"
                changed = True
    if changed:
        save_caps(cfg, existing, strict=False)
    return existing, set(seen)


def set_cap_status(cfg, cid, kind, status, note=None):
    d = load_caps(cfg) or {"capabilities": {}}
    now = now_iso()
    e = d["capabilities"].setdefault(cid, _new_cap(kind, status, now, [], seen=False))
    e["status"] = status
    if note is not None:
        e["note"] = note
    save_caps(cfg, d)


def cap_kind(cfg, cid):
    d = load_caps(cfg) or {"capabilities": {}}
    e = d["capabilities"].get(cid)
    if e:
        return e["kind"]
    return "plugin" if "@" in cid else "connector"


def referencing_profiles(profiles, cid):
    return [n for n, p in profiles.items()
            if cid in p["plugins"] + p["deny_plugins"] + p["connectors"] + p["deny_connectors"]]


def caps_view(caps, rows, profiles, oi, seen_ids):
    out = []
    for i, e in sorted(caps["capabilities"].items()):
        plug = e["kind"] == "plugin"
        n = sum(1 for r in rows if i in (r["desired"]["plugins"] if plug else r["desired"]["connectors_on"])
                and r["status"] != "new")
        out.append({"id": i, "kind": e["kind"], "status": e["status"], "first_seen": e.get("first_seen", ""),
                    "last_seen": e.get("last_seen", ""), "seen_on_orgs": e.get("seen_on_orgs", []),
                    "note": e.get("note", ""), "seen_now": i in seen_ids,
                    "granted_by": [nm for nm, p in profiles.items() if i in (p["plugins"] if plug else p["connectors"])],
                    "referenced_by": referencing_profiles(profiles, i), "projects": n,
                    "description": A().describe(i, oi) if plug else ""})
    return out


def show_inbox(cfg, out=print):
    d = load_caps(cfg)
    if d is None:
        out("no capabilities.json yet; run `scan` first")
        return 0
    profiles, _ = load_profiles(cfg)
    oi = A().account_info()
    caps = d["capabilities"]
    new = sorted(i for i, e in caps.items() if e["status"] == "new")
    gone = sorted(i for i, e in caps.items() if e["status"] == "gone")
    if not new and not gone:
        out("inbox is empty")
        return 0
    if new:
        out("New (%d):" % len(new))
        for i in new:
            e = caps[i]
            desc = A().describe(i, oi)
            out("  %-9s %s  orgs: %s%s" % (e["kind"], i, ", ".join(e.get("seen_on_orgs") or []) or "-",
                                          ("  | " + desc) if desc else ""))
    if gone:
        out("Gone (%d):" % len(gone))
        for i in gone:
            out("  %-9s %s  last seen %s  profiles: %s" % (caps[i]["kind"], i, (caps[i].get("last_seen") or "-")[:10],
                                                          ", ".join(referencing_profiles(profiles, i)) or "none"))
    return 0


def list_caps(cfg, status=None, out=print):
    d = load_caps(cfg)
    if d is None:
        out("no capabilities.json yet; run `scan` first")
        return 0
    for i, e in sorted(d["capabilities"].items()):
        if status and e["status"] != status:
            continue
        out("%-9s %-8s %-8s last seen %-10s %s" % (e["kind"], e["status"], ",".join(e.get("seen_on_orgs") or []) or "-",
                                                  (e.get("last_seen") or "-")[:10], i))
    return 0


def capability_ignore(cfg, cid, out=print):
    set_cap_status(cfg, cid, cap_kind(cfg, cid), "ignored")
    log_change(cfg, "(capability)", cid, "-", "ignored")
    out("ignored %s" % cid)
    return 0


def capability_adopt(cfg, cid, profile_names, dry=False, apply=True, out=print):
    """Add the capability to each profile (apply to their projects), then mark active.
    -> (rc, affected, profile_names)."""
    profiles, _ = load_profiles(cfg)
    bad = [n for n in profile_names if n not in profiles]
    if bad:
        out("unknown profile(s): %s" % ", ".join(bad))
        return 2, [], profile_names
    kind = cap_kind(cfg, cid)
    rc, affected, seen = 0, [], set()
    for n in profile_names:
        r, aff = _engine().profile_edit(cfg, n, "add", kind, cid, dry=dry, apply=apply, out=out)
        rc = rc or r
        for a in aff:
            if a["path"] not in seen:
                seen.add(a["path"])
                affected.append(a)
    if not dry and rc == 0:
        set_cap_status(cfg, cid, kind, "active")
        log_change(cfg, "(capability)", cid, "-", "adopted into " + ",".join(profile_names))
    return rc, affected, profile_names


def capability_retire(cfg, cid, dry=False, apply=True, out=print):
    """Remove from every profile and inline exception, apply, mark retired.
    -> (rc, affected, profiles that referenced it)."""
    profiles, meta = load_profiles(cfg)
    d = load_projects(cfg)
    kind = cap_kind(cfg, cid)
    cid = norm_conn(cid) if kind == "connector" else cid
    new_profiles = copy.deepcopy(profiles)
    hit = []
    for n, p in new_profiles.items():
        touched = False
        for k in ("plugins", "connectors", "deny_plugins", "deny_connectors"):
            if cid in p[k]:
                p[k] = [x for x in p[k] if x != cid]
                touched = True
        if touched:
            hit.append(n)
    projects = copy.deepcopy(d["projects"])
    inline_hit = []
    for r, e in projects.items():
        for k in INLINE_KEYS:
            if cid in e.get("inline", {}).get(k, []):
                e["inline"][k] = [x for x in e["inline"][k] if x != cid]
                if not e["inline"][k]:
                    del e["inline"][k]
                if r not in inline_hit:
                    inline_hit.append(r)
    rels = list(d["projects"]) if any(n in meta["default_profiles"] for n in hit) else \
        [r for r, e in d["projects"].items() if r in inline_hit or any(n in hit for n in e["profiles"])]
    if dry:
        affected = _engine()._project_changes(cfg, rels, projects, new_profiles, meta["default_profiles"])
        out("would retire %s: profiles %s; inline in %s; affects %d project(s)" % (
            cid, ", ".join(hit) or "none", ", ".join(inline_hit) or "none", len(rels)))
        for a in affected:
            out("  %s: %s" % (a["path"], ", ".join(a["changes"]) or a.get("note") or "no change"))
        return 0, affected, hit
    if hit:
        save_profiles(cfg, new_profiles, meta)
    if inline_hit:
        d["projects"] = projects
        save_projects(cfg, d)
    log_change(cfg, "(capability)", cid, "-", "retired (profiles: %s)" % ",".join(hit))
    out("retired %s (removed from profiles: %s; inline in: %s)" % (cid, ", ".join(hit) or "none", ", ".join(inline_hit) or "none"))
    rc = _engine()._apply_many(cfg, rels, out) if apply else 0
    set_cap_status(cfg, cid, kind, "retired")
    return rc, [], hit


def _caps_hint(cfg, hook):
    """File-only inbox reminder for the SessionStart hook, at most once a day."""
    if not hook:
        return ""
    d = load_caps(cfg)
    n = sum(1 for e in (d or {"capabilities": {}})["capabilities"].values() if e.get("status") == "new")
    if not n:
        return ""
    sp = state_read(".capabilities-hint.json")
    today = datetime.date.today().isoformat()
    if (read_json(sp, {}) or {}).get("date") == today:
        return ""
    try:
        atomic_write(state_write(".capabilities-hint.json"), json.dumps({"date": today}) + "\n")
    except Exception:
        return ""
    return " ntk: %d new capabilit%s waiting in the inbox (run `ntk` to review)." % (n, "y" if n == 1 else "ies")

