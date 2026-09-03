#!/usr/bin/env python3
"""Horidoro AV — restore-to-origin test.

Quarantined files record where they came from (origin.json); restore can put
them back exactly, and entries are pruned on restore/delete. The scan scripts
record best-effort origins into origins.tmp which the app merges.

Run:  python3 tests/test_origin.py
"""
import os
import sys
import tempfile

os.environ["HOME"] = tempfile.mkdtemp(prefix="horidoro-origin-")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "horidoro"))

import actions  # noqa: E402

failures = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" — {detail}" if detail else ""))
    if not cond:
        failures.append(name)


origin_dir = os.path.join(os.environ["HOME"], "Documents")
os.makedirs(origin_dir)
original = os.path.join(origin_dir, "false-positive.exe")
open(original, "w").write("MZ-not-malware")

# 1. a quarantine happens: the file is moved into quarantine, and the ORIGINAL
# path is recorded (the watcher records it before/at the move — the string is
# the origin, even once the file no longer sits there)
qfile = actions.QUARANTINE_DIR / "false-positive.exe"
actions.QUARANTINE_DIR.mkdir(parents=True, exist_ok=True)
actions.record_origin(original)
os.rename(original, qfile)
check("origin recorded", actions.get_origin("false-positive.exe") == original,
      str(actions.get_origin("false-positive.exe")))

# 2. restore-to-origin puts it back exactly and prunes the entry
restored = actions.restore_quarantined_to_origin("false-positive.exe")
check("restore-to-origin returns the original path", restored == original,
      str(restored))
check("file is back in its original folder", os.path.exists(original))
check("quarantine entry gone", not qfile.exists())
check("origin entry pruned after restore",
      actions.get_origin("false-positive.exe") is None)

# 3. delete prunes the origin entry too
os.rename(original, qfile)
actions.record_origin(qfile)
actions.delete_quarantined("false-positive.exe")
check("origin entry pruned after delete",
      actions.get_origin("false-positive.exe") is None)

# 4. restore via the folder-chooser path also prunes
open(original, "w").write("MZ-not-malware")  # recreate after the delete test
os.rename(original, qfile)
actions.record_origin(qfile)
pick = os.path.join(os.environ["HOME"], "Downloads")
os.makedirs(pick)
actions.restore_quarantined("false-positive.exe", pick)
check("chooser restore works", os.path.exists(os.path.join(pick, "false-positive.exe")))
check("origin entry pruned after chooser restore",
      actions.get_origin("false-positive.exe") is None)

# 4b. restore NEVER overwrites — if a file already sits at the destination
# (e.g. Steam re-downloaded it), the quarantined copy stays put
open(original, "w").write("MZ-original-again")
os.rename(original, qfile)
actions.record_origin("/home/user/Downloads/false-positive.exe")
occupied = os.path.join(pick, "false-positive.exe")  # exists from test 4
ok = actions.restore_quarantined("false-positive.exe", pick)
check("chooser restore refuses to overwrite an existing file", ok is False)
check("quarantined copy kept when the target exists", qfile.exists())
check("existing file untouched by a refused restore",
      os.path.exists(occupied))
# and the origin is not pruned on a refused restore (nothing was restored)
check("origin kept after a refused restore",
      actions.get_origin("false-positive.exe")
      == "/home/user/Downloads/false-positive.exe")

# 5. scripts' best-effort origins (origins.tmp) get merged
actions.TMP_DIR.mkdir(parents=True, exist_ok=True)
(actions.TMP_DIR / "origins.tmp").write_text(
    "evil.exe|/home/user/Downloads/evil.exe\n")
check("script origin merged", actions.get_origin("evil.exe")
      == "/home/user/Downloads/evil.exe",
      str(actions.get_origin("evil.exe")))
check("origins.tmp cleared after merge",
      not (actions.TMP_DIR / "origins.tmp").exists())

# 5b. merged lines carry WHO caught the file (daily scan / watcher / …);
# legacy 2-field lines merge with no source
(actions.QUARANTINE_DIR / "a.exe").write_bytes(b"MZ-a")
(actions.QUARANTINE_DIR / "b.exe").write_bytes(b"MZ-b")
(actions.TMP_DIR / "origins.tmp").write_text(
    "a.exe|/home/user/Downloads/a.exe|watcher\n"
    "b.exe|/home/user/Pictures/b.exe\n")
check("3-field line records the source", actions.get_source("a.exe")
      == "watcher", str(actions.get_source("a.exe")))
check("legacy 2-field line merges with no source",
      actions.get_source("b.exe") is None
      and actions.get_origin("b.exe") == "/home/user/Pictures/b.exe")
check("quarantine list carries the source",
      any(e["name"] == "a.exe" and e.get("source") == "watcher"
          for e in actions.list_quarantine()))
(actions.TMP_DIR / "origins.tmp").unlink(missing_ok=True)

# 5c. record_origin stamps the source too
actions.record_origin("/home/user/Downloads/c.exe", by="manual scan")
check("record_origin stores the source",
      actions.get_source("c.exe") == "manual scan")

# 5d. legacy plain-string origin.json entries survive (normalized on load)
import json as _json  # noqa: E402
(actions.QUARANTINE_DIR / "origin.json").write_text(
    _json.dumps({"old.exe": "/home/user/Old/old.exe"}))
check("legacy string origin normalized",
      actions.get_origin("old.exe") == "/home/user/Old/old.exe"
      and actions.get_source("old.exe") is None)

# 6. VERBOSE scans record origins from the clamscan log's FOUND lines
# (the scan scripts record origins themselves; the verbose manual-scan path
# runs a direct clamscan, so the app records them after — restore-to-origin
# must work from the Verbose button too)
log = actions.LOG_DIR / "manual_scan.log"
log.parent.mkdir(parents=True, exist_ok=True)
q = actions.QUARANTINE_DIR / "verbose-hit.exe"
q.write_bytes(b"MZ-verbose-hit")
log.write_text(
    "/home/user/Downloads/verbose-hit.exe: Heuristic.Test FOUND\n"
    "/home/user/Downloads/clean-file.bin: OK\n"
    "/home/user/Downloads/not-quarantined.exe: Heuristic.Test FOUND\n")
actions._record_origins_from_log(log)
check("verbose scan records the quarantined file's origin",
      actions.get_origin("verbose-hit.exe")
      == "/home/user/Downloads/verbose-hit.exe")
check("verbose scan ignores OK lines and non-quarantined FOUNDs",
      actions.get_origin("clean-file.bin") is None
      and actions.get_origin("not-quarantined.exe") is None)
log.unlink(missing_ok=True)

# 7. the app's own origin.json must never appear as a quarantined item
# (the user would see it in the Quarantine tab and try to restore/delete it)
q = actions.QUARANTINE_DIR / "false-positive.exe"
q.write_bytes(b"MZ")
actions.record_origin("/home/user/Downloads/false-positive.exe")
names = [e["name"] for e in actions.list_quarantine()]
check("origin.json is hidden from the quarantine list",
      "origin.json" not in names and "false-positive.exe" in names)
q.unlink(missing_ok=True)
check("origin record still works for restore",
      actions.get_origin("false-positive.exe")
      == "/home/user/Downloads/false-positive.exe")
(actions.QUARANTINE_DIR / "origin.json").unlink(missing_ok=True)

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("ALL ORIGIN TESTS PASSED")
