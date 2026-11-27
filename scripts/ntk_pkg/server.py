"""Local HTTP server: the page and its JSON API. 127.0.0.1 only, Host check,
per-run token on every write."""
import http.server
import json
import os
import secrets
import threading
import webbrowser
from urllib.parse import parse_qs, urlparse

from .engine import (apply_all, do_apply, do_attach, do_inline, load_page, profile_default, profile_edit,
                     profile_new, scan)
from .lifecycle import capability_adopt, capability_ignore, capability_retire
from . import history
from .model import NAME_RE, SchemaTooNew, abs_of, assert_writable, load_projects


# --------------------------------------------------------------------- serve
def _bad(msg="bad request"):
    return 400, {"error": msg}


def handle_post(cfg, path, body, msgs):
    """Mutating API call (caller holds the lock). -> (status, payload). payload gets
    messages and state added by the caller unless it is an error."""
    S = lambda v: isinstance(v, str) and v
    assert_writable(cfg)
    projects = load_projects(cfg)["projects"]

    def proj():
        rel = body.get("path")
        if not S(rel) or rel.startswith("/") or ".." in rel.split("/"):
            return None
        if rel not in projects and not os.path.isdir(abs_of(cfg, rel)):
            return None
        return rel

    if path in ("/api/attach", "/api/detach"):
        rel = proj()
        if rel is None or not S(body.get("profile")):
            return _bad()
        return 200, {"rc": do_attach(cfg, rel, body["profile"], out=msgs.append, attach=path == "/api/attach")}
    if path == "/api/inline":
        rel = proj()
        if rel is None or body.get("kind") not in ("plugin", "connector") or not S(body.get("id")) \
                or body.get("value") not in ("on", "off", "clear"):
            return _bad()
        return 200, {"rc": do_inline(cfg, rel, body["kind"], body["id"], body["value"], out=msgs.append)}
    if path == "/api/apply":
        rel = proj()
        if rel is None:
            return _bad()
        return 200, {"rc": do_apply(cfg, rel, out=msgs.append)}
    if path == "/api/profile-default":
        if not S(body.get("name")) or body.get("op") not in ("add", "remove"):
            return _bad()
        dry = bool(body.get("dry_run"))
        rc, affected = profile_default(cfg, body["op"], body["name"], dry=dry, out=msgs.append)
        return 200, dict({"rc": rc}, **({"affected": affected} if dry else {}))
    if path == "/api/capability":
        act, cid = body.get("action"), body.get("id")
        profs = body.get("profiles")
        if act not in ("adopt", "ignore", "retire") or not S(cid) or \
                (act == "adopt" and not (isinstance(profs, list) and profs and all(S(x) for x in profs))):
            return _bad()
        dry = bool(body.get("dry_run"))
        if act == "ignore":
            return 200, {"rc": capability_ignore(cfg, cid, out=msgs.append)}
        if act == "adopt":
            rc, affected, plist = capability_adopt(cfg, cid, profs, dry=dry, out=msgs.append)
        else:
            rc, affected, plist = capability_retire(cfg, cid, dry=dry, out=msgs.append)
        return 200, dict({"rc": rc}, **({"affected": affected, "profiles": plist} if dry else {}))
    if path == "/api/profile":
        if not S(body.get("name")) or body.get("op") not in ("add", "remove", "deny", "undeny") \
                or body.get("kind") not in ("plugin", "connector") or not S(body.get("id")):
            return _bad()
        dry = bool(body.get("dry_run"))
        rc, affected = profile_edit(cfg, body["name"], body["op"], body["kind"], body["id"], dry=dry, out=msgs.append)
        out = {"rc": rc}
        if dry:
            out["affected"] = affected
        return 200, out
    if path == "/api/profile-new":
        if not S(body.get("name")) or not NAME_RE.match(body["name"]):
            return _bad("invalid profile name")
        fp = body.get("from_project")
        if fp is not None and (not S(fp) or fp not in projects):
            return _bad("unknown project")
        return 200, {"rc": profile_new(cfg, body["name"], str(body.get("description") or ""),
                                        str(body.get("group") or ""), fp, out=msgs.append)}
    return 404, {"error": "not found"}


def _label(path, body):
    """`ntk history` label for a console action, in the CLI's words."""
    name = path.rsplit("/", 1)[-1]
    keys = ("action", "op", "name", "path", "profile", "kind", "id", "value")
    vals = [str(body[k]) for k in keys if isinstance(body.get(k), str) and body.get(k)]
    if isinstance(body.get("profiles"), list):
        vals.append(",".join(str(x) for x in body["profiles"]))
    return " ".join([name] + vals) + " (console)"


def handle_undo(cfg, body, msgs):
    hid = body.get("id")
    if hid is not None and not (isinstance(hid, str) and hid):
        return _bad()
    assert_writable(cfg)
    rc, e = history.undo(cfg, hid, out=msgs.append)
    if e is None or rc:
        return 200, {"rc": rc}
    return 200, {"rc": apply_all(cfg, out=lambda m: msgs.append(m) if ("FAILED" in m or m.startswith(("(all", "totals"))) else None)}


def make_handler(cfg, token):
    lock = threading.Lock()
    state_lock = threading.Lock()

    class H(http.server.BaseHTTPRequestHandler):
        server_version = "ntk"

        def log_message(self, *a):
            pass

        def _host_ok(self):
            h = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
            return h in ("127.0.0.1", "localhost")

        def _send(self, code, body, ctype="application/json"):
            b = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            if not self._host_ok():
                return self._send(403, '{"error":"bad host"}')
            u = urlparse(self.path)
            try:
                if u.path == "/":
                    return self._send(200, load_page(token), "text/html")
                if u.path == "/api/state":
                    fresh = parse_qs(u.query).get("fresh", ["0"])[0] == "1"
                    with state_lock:
                        return self._send(200, json.dumps(scan(cfg, fresh=fresh)))
            except Exception as e:
                return self._send(500, json.dumps({"error": str(e)}))
            self._send(404, '{"error":"not found"}')

        def do_POST(self):
            if not self._host_ok():
                return self._send(403, '{"error":"bad host"}')
            if not secrets.compare_digest(self.headers.get("X-Ntk-Token") or "", token):
                return self._send(403, '{"error":"bad token"}')
            u = urlparse(self.path)
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                if not isinstance(body, dict):
                    return self._send(400, '{"error":"bad request"}')
                msgs = []
                with lock:
                    if u.path == "/api/undo":
                        code, payload = handle_undo(cfg, body, msgs)
                    elif body.get("dry_run"):
                        code, payload = handle_post(cfg, u.path, body, msgs)
                    else:
                        with history.recording(cfg, _label(u.path, body)):
                            code, payload = handle_post(cfg, u.path, body, msgs)
                if code != 200:
                    return self._send(code, json.dumps(payload))
                rc = payload.pop("rc")
                payload.update({"ok": rc == 0, "messages": msgs})
                if "affected" not in payload:  # a dry run writes nothing, so no new state
                    with state_lock:
                        payload["state"] = scan(cfg)
                self._send(200, json.dumps(payload))
            except SchemaTooNew as e:
                self._send(409, json.dumps({"error": str(e)}))
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}))

    return H


def serve(cfg, port, open_browser):
    token = secrets.token_urlsafe(24)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), make_handler(cfg, token))
    url = "http://127.0.0.1:%d/" % port
    print("ntk serving on %s (Ctrl-C to stop)" % url, flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()

