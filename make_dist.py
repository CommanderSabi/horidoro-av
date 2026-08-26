#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Horidoro AV — build the shareable "give to a non-technical friend" package.

Creates dist/ containing:
  - horidoro-av.py          the fresh single-file app
  - Install Horidoro AV.sh  double-clickable one-time setup (copies the app
                           into ~/.local/bin and opens it; the app registers
                           its own apps-menu entry on first run)
  - README-FIRST.txt       picture-simple instructions
  - LICENSE                the MIT license (Copyright (c) 2026 Commander Sabi)

then zips all four into Horidoro-AV-for-You.zip (exec bits preserved).

Usage:  python3 build.py && python3 make_dist.py
"""

import os
import shutil
import zipfile
from pathlib import Path

ROOT = Path(__file__).parent
DIST = ROOT / "dist"
ZIP = ROOT / "Horidoro-AV-for-You.zip"
BUNDLE = ROOT / "horidoro-av.py"

SETUP_SH = r"""#!/bin/bash
# Horidoro AV — one-time setup (made for people who just want it to work).
set -e
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:$PATH"
cd "$(dirname "$0")"

LOGFILE="$HOME/.local/share/horidoro-av/setup.log"
mkdir -p "$HOME/.local/share/horidoro-av"

# Fail LOUDLY: show a visible error box (when possible) AND log it, so a
# launched-without-terminal run can never silently "do nothing".
die() {
    echo "Horidoro AV setup problem: $1" | tee -a "$LOGFILE"
    if command -v zenity >/dev/null 2>&1; then
        zenity --error --title "Horidoro AV" --text "$1" 2>/dev/null || true
    elif command -v kdialog >/dev/null 2>&1; then
        kdialog --error "$1" 2>/dev/null || true
    fi
    read -r -p "Press Enter to close." 2>/dev/null || true
    exit 1
}

echo "Horidoro AV — setting up..." | tee -a "$LOGFILE"

command -v python3 >/dev/null 2>&1 || die "Python 3 is not installed on this computer. Please ask whoever gave you this file for help."
python3 -c "import gi; gi.require_version('Gtk','3.0'); from gi.repository import Gtk" 2>/dev/null || die "This computer is missing a small part that Horidoro AV needs (GTK3). Please ask whoever gave you this file for help."
command -v distrobox >/dev/null 2>&1 || die "This computer is missing 'distrobox' (the engine runs inside a container). Install it first, e.g.  sudo apt install distrobox"

mkdir -p "$HOME/.local/bin"
cp horidoro-av.py "$HOME/.local/bin/horidoro-av" || die "Could not copy the app file."
chmod +x "$HOME/.local/bin/horidoro-av"

echo "Done! Opening Horidoro AV..." | tee -a "$LOGFILE"
sleep 1
exec "$HOME/.local/bin/horidoro-av" || die "Could not start Horidoro AV."
"""

README_FIRST = """\
WELCOME TO HORIDORO AV!
======================

Horidoro AV is a FREE antivirus for your computer.
It works completely on YOUR computer — nothing is sent anywhere.
No account, no sign-up, no cloud.

------------------------------------------------------------------
HOW TO INSTALL (takes about 5-10 minutes, do it once)
------------------------------------------------------------------

STEP 1 — Put these 3 files somewhere easy to find (like Downloads):

    - horidoro-av.py
    - Install Horidoro AV.sh
    - README-FIRST.txt      (this file)

STEP 2 — Double-click  "Install Horidoro AV.sh"

    A window may pop up asking what to do with the file.
    The button wording depends on your computer — it may say
    "Launch", "Run", or "Run in Terminal". Whichever one you
    see, choose it. (Some computers show a terminal window that
    closes quickly — that's normal.)

    The Horidoro AV window then opens by itself. If it doesn't
    open, look for it in your apps menu — it's now installed there.

    TROUBLE? If double-clicking doesn't start it at all: right-click
    "Install Horidoro AV.sh" -> Properties -> Permissions -> tick
    "Is executable", then double-click again.

STEP 3 — In the Horidoro AV window, click the  "Install"  button.

    - It asks "Install Horidoro AV?" — click  Yes.
    - Wait while it works (first time it downloads the antivirus
      definitions, which can take a few minutes). Don't close it.
    - When it says it's done, you're protected!

------------------------------------------------------------------
AFTER INSTALL — what happens automatically
------------------------------------------------------------------

    - Horidoro AV is in your apps menu. Open it anytime.
    - You choose what gets scanned and when: Settings -> Scan targets
      (one-click Quick add), then Schedule -> turn on daily and monthly
      scans.
    - Right-click any file or folder -> "Scan with Horidoro AV".
    - If something dangerous is found, it's put in quarantine
      and you'll get a notification.

------------------------------------------------------------------
IF SOMETHING LOOKS WRONG
------------------------------------------------------------------

    - Scary warning about an unknown file? It's just the computer
      being careful. Choose "Run" (or "Run in Terminal") anyway.
    - The app says something about "GTK3"? Tell the person who gave
      you this file.
    - You can uninstall everything later from the Settings tab
      (Uninstall button) — nothing is permanent. Check the box in that
      window if you also want to permanently delete the logs and any
      quarantined files.

------------------------------------------------------------------
PLEASE READ (important)
------------------------------------------------------------------

Horidoro AV is free software, provided "as is", with no warranty.
No antivirus catches everything — it helps protect your computer,
but please also keep your system updated and stay careful online.
You use it at your own risk.  Engine: ClamAV (open-source CLI backend, GPLv2).

Thank you for using Horidoro AV!

Horidoro AV is released under the MIT License (see the LICENSE file in
this folder). Copyright (c) 2026 Commander Sabi. Engine: ClamAV (GPLv2).
"""


def _zip_add(zf, path, arcname, mode):
    info = zipfile.ZipInfo.from_file(path, arcname)
    info.external_attr = (mode & 0xFFFF) << 16  # Unix permissions in the zip
    with open(path, "rb") as f:
        zf.writestr(info, f.read())


def main():
    if not BUNDLE.exists():
        raise SystemExit("horidoro-av.py missing — run python3 build.py first")
    if DIST.exists():
        shutil.rmtree(DIST)  # clean rebuild, nothing stale
    DIST.mkdir(parents=True)

    shutil.copy2(BUNDLE, DIST / "horidoro-av.py")
    (DIST / "horidoro-av.py").chmod(0o755)

    setup = DIST / "Install Horidoro AV.sh"
    setup.write_text(SETUP_SH)
    setup.chmod(0o755)

    (DIST / "README-FIRST.txt").write_text(README_FIRST)
    shutil.copy2(ROOT / "LICENSE", DIST / "LICENSE")

    if ZIP.exists():
        ZIP.unlink()
    with zipfile.ZipFile(ZIP, "w", zipfile.ZIP_DEFLATED) as zf:
        _zip_add(zf, DIST / "horidoro-av.py", "horidoro-av.py", 0o755)
        _zip_add(zf, setup, "Install Horidoro AV.sh", 0o755)
        _zip_add(zf, DIST / "README-FIRST.txt", "README-FIRST.txt", 0o644)
        _zip_add(zf, DIST / "LICENSE", "LICENSE", 0o644)
    print(f"dist/ ready + {ZIP.name} "
          f"({ZIP.stat().st_size / 1024:.1f} KB, {os.path.basename(ZIP)})")


if __name__ == "__main__":
    main()
