# -*- coding: utf-8 -*-
"""Horidoro AV — branding constants and global paths.

Everything user-facing about identity lives here so it can change in one place.
"""

import os
from pathlib import Path

APP_NAME = "Horidoro AV"
APP_SHORT = "Horidoro"
AUTHOR = "Commander Sabi"
VERSION = "0.1.1"
TAGLINE = "Local-first protection. Commander-approved."
ENGINE_CREDIT = "ClamAV (open-source CLI backend, GPLv2)"
HOMEPAGE = "https://github.com/CommanderSabi/horidoro-av"

# Support / donations (About tab "Support the project")
PATREON_URL = "https://www.patreon.com/cw/CommanderSabi"
USDC_ADDRESS = "2stgEZfERUMs7naTjikdR4CkDUnU1gsYKSHCX4VH5DQt"  # USDC on Solana only

# In-app changelog (About → "What's new"; newest first; reused for Releases)
CHANGELOG = [
    ("v0.1.1", [
        "Fixed: nightly daily/monthly scans could fail when scan paths "
        "contained spaces or special characters (a quoting bug in the "
        "generated scan scripts).",
        "Fixed: scan scripts now work correctly even when no scan targets "
        "are set (the 'skipped' notice actually appears).",
        "New: Stop a running scan — the Scan tab button cancels a stuck or "
        "long scan (scheduled or manual) and releases the scan lock, so a "
        "crashed scan can never leave 'another scan is running' behind.",
        "Fixed: Restore now works properly — across drives, with multiple "
        "files (each back to its own original location), and the dialog "
        "shows exactly where each file will go. If a file with the same "
        "name already exists where you're restoring, Horidoro asks whether "
        "to replace it — nothing is overwritten without asking.",
        "Fixed: files caught by folder scans now always remember their "
        "original location, so Restore can put them back automatically.",
        "New: the Quarantine list shows what caught each file (daily/monthly "
        "scan, watcher, right-click or manual scan).",
        "New: after restoring, Horidoro asks whether to add the restored "
        "locations to Exclusions — nothing is added without asking.",
        "Fixed: errors are never silent — a failure shows a clear message, "
        "and the built-in bug report includes the relevant diagnostics.",
        "Fixed: Restore now records where each file was actually caught — a "
        "stale record from an older scan can no longer point a restore at "
        "the wrong folder.",
        "New: only one Horidoro window can be open at a time — duplicate "
        "windows can no longer interfere with the on-access watcher.",
    ]),
    ("v0.1.0", [
        "First release of Horidoro AV — free, open-source, local-first "
        "antivirus for Linux.",
        "On-access protection with smart caching (unchanged files are never "
        "rescanned).",
        "Scheduled, manual and right-click scanning; full quarantine with "
        "restore-to-origin.",
        "Automatic app updates (checksum-verified, with a sound when done) — "
        "toggle in Settings → App updates, default ON.",
        "Bundled notification sounds (Settings → Sound notifications).",
        "Report a bug from the About tab — consent-first, with system info "
        "and app logs attached.",
    ]),
]


def latest_changelog():
    """Markup text for the About tab: the newest version's notes."""
    ver, notes = CHANGELOG[0]
    return f"<b>{ver}</b>\n" + "\n".join("• " + n for n in notes)

# Provenance marker (LisMyAngel) — never shown in the UI.
_FINGERPRINT = "LisMyAngel · Fight the Fairies"

# ClamAV hard technical limit: files larger than 2 GB are never scannable.
# The watcher and cache use this to skip such files without wasting work.
MAX_SCAN_BYTES = 2 * 1024**3

# Per-user config/data locations (never system-wide; all reversible/uninstallable)
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "horidoro-av"
STATE_FILE = CONFIG_DIR / "state.json"
DOCS_DIR = Path.home() / ".local/share/horidoro"  # scripts, logs, quarantine, sounds

# The engine container (everything antivirus-related runs inside this distrobox)
CONTAINER_NAME = "clamav"
CONTAINER_IMAGE = "registry.fedoraproject.org/fedora:latest"
PACKAGES = ["clamav", "clamd", "clamav-update", "sudo"]
