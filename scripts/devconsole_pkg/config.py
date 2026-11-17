"""Paths, env overrides, config loading, atomic writes and the change log.
Nothing here knows about any agent tool; Claude-specific paths live in the adapter."""
import datetime
import json
import os
import tempfile

# scripts/ (this package sits in scripts/devconsole_pkg/); console.html lives there
SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def config_path():
    return os.environ.get("DEVCONSOLE_CONFIG") or os.path.expanduser("~/.claude/devconsole/config.json")



def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def atomic_write(path, text, mode=None):
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".dc-tmp-")
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
        return os.path.join(self.data_dir, ".devconsole-cache.json")

    @property
    def caps_path(self):
        return os.path.join(self.data_dir, "capabilities.json")

    @property
    def log_path(self):
        return os.path.join(self.data_dir, "devconsole.log")


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

