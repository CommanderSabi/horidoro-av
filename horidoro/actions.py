# -*- coding: utf-8 -*-
"""Horidoro AV — scan / update / engine / quarantine / log actions.

Every action is hardened against the battle-tested ClamAV pitfalls:
  - clamdscan --multiscan --fdpass (NEVER --stream)
  - clamdscan --ping --wait for engine readiness
  - timeout wrappers so a hung scan can never wedge the chain
  - the Bouncer lock-file protocol for the engine lifecycle
"""

import getpass
import glob
import os
import queue
import re
import shlex
import shutil
import threading
import time
from pathlib import Path

from branding import APP_NAME, CONFIG_DIR, CONTAINER_NAME, HOMEPAGE, VERSION
from installer import CONF, LOG_DIR, QUARANTINE_DIR, SCRIPT_DIR, SOUNDS_DIR, TMP_DIR
from shell import db_version, distrobox, engine_status, in_container, run
from state import State
from watcher import HashCache, Watcher


def update_db():
    """Update virus definitions inside the container."""
    rc, out = distrobox("sudo freshclam", timeout=1800)
    if rc != 0:
        raise RuntimeError(f"Virus database update failed: {out[-300:]}")
    return out


def engine_on():
    """Start clamd if not running; wait until it is actually ready."""
    rc, out = distrobox(
        "pgrep -x clamd > /dev/null || (sudo clamd -c " + CONF +
        " && clamdscan --ping --wait > /dev/null 2>&1)")
    return rc == 0


def engine_off():
    """Stop clamd (safe: the Bouncer lock file prevents killing active scans)."""
    return distrobox("sudo pkill -9 clamd")[0] == 0


def engine_running():
    return "RUNNING" in distrobox("pgrep -x clamd > /dev/null && echo RUNNING || echo STOPPED")[1]


def scan_path(path, mode="quick"):
    """Scan a file/folder through the verified helper script."""
    target = shlex.quote(str(path))
    if mode == "verbose":
        # standalone clamscan (no daemon) — verbose OK-per-file output.
        # clamscan ignores /etc/clamd.d/scan.conf, so the always-on exclusions
        # must be passed explicitly (quarantine + container overlay trees).
        qdir = shlex.quote(str(QUARANTINE_DIR))
        log = shlex.quote(str(QUARANTINE_DIR.parent / "logs/manual_scan.log"))
        excl_q = shlex.quote("^" + str(QUARANTINE_DIR))
        excl_overlay = shlex.quote(
            "^/(var/)?home/[^/]+/\\.local/share/containers/storage/overlay")
        excl_overlayc = shlex.quote(
            "^/(var/)?home/[^/]+/\\.local/share/containers/storage/overlay-containers")
        rc, out = distrobox(
            f"clamscan -r --max-filesize=0 --max-scansize=0 --max-files=0 "
            f"--max-recursion=20 "
            f"--exclude-dir={excl_q} --exclude-dir={excl_overlay} "
            f"--exclude-dir={excl_overlayc} "
            f"--move={qdir} --log={log} "
            f"{target} 2>&1")
        _record_origins_from_log(QUARANTINE_DIR.parent / "logs" / "manual_scan.log")
        return out
    # quick: daemon-powered via the helper (engine lifecycle + cleanup built in)
    rc, out = run(f'"{SCRIPT_DIR}/scan_helper.sh" "{target}"', timeout=6 * 3600)
    if rc != 0:
        raise RuntimeError(f"Scan failed: {out[-300:]}")
    return out


# ---------------------------------------------------------------------------
# quarantine
# ---------------------------------------------------------------------------
def list_quarantine():
    """Return [{name, size, mtime}] — excluding clamd's internal lock files
    and the app's own origin.json record (not user data)."""
    entries = []
    if not QUARANTINE_DIR.exists():
        return entries
    _merge_script_origins()  # pick up origins recorded by the scan scripts
    for f in sorted(QUARANTINE_DIR.iterdir()):
        if f.name.startswith(".clamav-quarantine-lock"):
            continue
        if f.name == "origin.json":  # the app's restore-origin record, not a threat
            continue
        entries.append({"name": f.name, "size": f.stat().st_size,
                        "mtime": f.stat().st_mtime})
    return entries


def delete_quarantined(name):
    target = QUARANTINE_DIR / name
    if target.exists():
        target.unlink()
    _forget_origin(name)
    return True


def restore_quarantined(name, destination):
    """Move a quarantined file to a chosen destination.

    The original location is NOT recorded by clamdscan --move, so restore
    goes to a user-chosen folder (or use restore_quarantined_to_origin when
    an origin was recorded). Prunes the origin entry."""
    src = QUARANTINE_DIR / name
    dest = Path(destination)
    dest.mkdir(parents=True, exist_ok=True)
    if src.exists():
        src.rename(dest / name)
    _forget_origin(name)
    return True


# --- origin tracking (where quarantined files came from, for restore) --------
_ORIGIN_FILE = QUARANTINE_DIR / "origin.json"


def _load_origins():
    try:
        if _ORIGIN_FILE.exists():
            import json as _json
            return _json.loads(_ORIGIN_FILE.read_text()) or {}
    except (OSError, ValueError):
        pass
    return {}


def _save_origins(origins):
    try:
        import json as _json
        QUARANTINE_DIR.mkdir(parents=True, exist_ok=True)
        _ORIGIN_FILE.write_text(_json.dumps(origins, indent=2))
    except OSError:
        pass


def _forget_origin(name):
    origins = _load_origins()
    if name in origins:
        del origins[name]
        _save_origins(origins)


def record_origin(path):
    """Record where a quarantined file came from (for restore-to-origin)."""
    try:
        origins = _load_origins()
        origins[os.path.basename(path)] = str(path)
        _save_origins(origins)
    except OSError:
        pass


def _record_origins_from_log(log_path):
    """After a VERBOSE scan, record origins from the clamscan log's FOUND
    lines (the scan scripts do this themselves; the verbose path runs a
    direct clamscan, so it does it here). Only files that actually landed in
    quarantine get an entry."""
    try:
        if not log_path.exists():
            return
        for line in log_path.read_text(errors="replace").splitlines():
            line = line.rstrip()
            if line.endswith(" FOUND") and ": " in line:
                src = line.split(": ", 1)[0].strip()
                b = os.path.basename(src)
                if b and (QUARANTINE_DIR / b).exists():
                    record_origin(src)
    except OSError:
        pass


def _merge_script_origins():
    """Pull best-effort origins written by the scan scripts (origins.tmp)."""
    try:
        tmp = TMP_DIR / "origins.tmp"
        if not tmp.exists():
            return
        origins = _load_origins()
        changed = False
        for line in tmp.read_text(errors="replace").splitlines():
            if "|" in line:
                name, path = line.split("|", 1)
                if name and path and name not in origins:
                    origins[name] = path
                    changed = True
        tmp.unlink(missing_ok=True)
        if changed:
            _save_origins(origins)
    except OSError:
        pass


def get_origin(name):
    """Recorded original path of a quarantined file, if known."""
    _merge_script_origins()
    return _load_origins().get(name)


def restore_quarantined_to_origin(name):
    """Restore a quarantined file to its recorded original location.
    Returns the restored path, or None (unknown origin / origin taken)."""
    origin = get_origin(name)
    if not origin:
        return None
    src = QUARANTINE_DIR / name
    dest = Path(origin)
    try:
        if dest.exists():
            return None  # don't clobber an existing file — use the chooser
        dest.parent.mkdir(parents=True, exist_ok=True)
        if src.exists():
            src.rename(dest)
        _forget_origin(name)
        return str(dest)
    except OSError:
        return None


# ---------------------------------------------------------------------------
# logs
# ---------------------------------------------------------------------------
def read_log(name):
    """Read a scan log (already noise-filtered by the scripts)."""
    path = QUARANTINE_DIR.parent / "logs" / name
    if not path.exists():
        return "(no log yet)"
    return path.read_text(errors="replace")[-50000:]  # last 50KB, never huge


# ---------------------------------------------------------------------------
# on-access watcher (engine hold + per-file scan + headless loop)
# ---------------------------------------------------------------------------
WATCH_LOCK_ENTRY = "watcher"   # keeps the engine alive while watching


def watch_engine_up():
    """Start clamd if needed and mark the watcher as a lock holder so the
    scripts' Bouncer never shuts the engine down while on-access is active."""
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    lock = TMP_DIR / "horidoro_scan.lock"
    rc, _ = distrobox(
        f"touch '{lock}' && (grep -q {WATCH_LOCK_ENTRY} '{lock}' 2>/dev/null "
        f"|| echo {WATCH_LOCK_ENTRY} >> '{lock}')")
    if rc != 0:
        return False
    rc, _ = distrobox(
        f"pgrep -x clamd > /dev/null || (sudo clamd -c {CONF} && "
        f"clamdscan --ping --wait > /dev/null 2>&1)")
    return rc == 0


def watch_engine_down():
    """Release the watcher hold; if the lock is now empty the engine shuts
    down (Bouncer protocol) and the lock file is cleaned up."""
    lock = TMP_DIR / "horidoro_scan.lock"
    distrobox(
        f"sed -i '/^{WATCH_LOCK_ENTRY}$/d' '{lock}' 2>/dev/null; "
        f"if [ ! -s '{lock}' ] || [ $(tr -d '[:space:]' < '{lock}' | wc -c) -eq 0 ]; then "
        f"sudo pkill -9 clamd 2>/dev/null; rm -f '{lock}'; fi")


def watch_scan(path):
    """Scan one file with the (warm) daemon. Infected files are moved to
    quarantine by clamdscan --move. Returns ('clean'|'infected'|'error', out)."""
    QUARANTINE_DIR.mkdir(parents=True, exist_ok=True)  # --move requires it to exist
    target = shlex.quote(str(path))
    qdir = shlex.quote(str(QUARANTINE_DIR))
    rc, out = distrobox(
        f"timeout --kill-after=30s 30m clamdscan --multiscan --fdpass "
        f"--move={qdir} {target} 2>&1",
        timeout=2400)
    if "FOUND" in out and "Infected files: 1" in out:
        return "infected", out
    if rc != 0 or "ERROR" in out:
        return "error", out
    return "clean", out


def watch_append_log(line):
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        path = LOG_DIR / "watcher.log"
        with open(path, "a") as f:
            f.write(line + "\n")
        _watch_log_writes[0] += 1
        if _watch_log_writes[0] % 200 == 0:
            _trim_log(path, _WATCH_LOG_MAX)  # never let it grow forever
    except OSError:
        pass


_WATCH_LOG_MAX = 5000          # same ballpark as the daily scan log
_watch_log_writes = [0]        # trim every ~200 appends (cheap amortized)


def _trim_log(path, max_lines):
    """Tail-trim a log file to max_lines (same pattern as the scan scripts)."""
    try:
        with open(path) as f:
            lines = f.readlines()
        if len(lines) > max_lines:
            with open(path, "w") as f:
                f.write("".join(lines[-max_lines:]))
    except OSError:
        pass


def watch_notify(body, critical=False):
    """Desktop notification from the headless service (best-effort)."""
    uid = os.getuid()
    level = "critical" if critical else "low"
    icon = "dialog-error" if critical else "security-high"
    safe = body.replace('"', "'")[:300]
    run(f'DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{uid}/bus '
        f'notify-send -u {level} -i {icon} "Horidoro AV" "{safe}" 2>/dev/null')


def run_watcher_forever(state, extra_folders=(), stop_event=None,
                        progress=None):
    """Headless on-access loop (the --watch CLI / systemd service).

    Holds the engine warm, watches the configured folders, scans new files,
    logs every decision, and notifies on threats. Blocks until stopped;
    returns a summary string."""
    folders = [str(f) for f in (state.get("watcher_folders") or [])]
    folders += [str(f) for f in extra_folders]
    folders = list(dict.fromkeys(folders))  # dedupe, keep order
    folders = [f for f in folders if os.path.isdir(f)]
    if not folders:
        # Regular-people default: no folders configured (e.g. right after a
        # fresh install reset the list) -> protect Downloads anyway. The
        # "watcher on but watches nothing" state was a silent failure.
        dl = str(Path.home() / "Downloads")
        if os.path.isdir(dl):
            folders = [os.path.realpath(dl)]
            try:
                state.set("watcher_folders", folders)  # reflect in the GUI too
            except Exception:  # noqa: BLE001 — best-effort
                pass
            watch_append_log(
                f"{time.strftime('%Y-%m-%d %H:%M:%S')}  no folders "
                f"configured — watching ~/Downloads by default")
        else:
            return "No valid folders to watch — add them in the Watcher tab."
    if progress:
        progress("Starting engine…")
    started = False
    for attempt in range(3):  # transient container hiccups (conmon etc.) retry
        if watch_engine_up():
            started = True
            break
        if attempt < 2:
            time.sleep(5)
    if not started:
        watch_append_log(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  ENGINE-FAIL  "
                         f"could not start the engine after 3 attempts")
        return ("Engine failed to start after 3 attempts — check the Watcher "
                "tab log and that the container is healthy.")

    cache = HashCache(CONFIG_DIR / "cache.db")
    key = db_key()
    app_dirs = [str(d) for d in
                (QUARANTINE_DIR, LOG_DIR, SCRIPT_DIR, TMP_DIR, CONFIG_DIR)]
    excludes = app_dirs + [str(e) for e in (state.get("exclusions") or [])]
    stats = {"scanned": 0, "skipped": 0, "infected": 0, "errors": 0}
    lock = threading.Lock()
    last_error_notify = [0.0]   # throttle error notifications (5 min cooldown)
    # Batch threat notifications: detections within a ~3s window collapse into
    # ONE notification ("N threats quarantined") instead of spamming one per
    # file during a burst.
    pending_infected = []
    pending_lock = threading.Lock()
    batch_timer = [None]

    def flush_infected():
        batch_timer[0] = None
        with pending_lock:
            paths = list(pending_infected)
            pending_infected.clear()
        if not paths:
            return
        if len(paths) == 1:
            watch_notify(f"Threat quarantined: {paths[0]}", critical=True)
        else:
            shown = ", ".join(os.path.basename(p) for p in paths[:5])
            if len(paths) > 5:
                shown += f" (+{len(paths) - 5} more)"
            watch_notify(f"{len(paths)} threats quarantined: {shown}",
                         critical=True)
        play_sound("threat-detected", state=state)

    def queue_infected(path):
        with pending_lock:
            pending_infected.append(path)
        if batch_timer[0] is None:
            batch_timer[0] = threading.Timer(3.0, flush_infected)
            batch_timer[0].daemon = True
            batch_timer[0].start()

    # Bounded queue keeps the event loop responsive: bursts of events are
    # drained into the queue and scanned by a worker, never blocking inotify.
    work_q = queue.Queue(maxsize=5000)

    def scan_one(path):
        if cache.known_clean(path, key) or cache.verify_clean(path, key):
            with lock:
                stats["skipped"] += 1
            return
        result, detail = watch_scan(path)
        if result == "error":
            # The engine may have died mid-watching (container hiccup, conmon
            # crash, etc.). Restart it and retry once before giving up.
            watch_engine_up()
            result, detail = watch_scan(path)
        if result == "clean":
            cache.record_clean(path, key)
        elif result == "infected":
            cache.invalidate(path)
        with lock:
            stats["scanned"] += 1
            if result == "infected":
                stats["infected"] += 1
            elif result == "error":
                stats["errors"] += 1
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        if result == "error":
            reason = next((l.strip() for l in detail.splitlines()
                           if "ERROR" in l.upper()), "") or \
                next((l.strip() for l in detail.splitlines() if l.strip()), "")
            watch_append_log(f"{stamp}  ERROR  {path}  {reason[:160]}")
        else:
            watch_append_log(f"{stamp}  {result.upper():8s}  {path}")
        if result == "infected":
            queue_infected(path)
            record_origin(path)
            _sweep_browser_partials(state, path)
        elif result == "error" and time.time() - last_error_notify[0] > 300:
            last_error_notify[0] = time.time()
            watch_notify("An on-access scan failed — see the Watcher tab "
                         "for the error details")
            play_sound("an-error-occurred", state=state)
        if progress:
            progress(f"scanned={stats['scanned']} "
                     f"skipped={stats['skipped']} "
                     f"infected={stats['infected']}")

    def worker():
        while True:
            item = work_q.get()
            if item is None:
                work_q.task_done()  # the sentinel itself counts — join() needs it
                break
            try:
                scan_one(item)
            except Exception as e:  # noqa: BLE001 — one bad scan never kills the
                # service, but it MUST be logged: the silent-watcher bug was a
                # scan_one exception being swallowed with no log line at all
                try:
                    import traceback as _tb
                    tb = _tb.format_exc().strip().splitlines()[-1]
                    watch_append_log(
                        f"{time.strftime('%Y-%m-%d %H:%M:%S')}  WORKER-ERR  "
                        f"{item}  {type(e).__name__}: {e}  ({tb})")
                except Exception:  # noqa: BLE001 — logging must never crash
                    pass
                with lock:
                    stats["errors"] += 1
            finally:
                work_q.task_done()

    def on_file(path):
        try:
            work_q.put_nowait(path)
        except queue.Full:
            watch_append_log(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  DROPPED   "
                             f"{path} (scan backlog full)")
            with lock:
                stats["errors"] += 1

    threading.Thread(target=worker, daemon=True).start()

    def on_ready(count):
        watch_append_log(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  WATCHING  "
                         f"{count} directories in {len(folders)} folder(s)")
        if progress:
            progress(f"Watching {count} directories in {len(folders)} folder(s)")

    def on_overflow(total):
        watch_append_log(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  OVERFLOW  "
                         f"{total} event burst(s) lost (inotify queue full)")

    watcher = Watcher(folders, on_file, exclude=excludes,
                      on_overflow=on_overflow)
    watcher.start(on_ready=on_ready)
    prev_degraded = [False]

    def _check_degraded():
        """Surface the inotify watch-limit state: log + notify ONCE when it
        trips (silent gaps are the worst failure mode for on-access)."""
        degraded = bool(watcher.degraded)
        if degraded and not prev_degraded[0]:
            prev_degraded[0] = True
            watch_append_log(
                f"{time.strftime('%Y-%m-%d %H:%M:%S')}  WATCH-LIMIT  "
                f"{watcher.degraded}")
            watch_notify("Watch limit reached — some folders may not be "
                         "watched. See the Watcher tab.")
        elif not degraded:
            prev_degraded[0] = False

    stop = stop_event
    if stop is None:
        import signal as _signal
        stop = threading.Event()
        _signal.signal(_signal.SIGTERM, lambda *_: stop.set())
        _signal.signal(_signal.SIGINT, lambda *_: stop.set())
    try:
        while not stop.is_set():
            _check_degraded()
            stop.wait(2.0)
    finally:
        watcher.stop()
        work_q.put(None)          # stop the scan worker
        work_q.join()
        watch_engine_down()
        cache.close()
    return (f"Watcher stopped — scanned={stats['scanned']} "
            f"skipped={stats['skipped']} infected={stats['infected']} "
            f"errors={stats['errors']}")


# ---------------------------------------------------------------------------
# custom signature databases (manage extra detection rules)
# ---------------------------------------------------------------------------
DB_DIR = "/var/lib/clamav"
_DB_EXTS = (".cvd", ".cld", ".ndb", ".hdb", ".ldb", ".mdb", ".gdb",
            ".pdb", ".wdb", ".ftm", ".crb", ".msb", ".zmd", ".fp",
            ".db", ".csv")


def list_dbs():
    """Signature database files loaded by the engine.

    Filters to signature extensions and skips freshclam's state file plus
    distrobox startup chatter."""
    rc, out = distrobox(f"ls -1 {DB_DIR} 2>/dev/null")
    dbs = []
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("Starting container") or "[ OK ]" in line:
            continue
        if line.lower().endswith(_DB_EXTS):
            dbs.append(line)
    return sorted(dbs)


def add_db(path):
    """Copy a signature database file into the engine's database directory.
    Takes effect when the engine next starts."""
    target = shlex.quote(str(path))
    rc, out = distrobox(f"sudo cp {target} {DB_DIR}/ 2>&1")
    if rc != 0:
        raise RuntimeError(f"Could not copy database: {out[-200:]}")
    return True


def remove_db(name):
    """Delete a signature database file (guarded against path tricks)."""
    if "/" in name or name in (".", ".."):
        raise RuntimeError("Invalid database name")
    safe = shlex.quote(name)
    rc, out = distrobox(f"sudo rm -f {DB_DIR}/{safe} 2>&1")
    if rc != 0:
        raise RuntimeError(f"Could not remove database: {out[-200:]}")
    return True


# Well-known community signature feeds (the URLs of the well-known
# community feeds, fetched directly via freshclam's DatabaseCustomURL).
THIRD_PARTY_DBS = [
    "DatabaseCustomURL https://urlhaus.abuse.ch/downloads/urlhaus.ndb",
    "DatabaseCustomURL http://sigs.interserver.net/shell.ldb",
    "DatabaseCustomURL https://ftp.swin.edu.au/sanesecurity/junk.ndb",
    "DatabaseCustomURL https://ftp.swin.edu.au/sanesecurity/blurl.ndb",
    "DatabaseCustomURL https://ftp.swin.edu.au/sanesecurity/sanesecurity.ftm",
    "DatabaseCustomURL https://www.rfxn.com/downloads/rfxn.ndb",
]

# Runs inside the container via `sudo bash <file>` — avoids ALL inline-quoting
# pitfalls. Removes anything a previous attempt may have left behind (the old
# broken ThirdPartyDatabase lines), then appends the feeds once.
_THIRD_PARTY_SCRIPT = r'''#!/bin/bash
# Horidoro AV - add recommended third-party signature feeds (idempotent).
set -e
sed -i '/^ThirdPartyDatabase/d' /etc/freshclam.conf
sed -i '/^DatabaseMirror clamav\.freenet\.gq/d' /etc/freshclam.conf
sed -i '/^DatabaseMirror clamav\.eonetwork\.net/d' /etc/freshclam.conf
sed -i '/^DatabaseMirror db\.local\.clamav\.net/d' /etc/freshclam.conf
if ! grep -q 'DatabaseCustomURL rfxn' /etc/freshclam.conf; then
    cat "$1" >> /etc/freshclam.conf
fi
'''


def add_third_party_dbs(progress=None):
    """One-click: configure freshclam for the recommended community feeds and
    download them. Idempotent and self-cleaning (removes stale entries from
    earlier broken attempts)."""
    if progress:
        progress("Configuring third-party signature feeds…")
    snippet = "\n".join(THIRD_PARTY_DBS) + "\n"
    staged = TMP_DIR / "horidoro-thirdparty.conf"
    staged.write_text(snippet)
    helper = TMP_DIR / "horidoro-thirdparty.sh"
    helper.write_text(_THIRD_PARTY_SCRIPT)
    helper.chmod(0o755)
    rc, out = distrobox(
        f"sudo bash {shlex.quote(str(helper))} {shlex.quote(str(staged))} 2>&1")
    staged.unlink(missing_ok=True)  # cleanup — nothing left behind
    helper.unlink(missing_ok=True)
    if rc != 0:
        raise RuntimeError(f"Could not configure third-party databases: "
                           f"{out[-200:]}")
    if progress:
        progress("Downloading third-party databases (takes a couple of minutes)…")
    rc, out = distrobox("sudo freshclam", timeout=1800)
    if rc != 0:
        raise RuntimeError(f"Database download failed: {out[-300:]}")
    return True


# ---------------------------------------------------------------------------
# notification sounds (bundled clips, Settings toggle, silent fallback)
# ---------------------------------------------------------------------------
def play_sound(name, state=None):
    """Play a bundled notification sound (best-effort; never raises).
    Honors the Settings 'Sound notifications' toggle — read FRESH from the
    state FILE, because long-running services (the watcher) hold a stale
    in-memory State and must pick up a GUI toggle change immediately (the
    user toggled sounds ON but the watcher stayed silent until a restart).
    Missing file or no player -> silent."""
    try:
        if not State().get("sounds_enabled", True):
            return
    except Exception:  # noqa: BLE001
        pass
    path = SOUNDS_DIR / f"{name}.wav"
    if not path.exists():
        return
    try:
        if shutil.which("paplay"):
            run(f"paplay {shlex.quote(str(path))} >/dev/null 2>&1", timeout=15)
        elif shutil.which("canberra-gtk-play"):
            run(f"canberra-gtk-play -f {shlex.quote(str(path))} "
                ">/dev/null 2>&1", timeout=15)
        elif shutil.which("aplay"):
            run(f"aplay -q {shlex.quote(str(path))} >/dev/null 2>&1", timeout=15)
    except Exception:  # noqa: BLE001 — sound is cosmetic
        pass


# ---------------------------------------------------------------------------
# signature key (the watcher uses this to re-scan when definitions change)
# ---------------------------------------------------------------------------
def db_key():
    """Signature database version counter (e.g. '28096'). Changes only when
    freshclam downloads new definitions; cache entries recorded under an older
    key are re-scanned so new signatures re-evaluate old files."""
    ver = db_version()
    if "/" in ver:
        return ver.split("/")[1].strip()
    return ""


# Runs inside the container via `sudo bash <file>` — avoids ALL inline-quoting
# pitfalls. Appends a custom DatabaseCustomURL line if it isn't present yet.
_DB_URL_SCRIPT = r'''#!/bin/bash
# Horidoro AV - subscribe to a custom signature database URL (idempotent).
set -e
URL=$(cat "$1")
if ! grep -qF "$URL" /etc/freshclam.conf; then
    cat "$1" >> /etc/freshclam.conf
fi
'''


def add_db_url(url):
    """Subscribe freshclam to a custom database URL. Unlike 'add file' (a
    one-time copy that goes stale), this updates with every definition
    update — exactly like the recommended feeds."""
    url = str(url).strip()
    if not url.lower().startswith(("http://", "https://")):
        raise RuntimeError("That doesn't look like a URL — it must start with "
                           "http:// or https://")
    staged = TMP_DIR / "horidoro-dburl.conf"
    staged.write_text(f"DatabaseCustomURL {url}\n")
    helper = TMP_DIR / "horidoro-dburl.sh"
    helper.write_text(_DB_URL_SCRIPT)
    helper.chmod(0o755)
    rc, out = distrobox(
        f"sudo bash {shlex.quote(str(helper))} {shlex.quote(str(staged))} 2>&1")
    staged.unlink(missing_ok=True)  # cleanup — nothing left behind
    helper.unlink(missing_ok=True)
    if rc != 0:
        raise RuntimeError(f"Could not configure database URL: {out[-200:]}")
    rc, out = distrobox("sudo freshclam", timeout=1800)
    if rc != 0:
        raise RuntimeError(f"Database download failed: {out[-300:]}")
    return True


# ---------------------------------------------------------------------------
# browser download-partial cleanup (after a quarantine)
# ---------------------------------------------------------------------------
_PARTIAL_SUFFIXES = (".crdownload", ".part", ".partial",
                    ".download", ".opdownload")

_XDG_DIRS = ("Downloads", "Documents", "Pictures", "Desktop",
             "Videos", "Music")


def _sweep_browser_partials(state, quarantined_path):
    """After a quarantine, remove browser partial leftovers in EVERY place
    they can end up, not just where the threat was found. Browsers stage
    downloads in ~/Downloads before the user picks a save location, so the
    broken 'Unconfirmed *.crdownload' artifact can land in any common folder
    (watched or not). Sweeps: the quarantined file's dir, the user's standard
    folders, and every watched folder. The 20s age rule keeps actively-
    downloading files untouched."""
    dirs = {os.path.dirname(quarantined_path)}
    for name in _XDG_DIRS:
        try:
            d = os.path.realpath(os.path.expanduser(f"~/{name}"))
            if os.path.isdir(d):
                dirs.add(d)
        except OSError:
            pass
    for f in (state.get("watcher_folders") or []):
        try:
            d = os.path.realpath(str(f))
            if os.path.isdir(d):
                dirs.add(d)
        except OSError:
            pass
    for d in dirs:
        _clean_stale_partials(d, quarantined_name=quarantined_path)


def _clean_stale_partials(directory, quarantined_name=None, delay_sweep=True):
    """Remove browser download partials left next to a just-quarantined file.

    Two passes:
      1. Exact-match: <quarantined name> + browser partial suffix (e.g.
         eicar.com.crdownload, eicar.com.part) is deleted immediately — the
         final file landing means that partial is done being written.
      2. Generic: Chrome's 'Unconfirmed *.crdownload' leftovers, removed once
         they stop being written (20s). An actively-downloading file keeps a
         fresh mtime, so it is never touched.

    The browser re-creates a fresh 'Unconfirmed *' partial AFTER the quarantine
    (it notices the finished file vanished and treats the download as
    interrupted), so a delayed re-sweep (~30s, daemon thread) is scheduled to
    catch it. Best-effort, never raises.
    """
    try:
        if quarantined_name:
            base = os.path.basename(quarantined_name)
            for suf in _PARTIAL_SUFFIXES:
                p = os.path.join(directory, base + suf)
                try:
                    os.unlink(p)
                    watch_append_log(
                        f"{time.strftime('%Y-%m-%d %H:%M:%S')}  CLEANED   {p}")
                except OSError:
                    pass
        cutoff = time.time() - 20
        for p in glob.glob(os.path.join(directory, "Unconfirmed *.crdownload")):
            try:
                if os.path.getmtime(p) < cutoff:
                    os.unlink(p)
                    watch_append_log(
                        f"{time.strftime('%Y-%m-%d %H:%M:%S')}  CLEANED   {p}")
            except OSError:
                pass
    except OSError:
        pass
    if delay_sweep and quarantined_name:
        def _resweep():
            try:
                _clean_stale_partials(directory, delay_sweep=False)
            except Exception:  # noqa: BLE001 — best-effort cleanup
                pass
        t = threading.Timer(30.0, _resweep)
        t.daemon = True   # never keeps the service alive after stop
        t.start()


# ---------------------------------------------------------------------------
# manual ClamAV engine updates (definitions auto-update; the engine itself
# only changes when the user chooses — a major upgrade could alter behavior)
# ---------------------------------------------------------------------------
def engine_update_status():
    """Check the installed engine version vs what dnf has available.
    Returns {'installed', 'available', 'update'} or None if unreachable."""
    ver = db_version()
    parts = ver.split("/")
    installed = parts[0].strip() if parts else ver
    rc, out = distrobox(
        "sudo dnf check-update -q clamav clamd clamav-update 2>&1", timeout=180)
    if rc not in (0, 100):
        return {"installed": installed, "available": None, "update": None}
    available = None
    if rc == 100:
        for line in out.splitlines():
            parts = line.split()
            if parts and parts[0].split(".")[0] in ("clamav", "clamd", "clamav-update"):
                available = parts[1]
                break
    return {"installed": installed, "available": available, "update": rc == 100}


def update_engine(progress=None):
    """Manually upgrade the ClamAV engine inside the container (user-initiated).
    Returns the new version string."""
    if progress:
        progress("Updating the ClamAV engine… (a few minutes)")
    rc, out = distrobox(
        "sudo dnf upgrade -y clamav clamd clamav-update 2>&1", timeout=1800)
    if rc != 0:
        raise RuntimeError(f"Engine update failed: {out[-300:]}")
    if progress:
        progress("Restarting the engine…")
    distrobox("sudo pkill -9 clamd 2>/dev/null", timeout=60)
    return db_version()


def _wait_for_signal():
    import signal
    event = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: event.set())
    signal.signal(signal.SIGINT, lambda *_: event.set())
    event.wait()


# ---------------------------------------------------------------------------
# bug reports (consent-first, sanitized)
# ---------------------------------------------------------------------------
# Diagnostics are user-typed + log content, i.e. UNTRUSTED data: logs can
# contain filenames/URLs that embed escape sequences or control codes.
# Sanitizing keeps a pasted report clean — no control characters, no markdown
# fences, no redacted paths leaking.
_ANSI_ESC = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_CTRL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_INJECTION_LINE = re.compile(
    r"ignore\s+(all\s+|any\s+|previous\s+)?instructions"
    r"|system\s+prompt|reveal\s+(your|the)\s+(system\s+)?(prompt|instructions)"
    r"|you\s+are\s+(now\s+)?(an?\s+)?(ai|assistant|chatbot|language\s+model)"
    r"|<\|im_(start|end)\|>|developer\s+mode|dev\s*mode"
    r"|###\s*(system|instruction|user)\b", re.IGNORECASE)


def sanitize_report(text, max_line=400, max_total=12000):
    """Clean report text for a plain issue tracker or email.

    - strips ANSI colour/escape sequences and control characters
    - neutralizes backticks / markdown fences (no code-block injection)
    - drops lines that look like instruction-style log noise
    - redacts the user's home path and username (privacy)
    - hard length caps per line and total
    """
    home = str(Path.home())
    user = ""
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001
        pass
    out = []
    for raw in str(text).splitlines():
        line = _ANSI_ESC.sub("", raw)
        line = _CTRL_CHARS.sub("", line)
        line = line.replace("`", "'")
        line = line.rstrip()
        if not line:
            continue
        if _INJECTION_LINE.search(line):
            continue
        if home:
            line = line.replace(home, "~")
        if user:
            line = re.sub(rf"\b{re.escape(user)}\b", "$USER", line)
        if len(line) > max_line:
            line = line[:max_line] + "…"
        out.append(line)
    joined = "\n".join(out).strip("\n")
    if len(joined) > max_total:
        joined = joined[:max_total] + "\n…(truncated)"
    return joined


_REPORT_BANNER = "HORIDORO AV DIAGNOSTIC REPORT"


def build_bug_report(state=None):
    """Assemble a sanitized local diagnostics report.

    Contains only: app version, OS release, engine/DB version, install state,
    a system fingerprint (distrobox/podman versions, rootless status,
    container + engine state), the tail of EVERY app log, and — when an
    install failed — the install diagnostics from
    /tmp/horidoro-install-debug.log. Nothing else — no files, no personal
    data (home path and username are redacted). Line-by-line cleaned:
    control chars, escapes, backticks and instruction-style lines
    stripped."""
    lines = [
        _REPORT_BANNER,
        "",
        f"App: {APP_NAME} v{VERSION}",
        f"Project: {HOMEPAGE}",
    ]
    try:
        with open("/etc/os-release") as f:
            for ln in f.read().splitlines():
                if ln.startswith(("PRETTY_NAME=", "VERSION_ID=", "ID=")):
                    lines.append("OS: " + ln)
    except OSError:
        lines.append("OS: (unknown)")
    try:
        lines.append("Engine/DB: " + db_version())
    except Exception:  # noqa: BLE001 — engine may be unreachable
        lines.append("Engine/DB: (unavailable)")
    if state is not None:
        lines.append("Install state: "
                     + ("installed" if state.is_installed() else "not installed"))
        timers = ", ".join(state.get("timers_enabled") or []) or "none"
        lines.append(f"Timers: {timers}")
        lines.append("Watcher: "
                     + ("on" if state.get("watcher_enabled") else "off"))
    # system fingerprint — the distrobox/podman/container facts that
    # diagnosed the Aurora install bug; needed for engine/watcher/update
    # issues too, and cheap/read-only
    lines.append("System:")
    for cmd in ("distrobox --version 2>/dev/null",
                "podman --version 2>/dev/null"):
        try:
            rc, out = run(cmd, timeout=20)
            ver = (out.strip().splitlines() or ["?"])[-1]
            lines.append("  " + (ver or "?"))
        except Exception:  # noqa: BLE001
            pass
    try:
        rc, out = run("podman info 2>/dev/null | grep -i 'rootless:' | head -1",
                      timeout=20)
        lines.append("  podman: " + (out.strip() or "?"))
    except Exception:  # noqa: BLE001
        pass
    try:
        lines.append("  container: "
                     + ("present" if in_container() else "missing"))
        lines.append("  engine: " + engine_status())
    except Exception:  # noqa: BLE001
        pass
    lines.append("")
    lines.append("--- log tails (last entries) ---")
    # every log the app writes: scan schedules, manual/right-click scans,
    # the watcher, and definition updates — the report should cover all of
    # them, not just the two most common
    for name, keep in (("watcher.log", 2500), ("daily_scan.log", 2500),
                       ("manual_scan.log", 1000),
                       ("monthly_full_scan.log", 1000),
                       ("right_click_scan.log", 1000),
                       ("update_history.log", 1000)):
        tail = read_log(name)
        if tail and tail != "(no log yet)":
            lines.append(f"[{name}]")
            lines.append(tail[-keep:])
    # install diagnostics — written to /tmp by a FAILED install (survives the
    # automatic rollback); exactly the data needed to fix install bugs
    debug_log = Path("/tmp/horidoro-install-debug.log")
    if debug_log.exists():
        try:
            dbg = debug_log.read_text(errors="replace").strip()
        except OSError:
            dbg = ""
        if dbg:
            lines.append("")
            lines.append("--- install diagnostics (last failed install) ---")
            lines.append(dbg[-4000:])
    lines.append("")
    lines.append("HORIDORO AV DIAGNOSTIC REPORT END")
    return sanitize_report("\n".join(lines))
