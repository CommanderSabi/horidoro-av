#!/usr/bin/env python3
"""Horidoro AV — bundle runtime smoke test.

Guards the critical property: `python3 horidoro-av.py` actually RUNS (the
top-level `main()` must resolve — a regression here made the single-file
build die with NameError immediately). Runs the bundle in a sandboxed HOME
with no display available, so the GUI can't open; we only verify it boots
past startup and fails gracefully at the display stage.

Run:  python3 tests/test_bundle.py   (after python3 build.py)
"""
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUNDLE = os.path.join(ROOT, "horidoro-av.py")

failures = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" — {detail}" if detail else ""))
    if not cond:
        failures.append(name)


sandbox = tempfile.mkdtemp(prefix="horidoro-bundle-")
env = dict(os.environ)
env["HOME"] = sandbox
env["DISPLAY"] = ""
env["WAYLAND_DISPLAY"] = ""

try:
    p = subprocess.run([sys.executable, BUNDLE], env=env, capture_output=True,
                       text=True, timeout=25)
    out = (p.stdout or "") + (p.stderr or "")
    check("bundle: no NameError (main() resolves)", "NameError" not in out,
          out[:160])
    check("bundle: booted to the display stage",
          p.returncode == 0 or "cannot open display" in out,
          f"rc={p.returncode}, {out[:160]!r}")
    print(f"  (exit {p.returncode}, output: {out.strip()[:100]!r})")
except subprocess.TimeoutExpired:
    check("bundle: did not hang at startup", False, "timeout after 25s")

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("BUNDLE SMOKE TEST PASSED")
