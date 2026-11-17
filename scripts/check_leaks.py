#!/usr/bin/env python3
"""Fail if the repository contains things that must not be published: absolute
home-directory paths, email addresses, or common secret shapes. Standard library
only. Run from anywhere: `python3 scripts/check_leaks.py [path ...]` (default: every
file git tracks, or every file under the repo when git is unavailable).
Exit 0 when clean, 1 when something is found."""
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Patterns are assembled from pieces so this file does not match itself.
HOME_PATH = re.compile("/" + "Users/[a-z]|/" + "home/[a-z]")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
EMAIL_OK = re.compile(r"^noreply@anthropic\.com$|@(?:[A-Za-z0-9-]+\.)*example\.com$", re.I)
SECRETS = [
    ("openai/anthropic-style key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}")),
    ("github token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}")),
    ("aws access key id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("google api key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("private key block", re.compile("-----BEGIN [A-Z ]*PRIVATE" + " KEY-----")),
]


def scan_text(text):
    """-> [(line number, kind, matched text)]"""
    found = []
    for n, line in enumerate(text.splitlines(), 1):
        for m in HOME_PATH.finditer(line):
            found.append((n, "absolute home path", m.group(0)))
        for m in EMAIL.finditer(line):
            if not EMAIL_OK.search(m.group(0)):
                found.append((n, "email address", m.group(0)))
        for kind, rx in SECRETS:
            for m in rx.finditer(line):
                found.append((n, kind, m.group(0)[:12] + "..."))
    return found


def files(paths):
    if paths:
        return [os.path.abspath(p) for p in paths]
    try:
        out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True).stdout
        names = [x for x in out.decode("utf-8").split("\0") if x]
        if names:
            return [os.path.join(ROOT, x) for x in names]
    except Exception:
        pass
    res = []
    for d, dirs, fs in os.walk(ROOT):
        dirs[:] = [x for x in dirs if x not in (".git", "__pycache__")]
        res += [os.path.join(d, f) for f in fs]
    return res


def main(argv=None):
    bad = 0
    nfiles = 0
    for p in files(argv if argv is not None else sys.argv[1:]):
        try:
            with open(p, "rb") as f:
                raw = f.read()
        except OSError:
            continue
        if b"\0" in raw:
            continue  # binary
        nfiles += 1
        for n, kind, what in scan_text(raw.decode("utf-8", "replace")):
            bad += 1
            print("%s:%d: %s: %s" % (os.path.relpath(p, ROOT), n, kind, what))
    if bad:
        print("check_leaks: %d finding(s) in %d file(s)" % (bad, nfiles))
        return 1
    print("check_leaks: clean (%d files)" % nfiles)
    return 0


if __name__ == "__main__":
    sys.exit(main())
