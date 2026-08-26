# -*- coding: utf-8 -*-
"""Horidoro AV — branding constants and global paths.

Everything user-facing about identity lives here so it can change in one place.
"""

import os
from pathlib import Path

APP_NAME = "Horidoro AV"
APP_SHORT = "Horidoro"
AUTHOR = "Commander Sabi"
VERSION = "0.1.0"
TAGLINE = "Local-first protection. Commander-approved."
ENGINE_CREDIT = "ClamAV (open-source CLI backend, GPLv2)"
HOMEPAGE = "https://github.com/CommanderSabi/horidoro-av"

# Support / donations (About tab "Support the project")
PATREON_URL = "https://www.patreon.com/cw/CommanderSabi"
USDC_ADDRESS = "2stgEZfERUMs7naTjikdR4CkDUnU1gsYKSHCX4VH5DQt"  # USDC on Solana only

# In-app changelog (About → "What's new"; newest first; reused for Releases)
CHANGELOG = [
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
