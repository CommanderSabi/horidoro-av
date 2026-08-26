#!/usr/bin/env python3
"""Horidoro AV — headless watcher pipeline test (run_watcher_forever).

The engine/scan/notify functions are mocked so nothing touches a container;
the real Watcher (inotify), HashCache, worker queue, cleanup, and logging
all run. Covers the user's exact scenario: a Chrome-style EICAR download
(partial -> rename -> detected -> quarantined) must leave NOTHING behind.

Run:  python3 tests/test_headless.py
"""
import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "horidoro"))

SANDBOX = tempfile.mkdtemp(prefix="horidoro-headless-")
os.environ["HOME"] = SANDBOX

from state import State  # noqa: E402
import actions  # noqa: E402
from branding import CONFIG_DIR  # noqa: E402

failures = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" — {detail}" if detail else ""))
    if not cond:
        failures.append(name)


# ---- mocks (no container, no systemd, no notifications) --------------------
scanned = []


def fake_engine_up():
    return True


def fake_engine_down():
    pass


def fake_scan(path):
    scanned.append(path)
    name = os.path.basename(path).lower()
    if "evil" in name or "eicar" in name:
        # simulate clamdscan moving the file to quarantine
        os.remove(path)
        return "infected", "FOUND\nInfected files: 1"
    return "clean", "Infected files: 0"


log_lines = []
notifications = []


def fake_append_log(line):
    log_lines.append(line)


def fake_notify(body, critical=False):
    notifications.append((body, critical))


actions.watch_engine_up = fake_engine_up
actions.watch_engine_down = fake_engine_down
actions.watch_scan = fake_scan
actions.watch_append_log = fake_append_log
actions.watch_notify = fake_notify

# ---- run the real watcher pipeline -----------------------------------------
watch_dir = os.path.join(SANDBOX, "Downloads")
os.makedirs(watch_dir)
st = State()
st.set("watcher_folders", [watch_dir])

stop = threading.Event()
progress_msgs = []
result_holder = {}


def run_in_thread():
    result_holder["summary"] = actions.run_watcher_forever(
        st, stop_event=stop, progress=progress_msgs.append)


t = threading.Thread(target=run_in_thread, daemon=True)
t.start()
time.sleep(1.0)  # engine up + initial tree walk

# ---- Phase 1: a clean file + an infected file -------------------------------
open(os.path.join(watch_dir, "notes.txt"), "w").write("nothing to see")
open(os.path.join(watch_dir, "evil.exe"), "w").write("MZ-bad-bad")

deadline = time.time() + 8
while time.time() < deadline and len(scanned) < 2:
    time.sleep(0.2)
phase1_scanned = list(scanned)

# rewrite the clean file with IDENTICAL content — the cache's hash check
# (verify_clean) recognizes it and skips a rescan
open(os.path.join(watch_dir, "notes.txt"), "w").write("nothing to see")
time.sleep(3.0)

# ---- Phase 2: Chrome-style EICAR download — nothing left behind -------------
# Chrome writes eicar.com.crdownload while downloading, then renames it to
# eicar.com when finished (IN_MOVED_TO). The watcher scans the finished file
# and quarantines it. Chrome then RE-CREATES a fresh 'Unconfirmed *.crdownload'
# when it notices the finished file vanished — that is the "broken leftover".
partial = os.path.join(watch_dir, "eicar.com.crdownload")
open(partial, "w").write(
    "X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*")
final = os.path.join(watch_dir, "eicar.com")
os.rename(partial, final)   # download completes

# the partial itself must NEVER be scanned; the finished file must be
deadline = time.time() + 8
while time.time() < deadline and not any(
        s.endswith("eicar.com") for s in scanned):
    time.sleep(0.2)
time.sleep(1.0)  # quarantine + immediate cleanup pass settle

check("eicar: partial was never scanned",
      not any("crdownload" in s for s in scanned), str(scanned))
check("eicar: finished file WAS scanned",
      any(s.endswith("eicar.com") for s in scanned), str(scanned))
check("eicar: infected file removed (quarantined)",
      not os.path.exists(final))
# browser reaction: a fresh leftover appears (untouched, mtime = now)
leftover = os.path.join(watch_dir, "Unconfirmed 7.crdownload")
open(leftover, "w").write("leftover from interrupted download")
check("eicar: fresh leftover NOT deleted while active-looking",
      os.path.exists(leftover))
# the delayed re-sweep (~30s) removes it once it stops being written
deadline = time.time() + 45
while time.time() < deadline and os.path.exists(leftover):
    time.sleep(1.0)
check("eicar: browser leftover removed by re-sweep",
      not os.path.exists(leftover), "still present: " + leftover)
check("eicar: CLEANED logged",
      any("CLEANED" in l and "Unconfirmed" in l for l in log_lines),
      str([l for l in log_lines if "CLEANED" in l][-2:]))
check("eicar: threat notified", any("eicar.com" in b for b, _ in notifications),
      str(notifications[-2:]))

# ---- Phase 3: direct cleanup unit checks ------------------------------------
u = tempfile.mkdtemp(prefix="horidoro-partial-")
em = os.path.join(u, "eicar.com.crdownload")
open(em, "w").write("x")
ff = os.path.join(u, "eicar.com.part")
open(ff, "w").write("x")
fresh = os.path.join(u, "Unconfirmed 1.crdownload")
open(fresh, "w").write("active")
now = time.time()
os.utime(fresh, (now, now))
stale = os.path.join(u, "Unconfirmed 2.crdownload")
open(stale, "w").write("old")
os.utime(stale, (now - 120, now - 120))
actions._clean_stale_partials(u, quarantined_name=os.path.join(u, "eicar.com"),
                              delay_sweep=False)
check("unit: exact-match .crdownload removed immediately", not os.path.exists(em))
check("unit: exact-match .part removed immediately", not os.path.exists(ff))
check("unit: fresh Unconfirmed kept (active download)", os.path.exists(fresh))
check("unit: stale Unconfirmed removed", not os.path.exists(stale))

# ---- Phase 4: the multi-place sweep (browser leftovers anywhere) -----------
# Browsers stage downloads in ~/Downloads even when the user saves elsewhere,
# so the broken fragment can land in a NON-watched common folder. The sweep
# must cover ~/Downloads + watched folders, not just the quarantined file's dir.
dl = os.path.join(SANDBOX, "Downloads")
os.makedirs(dl, exist_ok=True)
old_leftover = os.path.join(dl, "Unconfirmed 9.crdownload")
open(old_leftover, "w").write("x")
os.utime(old_leftover, (time.time() - 120, time.time() - 120))
fresh_leftover = os.path.join(dl, "Unconfirmed 10.crdownload")
open(fresh_leftover, "w").write("y")  # fresh mtime = looks active, must be kept
st2 = State()
st2.set("watcher_folders", [watch_dir])
actions._sweep_browser_partials(st2, os.path.join(watch_dir, "eicar.com"))
check("sweep: stale leftover in ~/Downloads removed",
      not os.path.exists(old_leftover))
check("sweep: fresh leftover in ~/Downloads kept (active download)",
      os.path.exists(fresh_leftover))

# ---- stop -------------------------------------------------------------------
stop.set()
t.join(timeout=10)
summary = result_holder.get("summary", "(none)")

check("headless: both phase-1 files scanned exactly once",
      len(phase1_scanned) == 2, str(phase1_scanned))
check("headless: infected file detected + quarantined (removed by --move)",
      not os.path.exists(os.path.join(watch_dir, "evil.exe")))
check("headless: cache persisted (DB exists)",
      os.path.exists(CONFIG_DIR / "cache.db"))
check("headless: cache recorded the clean file", (
    lambda: __import__("watcher").HashCache(CONFIG_DIR / "cache.db").known_clean(
        os.path.join(watch_dir, "notes.txt")))())
check("headless: threat notification sent", any(
    "evil.exe" in b and critical for b, critical in notifications), str(notifications))
check("headless: log has scan lines", any(
    "CLEAN" in l and "notes.txt" in l for l in log_lines)
    and any("INFECTED" in l and "evil.exe" in l for l in log_lines), str(log_lines[:4]))
check("headless: log has WATCHING line", any("WATCHING" in l for l in log_lines))
check("headless: progress reported", len(progress_msgs) >= 1, str(progress_msgs[:2]))
check("headless: clean summary returned", "scanned=3" in summary
      and "infected=2" in summary and "skipped=1" in summary, summary)
check("headless: infected file removed, clean file kept",
      not os.path.exists(os.path.join(watch_dir, "evil.exe"))
      and os.path.exists(os.path.join(watch_dir, "notes.txt")))

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("ALL HEADLESS TESTS PASSED")
