"""Paths, env overrides, config loading, atomic writes and the change log.
Nothing here knows about any agent tool; Claude-specific paths live in the adapter."""
import datetime
import json
import os
import tempfile

# scripts/ (this package sits in scripts/ntk_pkg/); console.html lives there
SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def env(name, default=None):
    """Environment override NTK_<name>; the old DEVCONSOLE_<name> still works (deprecated)."""
    return os.environ.get("NTK_" + name) or os.environ.get("DEVCONSOLE_" + name) or default


NEW_DIR = "~/.claude/agent-need-to-know"
OLD_DIR = "~/.claude/devconsole"  # the working name's directory; read as a fallback only


def _new_dir():
    return os.path.expanduser(NEW_DIR)


def _old_dir():
    return os.path.expanduser(OLD_DIR)


def config_path():
    """NTK_CONFIG (or deprecated DEVCONSOLE_CONFIG), else the new default; if only the
    old default exists it is read from there. Nothing is moved automatically."""
    e = env("CONFIG")
    if e:
        return e
    new = os.path.join(_new_dir(), "config.json")
    old = os.path.join(_old_dir(), "config.json")
    return old if (not os.path.exists(new) and os.path.exists(old)) else new


def state_dir():
    """Where hint and state files are written: next to an explicit config, else the new dir."""
    e = env("CONFIG")
    return os.path.dirname(e) if e else _new_dir()


def state_read(name):
    """Path to read a state file from: the new location, else the old one if it has it."""
    p = os.path.join(state_dir(), name)
    if not os.path.exists(p) and not env("CONFIG"):
        old = os.path.join(_old_dir(), name)
        if os.path.exists(old):
            return old
    return p


def state_write(name):
    return os.path.join(state_dir(), name)


def legacy_notes():
    """Notes for scan and the page footer when settings still live in the old location."""
    if env("CONFIG"):
        return []
    if config_path() == os.path.join(_old_dir(), "config.json"):
        return ["Config is read from %s; it can be moved to %s (copy config.json, then remove the old folder)."
                % (OLD_DIR + "/", NEW_DIR + "/")]
    return []



def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def atomic_write(path, text, mode=None):
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".ntk-tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        if mode is None:
            try:
                mode = os.stat(path).st_mode & 0o777
            except OSError:
                mode = 0o644
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_json_2(path, obj):
    atomic_write(path, json.dumps(obj, indent=2, ensure_ascii=False) + "\n")



class Config:
    def __init__(self, root, data_dir):
        self.root = os.path.abspath(os.path.expanduser(root))
        self.data_dir = os.path.abspath(os.path.expanduser(data_dir))

    @property
    def profiles_path(self):
        return os.path.join(self.data_dir, "profiles.json")

    @property
    def projects_path(self):
        return os.path.join(self.data_dir, "projects.json")

    @property
    def cache_path(self):
        return os.path.join(self.data_dir, ".ntk-cache.json")

    @property
    def cache_read_path(self):
        new, old = self.cache_path, os.path.join(self.data_dir, ".devconsole-cache.json")
        return old if (not os.path.exists(new) and os.path.exists(old)) else new

    @property
    def caps_path(self):
        return os.path.join(self.data_dir, "capabilities.json")

    @property
    def log_path(self):
        """ntk.log; an existing devconsole.log keeps being appended to if ntk.log is absent."""
        new, old = os.path.join(self.data_dir, "ntk.log"), os.path.join(self.data_dir, "devconsole.log")
        return old if (not os.path.exists(new) and os.path.exists(old)) else new


def load_config():
    c = read_json(config_path())
    if not isinstance(c, dict) or "root" not in c:
        return None
    return Config(c["root"], c.get("data_dir") or c["root"])


INLINE_KEYS = ("plugins_on", "plugins_off", "connectors_on", "connectors_off")

def now_iso():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def log_change(cfg, rel, item, old, new):
    ts = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    os.makedirs(cfg.data_dir, exist_ok=True)
    with open(cfg.log_path, "a", encoding="utf-8") as f:
        f.write("%s\t%s\t%s\t%s -> %s\n" % (ts, rel, item, old, new))

