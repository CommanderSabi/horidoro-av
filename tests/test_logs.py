#!/usr/bin/env python3
"""Horidoro AV — log retention test (watcher.log must never grow forever).

The watcher log is tail-trimmed to _WATCH_LOG_MAX lines, same pattern as the
scan scripts. This guards the "infinite log" class the user asked about.

Run:  python3 tests/test_logs.py
"""
import os
import sys
import tempfile

os.environ["HOME"] = tempfile.mkdtemp(prefix="horidoro-logs-")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "horidoro"))

import actions  # noqa: E402

failures = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" — {detail}" if detail else ""))
    if not cond:
        failures.append(name)


path = actions.LOG_DIR / "watcher.log"
actions.LOG_DIR.mkdir(parents=True, exist_ok=True)
path.write_text("\n".join(f"line {i}" for i in range(6000)) + "\n")

actions._trim_log(path, actions._WATCH_LOG_MAX)
lines = path.read_text().splitlines()
check("trim: 6000 -> 5000 lines", len(lines) == 5000, str(len(lines)))
check("trim: keeps the TAIL", lines[0] == "line 1000", lines[0])

actions.watch_append_log("new entry")
check("append still works after trim",
      path.read_text().strip().endswith("new entry"))

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("ALL LOG TESTS PASSED")
