#!/usr/bin/env python3
"""Horidoro AV — bundled sound sanity test.

The four notification clips must be embedded (they ship with everyone),
decode to valid WAV data, and play_sound() must never raise (toggle off,
missing file, missing player -> silent).

Run:  python3 tests/test_sounds.py
"""
import base64
import os
import shutil
import sys
import tempfile
from pathlib import Path

os.environ["HOME"] = tempfile.mkdtemp(prefix="horidoro-sounds-")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "horidoro"))

import actions  # noqa: E402
import templates  # noqa: E402

failures = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" — {detail}" if detail else ""))
    if not cond:
        failures.append(name)


expected = {"scan-complete", "threat-detected", "update-complete",
            "an-error-occurred"}
check("sounds: all four clips embedded",
      expected == set(templates.SOUND_FILES),
      str(sorted(templates.SOUND_FILES)))
for name, b64 in templates.SOUND_FILES.items():
    raw = base64.b64decode(b64)
    check(f"sounds: {name} decodes to a WAV",
          raw[:4] == b"RIFF" and raw[8:12] == b"WAVE",
          f"{len(raw)} bytes")

# play_sound must be a safe no-op in every failure mode
class ToggleOff:
    def get(self, _k, _d=None):
        return False


class ToggleOn:
    def get(self, _k, _d=None):
        return True


actions.play_sound("scan-complete", state=ToggleOff())   # no sounds dir -> silent
actions.SOUNDS_DIR = Path(tempfile.gettempdir()) / "horidoro-no-sounds"
actions.play_sound("scan-complete", state=None)          # no sounds dir -> silent
orig_which = shutil.which
shutil.which = lambda _cmd: None                          # no audio player
try:
    actions.play_sound("scan-complete", state=ToggleOn())
finally:
    shutil.which = orig_which
check("sounds: play_sound never raises (off / missing / no player)", True)

# THE WATCHER-SOUND REGRESSION: the toggle is read FRESH from the state FILE,
# so a long-running service (watcher) honors a GUI toggle change immediately
# instead of keeping a stale in-memory copy (user: sounds ON, watcher silent).
from state import State  # noqa: E402

st = State()
st.set("sounds_enabled", False)
actions.SOUNDS_DIR.mkdir(parents=True, exist_ok=True)
(actions.SOUNDS_DIR / "scan-complete.wav").write_bytes(b"RIFFxxxxWAVE")
calls = []
orig_run = actions.run


def fake_which(cmd):
    return "/usr/bin/paplay" if cmd == "paplay" else None


def fake_run(cmd, **kw):
    calls.append(cmd)
    return (0, "")


shutil.which = fake_which
actions.run = fake_run
# file says OFF -> must stay silent even though the passed state says ON
# (this is exactly the watcher's stale-state scenario)
actions.play_sound("scan-complete", state=ToggleOn())
check("sounds: toggle read fresh from the file (off in file -> silent)",
      len(calls) == 0, f"{len(calls)} player call(s)")
# file says ON -> must play even though the passed state says OFF
st.set("sounds_enabled", True)
actions.play_sound("scan-complete", state=ToggleOff())
check("sounds: toggle read fresh from the file (on in file -> plays)",
      len(calls) == 1, f"{len(calls)} player call(s)")
actions.run = orig_run
shutil.which = orig_which

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("ALL SOUND TESTS PASSED")
