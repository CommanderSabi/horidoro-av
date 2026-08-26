# -*- coding: utf-8 -*-
"""Horidoro AV — state / manifest.

The app tracks everything it creates in a JSON manifest so it can remove or
change exactly what the user asks, nothing more. Every install/change/uninstall
decision derives from this file. Idempotent by design: re-running the installer
verifies/repairs rather than duplicating.
"""

import json
import os
from pathlib import Path

from branding import STATE_FILE, VERSION

DEFAULT_STATE = {
    "version": VERSION,
    "container_created": False,
    "packages_installed": False,
    "config_written": False,        # scan.conf
    "scripts_written": False,       # daily / monthly / right-click / update
    "timers_enabled": [],           # ["horidoro-daily", "horidoro-monthly"]
    "desktop_integration": None,    # "kde" | "gnome" | None
    "app_self_installed": False,    # app menu entry + ~/.local/bin copy
    "bashrc_requested": False,      # optional shell aliases
    "bashrc_modified": False,
    "docs_created": False,
    "sudo_rule_installed": False,
    "scan_paths": {                 # user-configurable scan targets
        "daily": [],
        "monthly": [],
    },
    "exclusions": [],
    "schedule": {                  # timer customization (Schedule tab)
        "daily": {"time": "02:00", "days": "Tue,Wed,Thu,Fri,Sat,Sun"},
        "monthly": {"time": "02:00", "day": "1"},
        "auto_update": {"enabled": True, "time": "03:00"},
    },
    "watcher_folders": [],
    "watcher_enabled": False,      # on-access background service
    "sounds_enabled": True,        # notification sounds (Settings toggle)
    "auto_update_check": True,     # app self-update check (Settings → App updates)
    "db_updated": False,
    "self_test_passed": False,
}


class State:
    """Load/save the manifest."""

    def __init__(self):
        self.data = json.loads(json.dumps(DEFAULT_STATE))  # deep copy
        self.load()

    def load(self):
        if STATE_FILE.exists():
            try:
                stored = json.loads(STATE_FILE.read_text())
                stored.update({k: v for k, v in DEFAULT_STATE.items() if k not in stored})
                self.data = stored
            except Exception:
                pass  # corrupt state -> fall back to defaults

    def save(self):
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(self.data, indent=2))

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value
        self.save()

    # --- derived status ----------------------------------------------------
    def is_installed(self):
        return bool(self.data.get("container_created"))

    def summary(self):
        d = self.data
        return {
            "container": "✓" if d["container_created"] else "—",
            "packages": "✓" if d["packages_installed"] else "—",
            "config": "✓" if d["config_written"] else "—",
            "scripts": "✓" if d["scripts_written"] else "—",
            "timers": ", ".join(d["timers_enabled"]) if d["timers_enabled"] else "—",
            "desktop": d["desktop_integration"] or "—",
            "db_updated": "✓" if d["db_updated"] else "—",
            "self_test": "✓" if d["self_test_passed"] else "—",
        }
