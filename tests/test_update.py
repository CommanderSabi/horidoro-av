#!/usr/bin/env python3
"""Horidoro AV — self-update test (checksum-verified, atomic swap)."""
import os
import shutil
import sys
import tempfile
from pathlib import Path

os.environ["HOME"] = tempfile.mkdtemp(prefix="horidoro-update-")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "horidoro"))

import updater  # noqa: E402

failures = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" — {detail}" if detail else ""))
    if not cond:
        failures.append(name)


# --- version parsing ---------------------------------------------------------
check("parse v0.1.1", updater.parse_version("v0.1.1") == (0, 1, 1))
check("parse 0.2.0", updater.parse_version("0.2.0") == (0, 2, 0))
check("parse garbage -> 0.0.0", updater.parse_version("junk") == (0, 0, 0))
check("0.1.1 > 0.1.0", updater.parse_version("v0.1.1") > updater.parse_version("v0.1.0"))

# --- check_for_update with a fake release source ------------------------------
_real_latest_release = updater._latest_release
updater._latest_release = lambda: {"tag_name": "v0.1.2", "assets": []}
s, r = updater.check_for_update()
check("newer version detected", s == "update" and r["tag_name"] == "v0.1.2")
updater._latest_release = lambda: {"tag_name": "v0.1.0", "assets": []}
check("same version -> current", updater.check_for_update() == ("current", None))
updater._latest_release = lambda: None
check("unreachable -> error", updater.check_for_update() == ("error", None))

# --- the check must hit the GitHub API, not the website HTML page -----------
# (regression: HOMEPAGE + '/releases/latest' is the HTML page — the updater
# fetched a web page, json.loads failed, and updates could never be found)
import io as _io
import json as _json
captured = {}


def fake_urlopen(req, timeout=None):
    captured["url"] = req.full_url
    return _io.BytesIO(_json.dumps({"tag_name": "v0.1.0", "assets": []}).encode())


updater._latest_release = _real_latest_release  # the REAL implementation
updater.urllib.request.urlopen = fake_urlopen
updater.check_for_update()
expect_url = ("https://api.github.com/repos/"
              + updater.HOMEPAGE.rstrip("/").replace("https://github.com/", "")
              + "/releases/latest")
check("check hits the API URL, not the HTML page",
      captured["url"] == expect_url, captured["url"])

# --- apply_update: real checksum verify + atomic swap, downloads faked -------
sandbox = tempfile.mkdtemp(prefix="upd-server-")
base = tempfile.mkdtemp(prefix="upd-app-")
app_path = Path(base) / "horidoro-av"
updater.APP_INSTALL_PATH = app_path
updater.TMP_DIR = Path(base) / "tmp"


def fake_download(url, dest):
    name = os.path.basename(dest)
    name = {"horidoro-av.update": "horidoro-av.py",
            "checksums.update": "checksums.txt"}.get(name, name)
    shutil.copy2(os.path.join(sandbox, name), dest)


updater._download = fake_download

rel = {"tag_name": "v0.1.1", "assets": [
    {"name": "horidoro-av.py", "browser_download_url": "s"},
    {"name": "checksums.txt", "browser_download_url": "s"}]}

app_bytes = b"#!/usr/bin/env python3\n# fake app v0.1.1\n"
open(os.path.join(sandbox, "horidoro-av.py"), "wb").write(app_bytes)
open(os.path.join(sandbox, "checksums.txt"), "w").write(
    f"{updater._sha256(os.path.join(sandbox, 'horidoro-av.py'))}  horidoro-av.py\n")
app_path.write_bytes(b"old version")

check("update applied (correct checksum)",
      updater.apply_update(rel) and app_path.read_bytes() == app_bytes)
check("installed file is executable", os.access(app_path, os.X_OK))

# tampered download must be refused — the old file stays untouched
open(os.path.join(sandbox, "horidoro-av.py"), "wb").write(app_bytes + b"# tampered\n")
open(os.path.join(sandbox, "checksums.txt"), "w").write(
    "0" * 64 + "  horidoro-av.py\n")
app_path.write_bytes(b"old version")
check("tampered update refused",
      not updater.apply_update(rel) and app_path.read_bytes() == b"old version")

check("can_update False in dev/test", updater.can_update() is False)

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("ALL UPDATE TESTS PASSED")
