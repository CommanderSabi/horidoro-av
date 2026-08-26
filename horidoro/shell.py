# -*- coding: utf-8 -*-
"""Horidoro AV — shell helpers.

Thin wrappers over the commands Horidoro orchestrates. All antivirus work
happens inside the `clamav` distrobox; nothing here touches the host OS
beyond reading status.
"""

import re
import subprocess

from branding import CONTAINER_NAME


def run(cmd, capture=True, timeout=600, check=False):
    """Run a shell command. Returns (returncode, combined output string)."""
    try:
        p = subprocess.run(cmd, shell=True, capture_output=capture, text=True,
                           timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, f"TIMEOUT after {timeout}s: {cmd[:120]}"
    except Exception as e:  # noqa: BLE001 — deliberate catch-all for CLI use
        return 1, str(e)


def distrobox(command, timeout=600):
    """Run a command inside the clamav distrobox (bash -lc)."""
    return run(f'distrobox enter -n {CONTAINER_NAME} -- bash -lc "{command}"',
               timeout=timeout)


def in_container():
    """True if the clamav distrobox exists (created at least once)."""
    rc, out = run("distrobox list")
    return CONTAINER_NAME in out


def engine_status():
    rc, out = distrobox("pgrep -x clamd > /dev/null && echo RUNNING || echo STOPPED")
    return out.strip().splitlines()[-1] if out.strip() else "UNKNOWN"


def db_version():
    """Engine + signature-DB version, e.g. 'ClamAV 1.4.6/28096/Tue ...'.

    Uses `clamscan --version` (works without root; `freshclam --version`
    fails because freshclam.conf is root-only). Filters distrobox startup
    chatter and ANSI colour codes."""
    rc, out = distrobox("clamscan --version 2>/dev/null")
    for line in out.splitlines():
        if "ClamAV" in line:
            return re.sub(r"\x1b\[[0-9;]*m", "", line).strip()
    return "unknown"
