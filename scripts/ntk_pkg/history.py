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
import datetime
import json
import os
import shutil

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


def _save(cfg, before, label, changed, kind="change", ref=None):
    hid = _new_id()
    d = os.path.join(_dir(cfg), hid)
    os.makedirs(d, exist_ok=True)
    for n, raw in before.items():
        if raw is not None:
            with open(os.path.join(d, n), "wb") as f:
                f.write(raw)
    meta = {"id": hid, "time": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
            "label": label, "files": changed, "kind": kind, "ref": ref, "undone": False}
    with open(os.path.join(d, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)
    for old in entries(cfg)[KEEP:]:
        shutil.rmtree(os.path.join(_dir(cfg), old["id"]), ignore_errors=True)
    return meta


@contextlib.contextmanager
def recording(cfg, label):
    """Record the data files as they were before the block, if the block changed them."""
    before = _snapshot(cfg)
    try:
        yield
    finally:
        changed = _changed(before, _snapshot(cfg))
        if changed:
            _save(cfg, before, label, changed)


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
