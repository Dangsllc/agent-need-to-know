"""Undo for ntk's own changes.

Every command or console action that changes profiles.json, projects.json or a
capability's status is recorded: the files as they were before it are copied to
data_dir/history/<id>/ with a short label. `undo` puts the files back and the
caller re-applies, so plugins, connectors and global plugins follow. An undo is
itself recorded, so it can be undone too (that's redo).

What this covers is ntk's own data. A change made outside ntk (running
`claude plugin ...` by hand, editing settings files) is not recorded; the next
apply moves such things back to what the profiles say.
"""
import contextlib
import copy
import datetime
import json
import os
import shutil

from .model import write_versioned

FILES = ("profiles.json", "projects.json", "capabilities.json")
KEEP = 100


def _dir(cfg):
    return os.path.join(cfg.data_dir, "history")


def _read(path):
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


def _caps_key(raw):
    """Only the decisions in capabilities.json count (status, note); scans rewrite
    last_seen and orgs all the time, which is not a change worth undoing."""
    if raw is None:
        return None
    try:
        caps = json.loads(raw.decode("utf-8")).get("capabilities", {})
    except Exception:
        return raw
    return json.dumps({k: [v.get("status"), v.get("note", "")] for k, v in sorted(caps.items())})


def _key(name, raw):
    return _caps_key(raw) if name == "capabilities.json" else raw


def _snapshot(cfg):
    return {n: _read(os.path.join(cfg.data_dir, n)) for n in FILES}


def _changed(before, after):
    return [n for n in FILES if _key(n, before[n]) != _key(n, after[n])]


def _new_id():
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")


def _save(cfg, before, label, changed, kind="change", ref=None, log=None):
    hid = _new_id()
    d = os.path.join(_dir(cfg), hid)
    os.makedirs(d, exist_ok=True)
    for n, raw in before.items():
        if raw is not None:
            with open(os.path.join(d, n), "wb") as f:
                f.write(raw)
    meta = {"id": hid, "time": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
            "label": label, "files": changed, "kind": kind, "ref": ref, "undone": False,
            "log": log or []}
    with open(os.path.join(d, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)
    for old in entries(cfg)[KEEP:]:
        shutil.rmtree(os.path.join(_dir(cfg), old["id"]), ignore_errors=True)
    return meta


def _log_size(cfg):
    try:
        return os.path.getsize(cfg.log_path)
    except OSError:
        return 0


def _log_since(cfg, pos):
    """Change-log lines written since byte `pos`: [{"time", "where", "item", "change"}]."""
    try:
        with open(cfg.log_path, "rb") as f:
            f.seek(pos)
            text = f.read().decode("utf-8", "replace")
    except OSError:
        return []
    return [_parse_log(l) for l in text.splitlines() if l.strip()]


def _parse_log(line):
    p = line.split("\t")
    if len(p) >= 4:
        return {"time": p[0], "where": p[1], "item": p[2], "change": "\t".join(p[3:])}
    return {"time": "", "where": "", "item": "", "change": line}


def change_log(cfg, n=300):
    """The newest `n` change-log lines, newest first, each tagged with the history
    entry it belongs to when one recorded it."""
    try:
        with open(cfg.log_path, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return []
    rows = [_parse_log(l) for l in lines[-n:] if l.strip()]
    owner = {}
    for e in entries(cfg):
        for l in e.get("log") or []:
            owner[(l["time"], l["where"], l["item"], l["change"])] = e["id"]
    for r in rows:
        r["entry"] = owner.get((r["time"], r["where"], r["item"], r["change"]))
    return rows[::-1]


@contextlib.contextmanager
def recording(cfg, label):
    """Record the data files as they were before the block, if the block changed
    them, with the change-log lines the block wrote."""
    before, pos = _snapshot(cfg), _log_size(cfg)
    try:
        yield
    finally:
        changed = _changed(before, _snapshot(cfg))
        if changed:
            _save(cfg, before, label, changed, log=_log_since(cfg, pos))


def entries(cfg):
    """Recorded changes, newest first."""
    out = []
    d = _dir(cfg)
    if not os.path.isdir(d):
        return out
    for hid in sorted(os.listdir(d), reverse=True):
        try:
            with open(os.path.join(d, hid, "meta.json"), encoding="utf-8") as f:
                out.append(json.load(f))
        except Exception:
            continue
    return out


def latest_undoable(cfg):
    for e in entries(cfg):
        if e.get("kind") != "undo" and not e.get("undone"):
            return e
    return None


def _set_meta(cfg, hid, **kv):
    p = os.path.join(_dir(cfg), hid, "meta.json")
    with open(p, encoding="utf-8") as f:
        m = json.load(f)
    m.update(kv)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(m, f, indent=2)


def undo(cfg, hid=None, dry=False, out=print):
    """Restore the data files from before change `hid` (default: the newest change
    not yet undone). Every later change is reverted with it. -> (rc, entry)."""
    es = entries(cfg)
    target = next((e for e in es if e["id"] == hid), None) if hid else latest_undoable(cfg)
    if target is None:
        out("nothing to undo" if not hid else "no history entry %s (see `ntk history`)" % hid)
        return (0 if not hid else 2), None
    later = [e for e in es if e["id"] > target["id"] and e.get("kind") != "undo" and not e.get("undone")]
    src = os.path.join(_dir(cfg), target["id"])
    if dry:
        out("would undo: %s (%s)%s" % (target["label"], target["time"],
                                       "; also reverts %d later change(s)" % len(later) if later else ""))
        return 0, target
    before = _snapshot(cfg)
    for n in FILES:
        raw = _read(os.path.join(src, n))
        if raw is None:
            continue
        dst = os.path.join(cfg.data_dir, n)
        if n == "capabilities.json" and before[n] is not None:
            raw = _merge_caps(before[n], raw)
        tmp = dst + ".undo-tmp"
        with open(tmp, "wb") as f:
            f.write(raw)
        os.replace(tmp, dst)
    changed = _changed(before, _snapshot(cfg))
    _save(cfg, before, "undo: " + target["label"], changed, kind="undo", ref=target["id"])
    for e in [target] + later:
        _set_meta(cfg, e["id"], undone=True)
    out("undid: %s (%s)%s" % (target["label"], target["time"],
                              "; also reverted %d later change(s)" % len(later) if later else ""))
    return 0, target


def _merge_caps(cur_raw, old_raw):
    """Put back statuses and notes from the old file; keep everything else current."""
    try:
        cur = json.loads(cur_raw.decode("utf-8"))
        old = json.loads(old_raw.decode("utf-8")).get("capabilities", {})
    except Exception:
        return old_raw
    for k, v in cur.get("capabilities", {}).items():
        if k in old:
            v["status"] = old[k].get("status", v.get("status"))
            v["note"] = old[k].get("note", v.get("note", ""))
    return (json.dumps(cur, indent=2) + "\n").encode("utf-8")


# ------------------------------------------------------------ undo just one change
def _load(raw):
    return None if raw is None else json.loads(raw.decode("utf-8"))


def _after(cfg, target):
    """The data files right after `target`: the next newer entry's "before", or now."""
    newer = sorted((e for e in entries(cfg) if e["id"] > target["id"]), key=lambda e: e["id"])
    if newer:
        d = os.path.join(_dir(cfg), newer[0]["id"])
        return {n: _read(os.path.join(d, n)) for n in FILES}
    return _snapshot(cfg)


def _diff(a, b, path=()):
    """Changes from a to b: dict keys added/removed/changed, list items added/removed."""
    ops = []
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            if k == "schema_version":
                continue
            if k not in b:
                ops.append(("del", path, k, a[k]))
            elif k not in a:
                ops.append(("add", path, k, b[k]))
            else:
                ops += _diff(a[k], b[k], path + (k,))
    elif isinstance(a, list) and isinstance(b, list) and all(not isinstance(x, (dict, list)) for x in a + b):
        ops += [("ldel", path, None, x) for x in a if x not in b]
        ops += [("ladd", path, None, x) for x in b if x not in a]
    elif a != b:
        ops.append(("set", path, a, b))
    return ops


def _at(doc, path):
    for k in path:
        if not isinstance(doc, dict) or k not in doc:
            return None
        doc = doc[k]
    return doc


def _invert(doc, ops):
    """Apply the inverse of `ops` to `doc` in place. -> conflicts (list of str)."""
    bad = []
    for op, path, k, v in ops:
        where = "/".join(str(p) for p in path)
        if op == "set":
            parent = _at(doc, path[:-1])
            cur = parent.get(path[-1]) if isinstance(parent, dict) else None
            if cur == v:
                parent[path[-1]] = k  # k holds the old value here
            elif cur != k:
                bad.append("%s was changed again later" % where)
            continue
        node = _at(doc, path)
        if op in ("ldel", "ladd"):
            if not isinstance(node, list):
                if op == "ldel":
                    bad.append("%s no longer exists" % where)
                continue
            if op == "ladd" and v in node:
                node.remove(v)
            elif op == "ldel" and v not in node:
                node.append(v)
        elif op == "add":
            if isinstance(node, dict) and k in node:
                if node[k] == v:
                    del node[k]
                else:
                    bad.append("%s/%s was changed again later" % (where, k))
        elif op == "del":
            if not isinstance(node, dict):
                bad.append("%s no longer exists" % where)
            elif k not in node:
                node[k] = copy.deepcopy(v)
            elif node[k] != v:
                bad.append("%s/%s was changed again later" % (where, k))
    return bad


def undo_only(cfg, hid, dry=False, out=print):
    """Undo just change `hid`, keeping every later change. Refused, with the reason,
    when a later change touched the same thing. -> (rc, entry)."""
    target = next((e for e in entries(cfg) if e["id"] == hid), None)
    if target is None:
        out("no history entry %s (see `ntk history`)" % hid)
        return 2, None
    if target.get("undone"):
        out("already undone: %s" % target["label"])
        return 2, None
    src = os.path.join(_dir(cfg), hid)
    after = _after(cfg, target)
    cur = _snapshot(cfg)
    new, conflicts = {}, []
    for n in ("profiles.json", "projects.json"):
        b, a = _load(_read(os.path.join(src, n))), _load(after[n])
        if b is None or a is None or b == a:
            continue
        doc = _load(cur[n])
        conflicts += ["%s: %s" % (n, c) for c in _invert(doc, _diff(b, a))]
        if doc != _load(cur[n]):
            new[n] = doc
    b_caps = (_load(_read(os.path.join(src, "capabilities.json"))) or {}).get("capabilities", {})
    a_caps = (_load(after["capabilities.json"]) or {}).get("capabilities", {})
    c_doc = _load(cur["capabilities.json"])
    if c_doc is not None:
        for k, v in b_caps.items():
            av = a_caps.get(k, {})
            if av.get("status") != v.get("status") and k in c_doc.get("capabilities", {}):
                ce = c_doc["capabilities"][k]
                if ce.get("status") == av.get("status"):
                    ce["status"], ce["note"] = v.get("status"), v.get("note", "")
                    new["capabilities.json"] = c_doc
                else:
                    conflicts.append("capabilities.json: %s was changed again later" % k)
    if conflicts:
        out("can't undo just %r: %s. Use undo to go back to before it instead (that also "
            "reverts the later changes)." % (target["label"], "; ".join(conflicts)))
        return 1, None
    if not new:
        out("nothing left to undo for %r" % target["label"])
        return 0, None
    if dry:
        out("would undo just: %s (%s)" % (target["label"], target["time"]))
        return 0, target
    for n, doc in new.items():
        write_versioned(os.path.join(cfg.data_dir, n), doc)
    changed = _changed(cur, _snapshot(cfg))
    _save(cfg, cur, "undo just: " + target["label"], changed, kind="undo", ref=hid)
    _set_meta(cfg, hid, undone=True)
    out("undid just: %s (%s)" % (target["label"], target["time"]))
    return 0, target
