# -*- coding: utf-8 -*-
"""Horidoro AV — self-update (opt-in, consent-first).

Checks GitHub Releases (public API, no auth) for a newer version. If one
exists, downloads the single-file app + checksums, verifies SHA-256, and
swaps the file atomically. Settings/quarantine/logs/sounds are untouched —
they live in separate directories.

The version check is the app's ONE deliberate network contact: it only
happens when the user leaves the automatic check on (default ON) or presses
"Check now", and it only reads the public release metadata — nothing is
sent.
"""

import hashlib
import json
import os
import re
import shutil
import urllib.request

from branding import APP_NAME, HOMEPAGE, VERSION
from installer import APP_INSTALL_PATH, TMP_DIR


def _ua():
    return f"{APP_NAME}/{VERSION} (self-update)"


def _download(url, dest):
    req = urllib.request.Request(url, headers={"User-Agent": _ua()})
    with urllib.request.urlopen(req, timeout=30) as r:
        with open(dest, "wb") as f:
            while True:
                chunk = r.read(1 << 16)
                if not chunk:
                    break
                f.write(chunk)


def _api_latest_url():
    """API URL for the latest release, derived from the website HOMEPAGE:
    https://github.com/OWNER/REPO -> https://api.github.com/repos/OWNER/REPO/…
    (HOMEPAGE + '/releases/latest' is the HTML PAGE — that bug made the
    updater fetch a web page and never find a release.)"""
    path = HOMEPAGE.rstrip("/").replace("https://github.com/", "").strip("/")
    return f"https://api.github.com/repos/{path}/releases/latest"


def _latest_release():
    """Latest release metadata from GitHub, or None on any failure."""
    req = urllib.request.Request(
        _api_latest_url(), headers={"Accept": "application/vnd.github+json",
                                   "User-Agent": _ua()})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 — offline / unreachable → no update
        return None


def parse_version(tag):
    """'v0.1.1' -> (0, 1, 1); anything unparseable -> (0, 0, 0)."""
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", tag or "")
    if not m:
        return (0, 0, 0)
    return tuple(int(x) for x in m.groups())


def check_for_update():
    """Return (status, release).

    status: 'update' (newer release dict), 'current' (up to date),
    'error' (couldn't reach GitHub)."""
    rel = _latest_release()
    if rel is None:
        return "error", None
    if parse_version(rel.get("tag_name", "")) > parse_version(VERSION):
        return "update", rel
    return "current", None


def _asset_url(release, name):
    for a in release.get("assets") or []:
        if a.get("name") == name:
            return a.get("browser_download_url")
    return None


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1 << 20)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _verify(app_file, checksums_file):
    """True if app_file's SHA-256 matches its line in checksums.txt."""
    try:
        want = None
        for line in open(checksums_file, encoding="utf-8", errors="replace"):
            parts = line.split()
            if len(parts) >= 2 and os.path.basename(parts[1]) == "horidoro-av.py":
                want = parts[0].lower()
                break
        if not want:
            return False
        return _sha256(app_file) == want
    except OSError:
        return False


def can_update():
    """Only the self-installed app updates itself (never a dev copy)."""
    try:
        return os.path.realpath(os.path.abspath(__file__)) == str(APP_INSTALL_PATH)
    except Exception:  # noqa: BLE001
        return False


def _restart_watcher():
    """Restart the watcher service so the new code is live everywhere
    (a healthy service won't restart on its own after a file swap)."""
    try:
        import subprocess
        subprocess.run(["systemctl", "--user", "restart",
                        "horidoro-watcher.service"],
                       capture_output=True, timeout=30)
    except Exception:  # noqa: BLE001 — best-effort
        pass


def apply_update(release):
    """Download, verify, atomically swap the app file. True on success."""
    py_url = _asset_url(release, "horidoro-av.py")
    cs_url = _asset_url(release, "checksums.txt")
    if not py_url or not cs_url:
        return False
    try:
        TMP_DIR.mkdir(parents=True, exist_ok=True)
        py_path = TMP_DIR / "horidoro-av.update"
        cs_path = TMP_DIR / "checksums.update"
        _download(py_url, py_path)
        _download(cs_url, cs_path)
        if not _verify(py_path, cs_path):
            try:
                py_path.unlink(missing_ok=True)
                cs_path.unlink(missing_ok=True)
            except OSError:
                pass
            return False  # checksum mismatch — refuse to install
        backup = APP_INSTALL_PATH.with_name(APP_INSTALL_PATH.name + ".bak")
        shutil.copy2(py_path, backup)
        os.replace(py_path, APP_INSTALL_PATH)  # atomic on the same filesystem
        os.chmod(APP_INSTALL_PATH, 0o755)
        try:
            backup.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            cs_path.unlink(missing_ok=True)
        except OSError:
            pass
        _restart_watcher()
        return True
    except Exception:  # noqa: BLE001 — update failure is never fatal
        return False
