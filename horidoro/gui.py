# -*- coding: utf-8 -*-
"""Horidoro AV — GTK interface (PyGObject/GTK3, present on Fedora Atomic).

The GUI is a controller: every button maps to a function in installer.py /
actions.py. Tabs: Dashboard, Scan, Quarantine, Logs, Schedule, Watcher,
Settings, About.
"""

import os
import sys
import threading

from branding import (APP_NAME, AUTHOR, DOCS_DIR, ENGINE_CREDIT, HOMEPAGE,
                      PATREON_URL, STATE_FILE, TAGLINE, USDC_ADDRESS, VERSION,
                      latest_changelog)
from state import State
from shell import db_version, engine_status, run
import actions
import templates
import updater
from installer import (InstallError, LOG_DIR, apply_config,
                       ensure_self_installed, install_all,
                       mounted_drives, repair_integration,
                       uninstall_all)

try:
    import gi
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk, GLib, Gdk
    HAVE_GTK = True
except Exception:  # noqa: BLE001 — GTK may be missing on non-Fedora systems
    HAVE_GTK = False


class MainWindow(Gtk.Window):
    def __init__(self, state):
        super().__init__()
        self.state = state
        self.set_title(f"{APP_NAME} — {TAGLINE}")
        self.set_default_size(980, 640)
        self.connect("destroy", lambda *_: Gtk.main_quit())
        self._set_app_icon()

        header = Gtk.HeaderBar()
        header.set_show_close_button(True)
        header.set_title(APP_NAME)
        header.set_subtitle(f"v{VERSION} · by {AUTHOR}")
        self.set_titlebar(header)

        self.notebook = Gtk.Notebook()
        self.notebook.set_scrollable(True)
        self._tabs = {}  # title -> page widget (for cross-tab jumps)

        # Persistent Install pinned to the right end of the TAB BAR (same row
        # as the tab labels — always visible, no scrolling). State-aware via
        # refresh_actions: visible only while Horidoro isn't installed.
        self.top_install_btn = Gtk.Button(label="Install Horidoro AV")
        self.top_install_btn.connect("clicked", self.on_install_clicked)
        self.top_install_btn.set_margin_end(8)
        self.notebook.set_action_widget(self.top_install_btn, Gtk.PackType.END)

        self.build_dashboard()
        self.build_scan()
        self.build_quarantine()
        self.build_logs()
        self.build_schedule()
        self.build_watcher()
        self.build_settings()
        self.build_about()

        self._busy = False
        # bottom status bar: global progress (spinner + text) for ANY operation,
        # visible from every tab — not just the Dashboard
        self.progress_spinner = Gtk.Spinner()
        self.progress_label = Gtk.Label(label="", xalign=0.0, wrap=True)
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        bar.set_margin_start(12)
        bar.set_margin_end(12)
        bar.set_margin_top(4)
        bar.set_margin_bottom(6)
        bar.pack_start(self.progress_spinner, False, False, 0)
        bar.pack_start(self.progress_label, True, True, 0)
        mainv = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        mainv.pack_start(self.notebook, True, True, 0)
        mainv.pack_start(bar, False, False, 0)
        self.add(mainv)

        self.refresh_status()
        # Keep the Dashboard summary rows live (toggles + external changes):
        # a cheap in-memory re-read every 3s, so On/Off states never go stale.
        GLib.timeout_add_seconds(3, self._auto_refresh_summary)

    # --- page scaffolding --------------------------------------------------
    def section(self, title):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_start(24)
        box.set_margin_end(24)
        box.set_margin_top(24)
        box.set_margin_bottom(24)
        label = Gtk.Label()
        label.set_markup(f"<span size='xx-large' weight='bold'>{title}</span>")
        label.set_halign(Gtk.Align.START)
        box.pack_start(label, False, False, 0)
        return box

    def _add_tab(self, page, title):
        """Add a tab whose content scrolls if it exceeds the window height —
        keeps the window screen-sized instead of growing past the display."""
        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        sw.add(page)
        self.notebook.append_page(sw, Gtk.Label(label=title))
        self._tabs[title] = sw
        return sw

    def info_row(self, key, value, bind=None):
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        k = Gtk.Label(label=f"{key}:", xalign=1.0)
        k.set_width_chars(22)
        v = Gtk.Label(label=str(value), xalign=0.0)
        if bind:
            setattr(self, bind, v)
        row.pack_start(k, False, False, 0)
        row.pack_start(v, False, False, 0)
        return row

    def refresh_summary(self):
        """Live-update the Dashboard's install-status rows from the manifest.
        ✓ shows green, — grey, other values (like timer names) as text."""
        s = self.state.summary()
        for attr, key in (("_st_container", "container"),
                          ("_st_packages", "packages"),
                          ("_st_config", "config"),
                          ("_st_scripts", "scripts"),
                          ("_st_timers", "timers"),
                          ("_st_desktop", "desktop"),
                          ("_st_db", "db_updated"),
                          ("_st_selftest", "self_test")):
            label = getattr(self, attr, None)
            if label is None:
                continue
            val = s[key]
            if val == "✓":
                label.set_markup("<span color='#2e9e4f'>✓</span>")
            elif val == "—":
                label.set_markup("<span color='grey'>—</span>")
            else:
                label.set_text(val)
        # schedule + real-time rows read live from the manifest
        enabled = self.state.get("timers_enabled") or []
        sched = self.state.get("schedule") or {}
        daily = sched.get("daily") or {}
        monthly = sched.get("monthly") or {}
        targets = self.state.get("scan_paths") or {}
        if "horidoro-daily" in enabled:
            if targets.get("daily"):
                self._st_daily.set_markup(
                    f"<span color='#2e9e4f'>✓ On — {daily.get('days', '…')} "
                    f"at {daily.get('time', '…')}</span>")
            else:
                self._st_daily.set_markup(
                    "<span color='#b36b00'>On — no scan targets set "
                    "(scans skip)</span>")
        else:
            self._st_daily.set_markup("<span color='grey'>Off</span>")
        if "horidoro-monthly" in enabled:
            if targets.get("monthly"):
                self._st_monthly.set_markup(
                    f"<span color='#2e9e4f'>✓ On — day "
                    f"{monthly.get('day', '…')} at {monthly.get('time', '…')}</span>")
            else:
                self._st_monthly.set_markup(
                    "<span color='#b36b00'>On — no scan targets set "
                    "(scans skip)</span>")
        else:
            self._st_monthly.set_markup("<span color='grey'>Off</span>")
        if self.state.get("watcher_enabled"):
            n = len(self.state.get("watcher_folders") or [])
            if n:
                self._st_realtime.set_markup(
                    f"<span color='#2e9e4f'>✓ On — watching {n} folder(s)</span>")
            else:
                self._st_realtime.set_markup(
                    "<span color='#b36b00'>On — no folders watched yet</span>")
        else:
            self._st_realtime.set_markup("<span color='grey'>Off</span>")
        auto = sched.get("auto_update") or {}
        if not self.state.is_installed():
            self._st_autoupdate.set_markup("<span color='grey'>—</span>")
        elif auto.get("enabled", True):
            self._st_autoupdate.set_markup(
                f"<span color='#2e9e4f'>✓ On — auto-updates daily at "
                f"{auto.get('time', '03:00')}</span>")
        else:
            self._st_autoupdate.set_markup(
                "<span color='grey'>Off — update manually</span>")

    def refresh_status(self):
        def work():
            if not self.state.is_installed():
                return {"installed": False, "dbs": []}
            ver = db_version()
            parts = ver.split("/")
            return {"engine": engine_status(), "db": ver,
                    "engine_ver": parts[0].strip() if parts else ver,
                    "engine_upd": actions.engine_update_status(),
                    "dbs": actions.list_dbs(), "installed": True}

        def done(result):
            self.refresh_summary()
            self.refresh_actions()
            self.refresh_schedule_status()
            if result.get("installed"):
                self._status_engine.set_markup(
                    "<span color='#2e9e4f'>✓ On — Idle</span>")
                self._status_db.set_text(result["db"])
                self._st_engine_ver.set_text(result.get("engine_ver", ""))
                upd = result.get("engine_upd") or {}
                if upd.get("update"):
                    self._st_engine_update.set_markup(
                        f"<span color='#b36b00'>⚠ Update available: "
                        f"{upd.get('available')}</span>")
                    self._engine_update_avail = True
                elif upd.get("update") is None:
                    self._st_engine_update.set_markup(
                        "<span color='grey'>Couldn't check</span>")
                    self._engine_update_avail = False
                else:
                    self._st_engine_update.set_markup(
                        f"<span color='#2e9e4f'>✓ Up to date "
                        f"({upd.get('installed', '')})</span>")
                    self._engine_update_avail = False
            else:
                self._status_engine.set_markup(
                    "<span color='grey'>Off</span>")
                self._status_db.set_text("—")
                self._st_engine_ver.set_text("—")
                self._st_engine_update.set_markup("<span color='grey'>—</span>")
                self._engine_update_avail = False
            self.refresh_actions()
            dbs = result.get("dbs") or []
            self.db_expander.set_label(
                f"Signature databases ({len(dbs)})")
            self.db_list_main.set_text("\n".join(dbs) if dbs else "(none)")

        threading.Thread(target=lambda: GLib.idle_add(done, work()), daemon=True).start()

    def refresh_actions(self):
        """Only show actions that make sense in the current install state:
        Install when not installed; Update/Uninstall when installed."""
        installed = self.state.is_installed()
        self.install_btn.set_visible(not installed)
        self.top_install_btn.set_visible(not installed)
        self.update_btn.set_visible(installed)
        self.uninstall_btn.set_visible(installed)
        self.engine_update_btn.set_visible(
            installed and self._engine_update_avail)

    def _auto_refresh_summary(self):
        """Keep the Dashboard status rows current (cheap: in-memory manifest)."""
        self.refresh_summary()
        return True

    # --- pages -------------------------------------------------------------
    def _logo_image(self, size=180):
        """A Gtk.Image of the embedded Horidoro logo (best-effort)."""
        try:
            import base64 as _b64
            from gi.repository import GdkPixbuf
            loader = GdkPixbuf.PixbufLoader.new()
            loader.write(_b64.b64decode(templates.APP_ICON_PNG))
            loader.close()
            pix = loader.get_pixbuf()
            pix = pix.scale_simple(size, size, GdkPixbuf.InterpType.BILINEAR)
            return Gtk.Image.new_from_pixbuf(pix)
        except Exception:  # noqa: BLE001 — cosmetic only
            return None

    def build_dashboard(self):
        page = self.section("Dashboard")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        # status column on the left, logo on the right
        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=24)
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        left.pack_start(self.info_row("Engine", "", bind="_st_container"),
                        False, False, 0)
        left.pack_start(self.info_row("Packages", "", bind="_st_packages"),
                        False, False, 0)
        left.pack_start(self.info_row("Config", "", bind="_st_config"),
                        False, False, 0)
        left.pack_start(self.info_row("Scripts", "", bind="_st_scripts"),
                        False, False, 0)
        left.pack_start(self.info_row("Timers", "", bind="_st_timers"),
                        False, False, 0)
        left.pack_start(self.info_row("Desktop integration", "",
                                      bind="_st_desktop"), False, False, 0)
        left.pack_start(self.info_row("Virus DB", "", bind="_st_db"),
                        False, False, 0)
        left.pack_start(self.info_row("Self-test", "", bind="_st_selftest"),
                        False, False, 0)
        left.pack_start(self.info_row("Daily scan", "", bind="_st_daily"),
                        False, False, 0)
        left.pack_start(self.info_row("Monthly scan", "", bind="_st_monthly"),
                        False, False, 0)
        left.pack_start(self.info_row("Real-time protection", "",
                                      bind="_st_realtime"), False, False, 0)
        left.pack_start(self.info_row("Definition updates", "",
                                      bind="_st_autoupdate"), False, False, 0)

        # AV engine status (inline: label + gear spinner + state)
        erow = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        ek = Gtk.Label(label="AV engine:", xalign=1.0)
        ek.set_width_chars(22)
        self.engine_spinner = Gtk.Spinner()
        self._status_engine = Gtk.Label(label="…", xalign=0.0)
        erow.pack_start(ek, False, False, 0)
        erow.pack_start(self._status_engine, True, True, 0)
        erow.pack_end(self.engine_spinner, False, False, 0)  # keeps text column aligned
        left.pack_start(erow, False, False, 0)
        left.pack_start(self.info_row("Engine version", "", bind="_st_engine_ver"),
                        False, False, 0)
        # engine-update status row + button (checked in the background refresh)
        eupd_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        eupd_k = Gtk.Label(label="Engine update:", xalign=1.0)
        eupd_k.set_width_chars(22)
        self._st_engine_update = Gtk.Label(label="…", xalign=0.0)
        self.engine_update_btn = Gtk.Button(label="Update engine…")
        self.engine_update_btn.connect("clicked", self.on_engine_update)
        eupd_row.pack_start(eupd_k, False, False, 0)
        eupd_row.pack_start(self._st_engine_update, True, True, 0)
        eupd_row.pack_start(self.engine_update_btn, False, False, 0)
        left.pack_start(eupd_row, False, False, 0)
        self._engine_update_avail = False
        left.pack_start(self.info_row("DB version", "", bind="_status_db"),
                        False, False, 0)
        self.refresh_summary()

        top.pack_start(left, True, True, 0)
        logo = self._logo_image(220)
        if logo is not None:
            logo.set_halign(Gtk.Align.END)
            logo.set_valign(Gtk.Align.CENTER)
            top.pack_start(logo, False, False, 0)
        box.pack_start(top, False, False, 0)

        # signature databases (live list, same as Settings → Signature databases)
        self.db_expander = Gtk.Expander(label="Signature databases")
        self.db_list_main = Gtk.Label(label="", xalign=0.0, wrap=True)
        self.db_expander.add(self.db_list_main)
        box.pack_start(self.db_expander, False, False, 0)
        dbtn = Gtk.Button(label="Manage databases… (opens Settings)")
        dbtn.connect("clicked", self._open_settings_tab)
        box.pack_start(dbtn, False, False, 0)

        actions = Gtk.Box(spacing=8)
        self.install_btn = Gtk.Button(label="Install")
        self.install_btn.connect("clicked", self.on_install_clicked)
        self.update_btn = Gtk.Button(label="Update virus DB")
        self.update_btn.connect("clicked", self.on_update_clicked)
        self.uninstall_btn = Gtk.Button(label="Uninstall")
        self.uninstall_btn.connect("clicked", self.on_uninstall_clicked)
        for b in (self.install_btn, self.update_btn, self.uninstall_btn):
            actions.pack_start(b, False, False, 0)
        box.pack_start(actions, False, False, 0)
        self.refresh_actions()  # set initial visibility immediately



        page.pack_start(box, False, False, 0)
        self._add_tab(page, "Dashboard")

    def build_scan(self):
        page = self.section("Scan")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        row = Gtk.Box(spacing=8)
        self.scan_entry = Gtk.Entry()
        self.scan_entry.set_placeholder_text("Path to scan (file or folder)…")
        browse = Gtk.Button(label="Browse…")
        browse.connect("clicked", self.on_browse)
        row.pack_start(self.scan_entry, True, True, 0)
        row.pack_start(browse, False, False, 0)
        box.pack_start(row, False, False, 0)

        modes = Gtk.Box(spacing=8)
        for label in ("Quick scan (fast, multi-core)",
                      "Verbose scan (detailed, single-core)"):
            b = Gtk.Button(label=label)
            b.connect("clicked", self.on_scan_clicked)
            modes.pack_start(b, False, False, 0)
        box.pack_start(modes, False, False, 0)
        srow = Gtk.Box(spacing=8)
        self.scan_spinner = Gtk.Spinner()
        srow.pack_start(self.scan_spinner, False, False, 0)
        self.scan_status = Gtk.Label(label="", xalign=0.0, wrap=True)
        srow.pack_start(self.scan_status, True, True, 0)
        box.pack_start(srow, False, False, 0)
        hint = Gtk.Label(
            label="Quick scan uses the engine daemon and spreads the work across "
                  "all CPU cores — the everyday choice for folders. Verbose scan "
                  "runs the standalone scanner (one core) and lists every file "
                  "individually — use it for a targeted file or a full per-file report.",
            xalign=0.0, wrap=True)
        box.pack_start(hint, False, False, 0)

        self.scan_output = Gtk.TextView()
        self.scan_output.set_editable(False)
        sw = Gtk.ScrolledWindow()
        sw.set_min_content_height(300)
        sw.add(self.scan_output)
        box.pack_start(sw, True, True, 0)

        page.pack_start(box, True, True, 0)
        self._add_tab(page, "Scan")

    def build_quarantine(self):
        page = self.section("Quarantine")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        hint = Gtk.Label(
            label="Quarantined files appear here. Tick the files, then "
                  "Restore or Delete them.",
            xalign=0.0, wrap=True)
        box.pack_start(hint, False, False, 0)
        self.quarantine_list = Gtk.ListBox()
        self.quarantine_list.set_selection_mode(Gtk.SelectionMode.NONE)
        sw = Gtk.ScrolledWindow()
        sw.add(self.quarantine_list)
        box.pack_start(sw, True, True, 0)

        actions = Gtk.Box(spacing=8)
        refresh = Gtk.Button(label="Refresh")
        refresh.connect("clicked", lambda w: self.refresh_quarantine())
        restore = Gtk.Button(label="Restore selected…")
        restore.connect("clicked", self.on_restore_quarantined)
        delete = Gtk.Button(label="Delete selected")
        delete.connect("clicked", self.on_delete_quarantined)
        delete_all = Gtk.Button(label="Delete all…")
        delete_all.connect("clicked", self.on_delete_all_quarantined)
        actions.pack_start(refresh, False, False, 0)
        actions.pack_start(restore, False, False, 0)
        actions.pack_start(delete, False, False, 0)
        actions.pack_start(delete_all, False, False, 0)
        box.pack_start(actions, False, False, 0)

        page.pack_start(box, True, True, 0)
        self._add_tab(page, "Quarantine")
        # auto-refresh so newly quarantined files appear without pressing Refresh
        GLib.timeout_add_seconds(5, self._auto_refresh_quarantine)

    def build_logs(self):
        page = self.section("Logs")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        row = Gtk.Box(spacing=8)
        row.pack_start(Gtk.Label(label="Show log:", xalign=1.0), False, False, 0)
        self.log_chooser = Gtk.ComboBoxText()
        self._log_choices = [
            ("Daily scan", "daily_scan.log"),
            ("On-access (watcher)", "watcher.log"),
            ("Manual scans", "manual_scan.log"),
            ("Monthly scan", "monthly_full_scan.log"),
            ("Right-click scans", "right_click_scan.log"),
            ("Definition updates", "update_history.log"),
        ]
        for label, _fname in self._log_choices:
            self.log_chooser.append_text(label)
        self.log_chooser.connect("changed", self._on_log_chosen)
        row.pack_start(self.log_chooser, False, False, 0)
        box.pack_start(row, False, False, 0)
        self.log_view = Gtk.TextView()
        self.log_view.set_editable(False)
        sw = Gtk.ScrolledWindow()
        sw.add(self.log_view)
        box.pack_start(sw, True, True, 0)
        refresh = Gtk.Button(label="Refresh")
        refresh.connect("clicked", lambda w: self.refresh_logs())
        brow = Gtk.Box(spacing=8)
        brow.pack_start(refresh, False, False, 0)
        delete = Gtk.Button(label="Delete this log…")
        delete.connect("clicked", self.on_delete_log_clicked)
        brow.pack_start(delete, False, False, 0)
        box.pack_start(brow, False, False, 0)
        page.pack_start(box, True, True, 0)
        self._add_tab(page, "Logs")
        self._logs_mtime = 0
        # default to the first log that actually has content (on a fresh
        # install the daily log may not exist yet)
        default = 0
        for i, (_label, fname) in enumerate(self._log_choices):
            try:
                if (LOG_DIR / fname).stat().st_size > 0:
                    default = i
                    break
            except OSError:
                continue
        self.log_chooser.set_active(default)
        self._log_name = self._log_choices[default][1]
        GLib.timeout_add_seconds(10, self._auto_refresh_logs)

    def build_schedule(self):
        page = self.section("Schedule")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        sched = self.state.get("schedule") or {}
        # switches presented like the Watcher one (label + switch + status text)
        # so they read as on/off toggles, not bare sliders
        drow = Gtk.Box(spacing=12)
        drow.pack_start(Gtk.Label(label="Daily scan", xalign=0.0),
                        False, False, 0)
        self.daily_toggle = Gtk.Switch()
        self.daily_toggle.set_active("horidoro-daily" in self.state.get("timers_enabled", []))
        self.daily_toggle.connect("state-set", self.on_daily_toggle)
        drow.pack_start(self.daily_toggle, False, False, 0)
        self.daily_status = Gtk.Label(label="", xalign=0.0, wrap=True)
        drow.pack_start(self.daily_status, True, True, 0)
        box.pack_start(drow, False, False, 0)

        mrow = Gtk.Box(spacing=12)
        mrow.pack_start(Gtk.Label(label="Monthly full scan", xalign=0.0),
                        False, False, 0)
        self.monthly_toggle = Gtk.Switch()
        self.monthly_toggle.set_active("horidoro-monthly" in self.state.get("timers_enabled", []))
        self.monthly_toggle.connect("state-set", self.on_monthly_toggle)
        mrow.pack_start(self.monthly_toggle, False, False, 0)
        self.monthly_status = Gtk.Label(label="", xalign=0.0, wrap=True)
        mrow.pack_start(self.monthly_status, True, True, 0)
        box.pack_start(mrow, False, False, 0)

        # automatic definition updates (default ON — signatures stay fresh)
        urow = Gtk.Box(spacing=12)
        urow.pack_start(Gtk.Label(label="Automatic definition updates",
                                  xalign=0.0), False, False, 0)
        self.auto_update_toggle = Gtk.Switch()
        self.auto_update_toggle.set_active(
            bool(self.state.is_installed()
                 and (sched.get("auto_update") or {}).get("enabled", True)))
        self.auto_update_toggle.connect("state-set", self.on_auto_update_toggle)
        urow.pack_start(self.auto_update_toggle, False, False, 0)
        urow.pack_start(Gtk.Label(label="at", xalign=1.0), False, False, 0)
        self.auto_update_time = Gtk.Entry()
        self.auto_update_time.set_text(
            (sched.get("auto_update") or {}).get("time", "03:00"))
        self.auto_update_time.set_width_chars(6)
        urow.pack_start(self.auto_update_time, False, False, 0)
        self.auto_update_status = Gtk.Label(label="", xalign=0.0, wrap=True)
        urow.pack_start(self.auto_update_status, True, True, 0)
        box.pack_start(urow, False, False, 0)

        box.pack_start(Gtk.Separator(), False, False, 0)
        daily = sched.get("daily") or {}
        monthly = sched.get("monthly") or {}

        lab = Gtk.Label()
        lab.set_markup("<b>When scans run</b>")
        lab.set_halign(Gtk.Align.START)
        box.pack_start(lab, False, False, 0)

        drow = Gtk.Box(spacing=8)
        drow.pack_start(Gtk.Label(label="Daily at", xalign=1.0), False, False, 0)
        self.daily_time = Gtk.Entry()
        self.daily_time.set_text(daily.get("time", "02:00"))
        self.daily_time.set_width_chars(6)
        drow.pack_start(self.daily_time, False, False, 0)
        drow.pack_start(Gtk.Label(label="on", xalign=1.0), False, False, 0)
        # day picker: checkboxes beat a free-text field — no typos possible
        daybox = Gtk.Box(spacing=4)
        self.daily_day_cb = {}
        for name in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"):
            cb = Gtk.CheckButton(label=name)
            self.daily_day_cb[name] = cb
            daybox.pack_start(cb, False, False, 0)
        self._set_days_checkboxes(daily.get("days", "Tue,Wed,Thu,Fri,Sat,Sun"))
        drow.pack_start(daybox, False, False, 0)
        box.pack_start(drow, False, False, 0)

        mrow = Gtk.Box(spacing=8)
        mrow.pack_start(Gtk.Label(label="Monthly on day", xalign=1.0), False, False, 0)
        self.monthly_day = Gtk.Entry()
        self.monthly_day.set_text(monthly.get("day", "1"))
        self.monthly_day.set_width_chars(4)
        mrow.pack_start(self.monthly_day, False, False, 0)
        mrow.pack_start(Gtk.Label(label="at", xalign=1.0), False, False, 0)
        self.monthly_time = Gtk.Entry()
        self.monthly_time.set_text(monthly.get("time", "02:00"))
        self.monthly_time.set_width_chars(6)
        mrow.pack_start(self.monthly_time, False, False, 0)
        box.pack_start(mrow, False, False, 0)

        apply = Gtk.Button(label="Apply schedule")
        apply.connect("clicked", self.on_apply_schedule_clicked)
        box.pack_start(apply, False, False, 0)
        targets = Gtk.Button(label="Change what gets scanned… (opens Settings)")
        targets.connect("clicked", self._open_settings_tab)
        box.pack_start(targets, False, False, 0)
        hint = Gtk.Label(
            label="Time is 24-hour HH:MM (e.g. 02:00). Tick the days the daily "
                  "scan should run (none ticked = every day). Monthly day is a "
                  "number 1–28.\n\n"
                  "Virus definitions update automatically before every scheduled "
                  "scan (and any time you press Update virus DB).",
            xalign=0.0, wrap=True)
        box.pack_start(hint, False, False, 0)
        page.pack_start(box, False, False, 0)
        self._add_tab(page, "Schedule")
        self.refresh_schedule_status()

    def refresh_schedule_status(self):
        enabled = self.state.get("timers_enabled") or []
        sched = self.state.get("schedule") or {}
        daily = sched.get("daily") or {}
        monthly = sched.get("monthly") or {}
        installed = self.state.is_installed()
        auto = sched.get("auto_update") or {}
        # the auto-update switch only reflects ON after a real install
        self.auto_update_toggle.set_active(
            bool(installed and auto.get("enabled", True)))
        if "horidoro-daily" in enabled:
            days = daily.get("days", "…")
            if days == "Mon,Tue,Wed,Thu,Fri,Sat,Sun":
                days = "every day"
            self.daily_status.set_text(
                f"On — {days} at {daily.get('time', '…')}")
        else:
            self.daily_status.set_text("Off — daily scan not scheduled")
        if "horidoro-monthly" in enabled:
            self.monthly_status.set_text(
                f"On — day {monthly.get('day', '…')} at {monthly.get('time', '…')}")
        else:
            self.monthly_status.set_text("Off — monthly scan not scheduled")
        auto = sched.get("auto_update") or {}
        if not installed:
            self.auto_update_status.set_text("Off until installed")
        elif auto.get("enabled", True):
            self.auto_update_status.set_text(
                f"On — updates automatically every day at "
                f"{auto.get('time', '03:00')}")
        else:
            self.auto_update_status.set_text(
                "Off — definitions update only before scheduled scans "
                "or via the Update button")
        if hasattr(self, "sounds_toggle"):
            son = installed and self.state.get("sounds_enabled", True)
            self.sounds_toggle.set_active(bool(son))
            self.sounds_status.set_text(
                "On — plays on scan complete, threats, and updates" if son
                else "Off — everything silent")

    def build_watcher(self):
        page = self.section("Watcher")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        hint = Gtk.Label(
            label="On-access protection: new or changed files in watched folders are "
                  "scanned the moment they land. Runs as a background service "
                  "(horidoro-watcher), fully local — kernel events, no cloud, no polling.",
            xalign=0.0, wrap=True)
        box.pack_start(hint, False, False, 0)

        # on/off toggle + live status
        row = Gtk.Box(spacing=12)
        row.pack_start(Gtk.Label(label="On-access protection", xalign=0.0),
                       False, False, 0)
        self.watcher_switch = Gtk.Switch()
        self.watcher_switch.set_active(bool(self.state.get("watcher_enabled")))
        self.watcher_switch.connect("state-set", self.on_watcher_toggle)
        row.pack_start(self.watcher_switch, False, False, 0)
        self.watcher_status = Gtk.Label(label="…", xalign=0.0, wrap=True)
        row.pack_start(self.watcher_status, True, True, 0)
        box.pack_start(row, False, False, 0)

        # watched folders
        self.watcher_folders = Gtk.ListBox()
        sw = Gtk.ScrolledWindow()
        sw.set_min_content_height(120)
        sw.add(self.watcher_folders)
        box.pack_start(sw, False, False, 0)
        frow = Gtk.Box(spacing=8)
        add = Gtk.Button(label="Add folder to watch…")
        add.connect("clicked", self.on_watcher_add)
        dl = Gtk.Button(label="Watch Downloads")
        dl.connect("clicked", self._watch_downloads)
        remove = Gtk.Button(label="Remove selected")
        remove.connect("clicked", self.on_watcher_remove)
        restart = Gtk.Button(label="Restart watcher")
        restart.connect("clicked", self.on_restart_watcher)
        frow.pack_start(add, False, False, 0)
        frow.pack_start(dl, False, False, 0)
        frow.pack_start(remove, False, False, 0)
        frow.pack_start(restart, False, False, 0)
        box.pack_start(frow, False, False, 0)
        GLib.timeout_add_seconds(5, self._auto_refresh_watcher)

        exp = Gtk.Expander(label="How on-access protection works — and its memory cost")
        exp.add(Gtk.Label(
            label="While on-access protection is ON, the antivirus engine stays "
                  "loaded in memory (a few hundred MB) so new files are scanned the "
                  "instant they land — no waiting, no polling. Adding more folders "
                  "costs almost nothing extra (just a few kernel watches per folder). "
                  "Toggle the switch off to unload the engine and free the RAM; "
                  "scheduled and manual scans still work and wake the engine only "
                  "when needed.\n\n"
                  "Browsers (Chrome, Brave, Firefox…) stage every download in your "
                  "Downloads folder before you pick where to save it — so as long as "
                  "Downloads is watched (the app adds it automatically when you turn "
                  "on-access on), any file downloaded through a browser is scanned "
                  "automatically, no matter where you save it. If a download is "
                  "caught, the browser may leave a small 'Unconfirmed *.crdownload' "
                  "fragment behind; Horidoro cleans those up in your common folders "
                  "automatically.\n\n"
                  "Watched folders matter for everything else: files you copy, move, "
                  "or receive yourself are only scanned if their folder is in the "
                  "list above. Add the folders you use most (Downloads is added "
                  "automatically).\n\n"
                  "Linux caps how many folders can be watched per user (inotify). "
                  "If you add a lot of folders, the cap can be reached — Horidoro "
                  "will warn you, and it can be raised.",
            wrap=True, xalign=0.0))
        box.pack_start(exp, False, False, 0)

        # watch-limit warning (shown when the service hits the inotify cap)
        self.watcher_limit_warn = Gtk.Label(label="", wrap=True, xalign=0.0)
        self.watcher_limit_warn.set_markup(
            "<span color='#d43d2a'>⚠ Watch limit reached — some folders may not "
            "be watched. Raise it with:\n"
            "echo fs.inotify.max_user_watches=524288 | sudo tee "
            "/etc/sysctl.d/90-inotify.conf &amp;&amp; sudo sysctl -p</span>")
        self.watcher_limit_warn.set_no_show_all(True)
        box.pack_start(self.watcher_limit_warn, False, False, 0)

        # event log (tail of the watcher's own log)
        lab = Gtk.Label(label="Event log (last entries):", xalign=0.0)
        box.pack_start(lab, False, False, 0)
        self.watcher_log = Gtk.TextView()
        self.watcher_log.set_editable(False)
        lsw = Gtk.ScrolledWindow()
        lsw.set_min_content_height(140)
        lsw.add(self.watcher_log)
        box.pack_start(lsw, True, True, 0)

        page.pack_start(box, True, True, 0)
        self._add_tab(page, "Watcher")
        self.refresh_watcher()

    def refresh_watcher(self):
        # folders list (kept separate so the auto status refresh never steals
        # the user's selection)
        self.watcher_folders.foreach(lambda w: self.watcher_folders.remove(w))
        for folder in (self.state.get("watcher_folders") or []):
            row = Gtk.ListBoxRow()
            row.add(Gtk.Label(label=str(folder), xalign=0.0))
            row._path = str(folder)
            self.watcher_folders.add(row)
        self.watcher_folders.show_all()
        self.refresh_watcher_status()

    def refresh_watcher_status(self):
        _rc, out = run("systemctl --user is-active horidoro-watcher.service 2>/dev/null")
        active = out.strip() == "active"
        _rc2, out2 = run("systemctl --user is-failed horidoro-watcher.service 2>/dev/null")
        is_failed = out2.strip() == "failed"
        enabled = bool(self.state.get("watcher_enabled"))
        if is_failed:
            self.watcher_status.set_text(
                "FAILED — the watcher crashed. Press Restart, then check the "
                "event log below for the reason.")
        elif active:
            n = len(self.state.get("watcher_folders") or [])
            self.watcher_status.set_text(
                f"ACTIVE — watching {n} folder(s). New files are scanned as they land.")
        elif enabled:
            self.watcher_status.set_text(
                "On, but not running right now — it may have failed to start "
                "(see the event log below). Press Restart.")
        else:
            self.watcher_status.set_text(
                "Inactive. Flip the switch to start real-time protection.")
        log = actions.read_log("watcher.log")
        if "WATCH-LIMIT" in log:
            self.watcher_limit_warn.show()
        else:
            self.watcher_limit_warn.hide()
        self.watcher_log.get_buffer().set_text(log)

    def _auto_refresh_watcher(self):
        if self.state.get("watcher_enabled"):
            self.refresh_watcher_status()
        return True

    def on_restart_watcher(self, _w):
        run("systemctl --user restart horidoro-watcher.service 2>/dev/null")
        GLib.timeout_add(1500, self.refresh_watcher_status)
        self.watcher_log.get_buffer().set_text(actions.read_log("watcher.log"))

    def on_watcher_toggle(self, switch, active):
        if active and not self.state.is_installed():
            # Reject via idle: switch snaps back to OFF, notice appears once
            # after this handler returns (no nested modal loop, no loop).
            GLib.idle_add(self._reject_switch, switch,
                          "On-access protection needs Horidoro installed "
                          "(Install on the Dashboard).")
            return True
        if active and not (self.state.get("watcher_folders") or []):
            self._watch_downloads()  # sensible default: protect Downloads first
        self.state.set("watcher_enabled", active)
        self.refresh_summary()  # Dashboard row updates instantly
        unit = "horidoro-watcher.service"
        if active:
            run(f"systemctl --user enable --now {unit} 2>/dev/null")
        else:
            run(f"systemctl --user disable --now {unit} 2>/dev/null")
        GLib.timeout_add(1200, self.refresh_watcher)  # let the service settle
        return False  # accept the change (default handler applies it)

    def _watch_downloads(self, _w=None):
        dl = os.path.expanduser("~/Downloads")
        if os.path.isdir(dl):
            dl = os.path.realpath(dl)
            folders = [str(f) for f in (self.state.get("watcher_folders") or [])]
            if dl not in folders:
                folders.append(dl)
                self.state.set("watcher_folders", folders)
                self._restart_watcher_if_active()
                self.refresh_watcher()

    def _add_suggested_daily(self, _w=None):
        for name in ("Downloads", "Documents", "Desktop", "Pictures",
                     "Videos", "Music"):
            p = os.path.expanduser(f"~/{name}")
            if os.path.isdir(p):
                self._append_row(self.daily_path_list, os.path.realpath(p))

    def on_watcher_add(self, _w):
        if not self.state.is_installed():
            self.show_notice("Install first",
                             "Install Horidoro before setting up on-access protection.")
            return
        dlg = Gtk.FileChooserDialog("Add folder to watch", self,
                                    Gtk.FileChooserAction.SELECT_FOLDER,
                                    (Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                                     Gtk.STOCK_OPEN, Gtk.ResponseType.OK))
        if dlg.run() == Gtk.ResponseType.OK:
            path = os.path.realpath(dlg.get_filename())
            folders = [str(f) for f in (self.state.get("watcher_folders") or [])]
            if any(path == f or path.startswith(f + os.sep)
                   or f.startswith(path + os.sep) for f in folders):
                self.show_notice("Already covered",
                                 "That folder is already watched (directly or via a parent).")
            else:
                folders.append(path)
                self.state.set("watcher_folders", folders)
                self._restart_watcher_if_active()
                self.refresh_watcher()
        dlg.destroy()

    def on_watcher_remove(self, _w):
        row = self.watcher_folders.get_selected_row()
        if row is None:
            return
        folders = [str(f) for f in (self.state.get("watcher_folders") or [])]
        if row._path in folders:
            folders.remove(row._path)
            self.state.set("watcher_folders", folders)
            self._restart_watcher_if_active()
            self.refresh_watcher()

    def _restart_watcher_if_active(self):
        if self.state.get("watcher_enabled"):
            run("systemctl --user restart horidoro-watcher.service 2>/dev/null")

    def build_settings(self):
        page = self.section("Settings")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        scrolly = Gtk.Label()
        scrolly.set_markup("<span color='grey' size='small'>This page scrolls — "
                            "use the mouse wheel or the scrollbar on the right "
                            "↓</span>")
        scrolly.set_halign(Gtk.Align.START)
        box.pack_start(scrolly, False, False, 0)
        box.pack_start(self.info_row("Data location", DOCS_DIR), False, False, 0)
        box.pack_start(self.info_row("State file", STATE_FILE), False, False, 0)
        box.pack_start(self.info_row("Engine", ENGINE_CREDIT), False, False, 0)

        # signature databases -----------------------------------------------
        box.pack_start(Gtk.Separator(), False, False, 0)
        lab = Gtk.Label()
        lab.set_markup("<b>Signature databases</b>")
        lab.set_halign(Gtk.Align.START)
        box.pack_start(lab, False, False, 0)
        self.db_list = Gtk.ListBox()
        dsw = Gtk.ScrolledWindow()
        dsw.set_min_content_height(110)
        dsw.add(self.db_list)
        box.pack_start(dsw, False, False, 0)
        drow = Gtk.Box(spacing=8)
        ref = Gtk.Button(label="Refresh")
        ref.connect("clicked", lambda _w: self.refresh_dbs())
        addb = Gtk.Button(label="Add database file…")
        addb.connect("clicked", self.on_db_add)
        addurl = Gtk.Button(label="Add by URL…")
        addurl.connect("clicked", self.on_db_add_url)
        rem = Gtk.Button(label="Remove selected")
        rem.connect("clicked", self.on_db_remove)
        drow.pack_start(ref, False, False, 0)
        drow.pack_start(addb, False, False, 0)
        drow.pack_start(addurl, False, False, 0)
        drow.pack_start(rem, False, False, 0)
        box.pack_start(drow, False, False, 0)
        third = Gtk.Button(label="Add recommended databases…")
        third.connect("clicked", self.on_db_thirdparty)
        box.pack_start(third, False, False, 0)
        # local working indicator right here in the databases section
        dbwork = Gtk.Box(spacing=8)
        self.db_spinner = Gtk.Spinner()
        self.db_status = Gtk.Label(label="", xalign=0.0, wrap=True)
        dbwork.pack_start(self.db_spinner, False, False, 0)
        dbwork.pack_start(self.db_status, True, True, 0)
        box.pack_start(dbwork, False, False, 0)
        dhint = Gtk.Label(
            label="How to add databases:\n• 'Add by URL' subscribes to a feed — "
                  "it updates automatically with every definition update.\n"
                  "• 'Add recommended' adds the author's picks the same way.\n"
                  "• 'Add database file' copies a static file — it will NOT "
                  "update itself over time.\n"
                  "Changes load when the engine next starts.",
            xalign=0.0, wrap=True)
        box.pack_start(dhint, False, False, 0)
        self.refresh_dbs()
        GLib.timeout_add_seconds(10, self._auto_refresh_dbs)

        # notification sounds ---------------------------------------------
        srow = Gtk.Box(spacing=12)
        srow.pack_start(Gtk.Label(label="Sound notifications", xalign=0.0),
                        False, False, 0)
        self.sounds_toggle = Gtk.Switch()
        # Like the other toggles: off until installed, then ON (the default
        # preference, guaranteed by the uninstall reset).
        self.sounds_toggle.set_active(
            bool(self.state.is_installed()
                 and self.state.get("sounds_enabled", True)))
        self.sounds_toggle.connect("state-set", self.on_sounds_toggle)
        srow.pack_start(self.sounds_toggle, False, False, 0)
        self.sounds_status = Gtk.Label(label="", xalign=0.0, wrap=True)
        srow.pack_start(self.sounds_status, True, True, 0)
        box.pack_start(srow, False, False, 0)

        # engine updates ---------------------------------------------------
        box.pack_start(Gtk.Separator(), False, False, 0)
        lab = Gtk.Label()
        lab.set_markup("<b>Engine updates</b>")
        lab.set_halign(Gtk.Align.START)
        box.pack_start(lab, False, False, 0)
        self.engine_update_label = Gtk.Label(
            label="Automatic updates only refresh the virus definitions — the "
                  "ClamAV engine version changes only when you update it here.",
            xalign=0.0, wrap=True)
        box.pack_start(self.engine_update_label, False, False, 0)
        erow = Gtk.Box(spacing=8)
        checkb = Gtk.Button(label="Check for engine updates…")
        checkb.connect("clicked", self.on_engine_check)
        upb = Gtk.Button(label="Update engine…")
        upb.connect("clicked", self.on_engine_update)
        erow.pack_start(checkb, False, False, 0)
        erow.pack_start(upb, False, False, 0)
        box.pack_start(erow, False, False, 0)
        # don't-panic context: a new engine version is not urgent
        calm = Gtk.Expander(label="Do I need to update right away?")
        calm.add(Gtk.Label(
            label="No. Your virus definitions update automatically many times a "
                  "day — those are what catch malware, and they work with any "
                  "recent engine version. A new ENGINE version is mostly fixes "
                  "for specific file-type parsing and bugs: worth doing soon, "
                  "but not urgent, and the stable 1.4.x line stays supported — "
                  "being one version behind is common.\n\n"
                  "What could happen if you update: on major version jumps, "
                  "config options or command flags can change — the app's "
                  "battle-tested settings live in ~/.local/share/horidoro/scripts "
                  "and /etc/clamd.d/scan.conf if anything needs checking. Rarely, "
                  "an update introduces a regression. That's why this is your "
                  "choice, not automatic — update when it's convenient.",
            wrap=True, xalign=0.0))
        box.pack_start(calm, False, False, 0)

        # app updates --------------------------------------------------------
        box.pack_start(Gtk.Separator(), False, False, 0)
        lab = Gtk.Label()
        lab.set_markup("<b>App updates</b>")
        lab.set_halign(Gtk.Align.START)
        box.pack_start(lab, False, False, 0)
        arow = Gtk.Box(spacing=12)
        arow.pack_start(Gtk.Label(label="Automatic app updates", xalign=0.0),
                        False, False, 0)
        self.app_update_toggle = Gtk.Switch()
        self.app_update_toggle.set_active(
            bool(self.state.is_installed()
                 and self.state.get("auto_update_check", True)))
        self.app_update_toggle.connect("state-set", self.on_app_update_toggle)
        arow.pack_start(self.app_update_toggle, False, False, 0)
        arow.pack_start(
            Gtk.Label(label="checks for new versions daily and installs them "
                             "automatically (verified by checksum)",
                      wrap=True, xalign=0.0), True, True, 0)
        box.pack_start(arow, False, False, 0)
        self.app_update_status = Gtk.Label(label="", xalign=0.0, wrap=True)
        box.pack_start(self.app_update_status, False, False, 0)
        urow2 = Gtk.Box(spacing=8)
        ck = Gtk.Button(label="Check for updates…")
        ck.connect("clicked", self.on_check_update_clicked)
        upb2 = Gtk.Button(label="Update now…")
        upb2.connect("clicked", self.on_update_app_clicked)
        urow2.pack_start(ck, False, False, 0)
        urow2.pack_start(upb2, False, False, 0)
        box.pack_start(urow2, False, False, 0)
        ahelp = Gtk.Label(
            label="Updates only replace the app file — your settings, "
                  "quarantine, and logs are kept. The download is verified "
                  "against the published checksum before it's installed. A "
                  "sound plays when an automatic update completes.",
            wrap=True, xalign=0.0)
        box.pack_start(ahelp, False, False, 0)

        # scan targets + exclusions -------------------------------------------
        lab = Gtk.Label()
        lab.set_markup("<b>Scan targets</b>")
        lab.set_halign(Gtk.Align.START)
        box.pack_start(lab, False, False, 0)
        self.daily_path_list = Gtk.ListBox()
        self.monthly_path_list = Gtk.ListBox()
        self.exclusion_list = Gtk.ListBox()
        self._config_list_section(box, "Daily scan targets", self.daily_path_list,
                                  self._add_daily_path, self._remove_daily_path,
                                  quick=("Home", "Downloads", "Documents",
                                         "Desktop", "Pictures", "Videos",
                                         "Music"))
        self._config_list_section(box, "Monthly full scan targets",
                                  self.monthly_path_list,
                                  self._add_monthly_path, self._remove_monthly_path,
                                  quick=("Home", "Downloads", "Documents",
                                         "Desktop", "Drives"))
        self._config_list_section(box, "Exclusions (never scanned)",
                                  self.exclusion_list,
                                  self._add_exclusion, self._remove_exclusion,
                                  quick=())
        self._fill_path_list(self.daily_path_list,
                             self.state.get("scan_paths", {}).get("daily") or [])
        self._fill_path_list(self.monthly_path_list,
                             self.state.get("scan_paths", {}).get("monthly") or [])
        self._fill_path_list(self.exclusion_list, self.state.get("exclusions") or [])
        apply = Gtk.Button(label="Apply scan configuration")
        apply.connect("clicked", self.on_apply_config_clicked)
        box.pack_start(apply, False, False, 0)
        hint = Gtk.Label(
            label="An EMPTY list means no automatic scan until you add targets — "
                  "nothing is scanned implicitly. Use the Quick add buttons "
                  "(+ Home, + Downloads…) or Add… to choose exactly what gets "
                  "scanned, then press Apply.",
            xalign=0.0, wrap=True)
        box.pack_start(hint, False, False, 0)

        guide = Gtk.Expander(
            label="Scanning guide — what to scan, false positives, memory use")
        guide.add(Gtk.Label(
            label="What to scan: your home folder plus anywhere you download files "
                  "is the right coverage for most people. The monthly full scan also "
                  "covers mounted drives.\n\n"
                  "What you can safely skip: internals of apps and the system — "
                  "container layers, package caches, Steam/Flatpak library folders, "
                  "node_modules, compiled bundles. They are verified by their own "
                  "tools, are often huge, and add noise to results.\n\n"
                  "False positives: ClamAV is signature-based, so it flags known "
                  "malware patterns. Some legitimate files (bundled JavaScript, "
                  "app.asar archives, installers with packers) can match. If you "
                  "trust a file, restore it from quarantine or add an exclusion.\n\n"
                  "Memory use: while on-access protection is ON (Watcher tab), the "
                  "engine stays loaded so new files are scanned instantly — that "
                  "costs a few hundred MB of RAM. Toggle on-access off to free the "
                  "memory; scheduled and manual scans still work and start the "
                  "engine on demand.",
            wrap=True, xalign=0.0))
        box.pack_start(guide, False, False, 0)

        page.pack_start(box, False, False, 0)
        self._add_tab(page, "Settings")

    # --- scan targets / exclusions / schedule config ---------------------------
    def _config_list_section(self, box, title, listbox, on_add, on_remove,
                             quick=("Home", "Downloads", "Documents")):
        lab = Gtk.Label()
        lab.set_markup(f"<b>{title}</b>")
        lab.set_halign(Gtk.Align.START)
        box.pack_start(lab, False, False, 0)
        sw = Gtk.ScrolledWindow()
        sw.set_min_content_height(80)
        sw.add(listbox)
        box.pack_start(sw, False, False, 0)
        btns = Gtk.Box(spacing=8)
        add = Gtk.Button(label="Add…")
        add.connect("clicked", on_add)
        rm = Gtk.Button(label="Remove selected")
        rm.connect("clicked", on_remove)
        btns.pack_start(add, False, False, 0)
        btns.pack_start(rm, False, False, 0)
        box.pack_start(btns, False, False, 0)
        # one-click suggestions
        qrow = Gtk.Box(spacing=8)
        qrow.pack_start(Gtk.Label(label="Quick add:"), False, False, 0)
        for name in quick:
            b = Gtk.Button(label=f"+ {name}")
            b.connect("clicked",
                      lambda _w, n=name, lb=listbox: self._quick_add(lb, n))
            qrow.pack_start(b, False, False, 0)
        box.pack_start(qrow, False, False, 0)

    def _quick_add(self, listbox, name):
        """One-click suggestion: append a well-known location to a target list.
        'Home' always exists; named folders only if they exist on this machine;
        'Drives' appends every mounted user-data drive."""
        if name == "Drives":
            for d in mounted_drives():
                self._append_row(listbox, d)
            return
        if name == "Home":
            path = os.path.realpath(os.path.expanduser("~"))
        else:
            path = os.path.realpath(os.path.expanduser(f"~/{name}"))
            if not os.path.isdir(path):
                return
        self._append_row(listbox, path)

    def _fill_path_list(self, listbox, paths):
        listbox.foreach(lambda w: listbox.remove(w))
        for p in paths:
            row = Gtk.ListBoxRow()
            row.add(Gtk.Label(label=str(p), xalign=0.0))
            row._path = str(p)
            listbox.add(row)
        listbox.show_all()

    def _path_list_values(self, listbox):
        out = []
        listbox.foreach(lambda w: out.append(w._path))
        return out

    def _pick_path(self, title, on_pick):
        dlg = Gtk.FileChooserDialog(title, self,
                                    Gtk.FileChooserAction.SELECT_FOLDER,
                                    (Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                                     Gtk.STOCK_OPEN, Gtk.ResponseType.OK))
        if dlg.run() == Gtk.ResponseType.OK:
            on_pick(os.path.realpath(dlg.get_filename()))
        dlg.destroy()

    def _prompt_text(self, title, label_text, on_ok):
        dlg = Gtk.MessageDialog(transient_for=self, modal=True,
                                message_type=Gtk.MessageType.QUESTION,
                                buttons=Gtk.ButtonsType.OK_CANCEL, text=title)
        entry = Gtk.Entry()
        dlg.get_message_area().pack_start(entry, False, False, 0)
        dlg.format_secondary_text(label_text)
        entry.show()
        resp = dlg.run()
        if resp == Gtk.ResponseType.OK and entry.get_text().strip():
            on_ok(entry.get_text().strip())
        dlg.destroy()

    def _append_row(self, listbox, value):
        for existing in self._path_list_values(listbox):
            if existing == value:
                return
        row = Gtk.ListBoxRow()
        row.add(Gtk.Label(label=value, xalign=0.0))
        row._path = value
        listbox.add(row)
        listbox.show_all()

    def _remove_selected(self, listbox):
        row = listbox.get_selected_row()
        if row is not None:
            listbox.remove(row)

    def _add_daily_path(self, _w):
        self._pick_path("Add daily scan target",
                        lambda p: self._append_row(self.daily_path_list, p))

    def _add_monthly_path(self, _w):
        self._pick_path("Add monthly scan target",
                        lambda p: self._append_row(self.monthly_path_list, p))

    def _add_exclusion(self, _w):
        self._prompt_text(
            "Add exclusion",
            "Path that should never be scanned (e.g. /home/you/Videos). "
            "You can also use a pattern starting with ^.",
            lambda v: self._append_row(self.exclusion_list, v))

    def _remove_daily_path(self, _w):
        self._remove_selected(self.daily_path_list)

    def _remove_monthly_path(self, _w):
        self._remove_selected(self.monthly_path_list)

    def _remove_exclusion(self, _w):
        self._remove_selected(self.exclusion_list)

    def on_apply_config_clicked(self, _w):
        if self._busy or not self._require_installed():
            return
        sp = dict(self.state.get("scan_paths") or {})
        sp["daily"] = self._path_list_values(self.daily_path_list)
        sp["monthly"] = self._path_list_values(self.monthly_path_list)
        self.state.set("scan_paths", sp)
        self.state.set("exclusions", self._path_list_values(self.exclusion_list))

        def work(progress):
            apply_config(self.state, progress=progress)
            return "Scan configuration applied."

        def done(out):
            self.refresh_status()
            self.show_notice("Settings", out)

        self._threaded(work, done, progress=self._progress)

    def on_apply_schedule_clicked(self, _w):
        if self._busy or not self._require_installed():
            return
        self.state.set("schedule", {
            "daily": {"time": self.daily_time.get_text().strip(),
                      "days": self._days_from_checkboxes()},
            "monthly": {"time": self.monthly_time.get_text().strip(),
                        "day": self.monthly_day.get_text().strip()},
            "auto_update": {"enabled": self.auto_update_toggle.get_active(),
                            "time": self.auto_update_time.get_text().strip()},
        })

        def work(progress):
            apply_config(self.state, progress=progress)
            return "Schedule applied."

        def done(out):
            self.refresh_status()
            self.show_notice("Schedule", out)

        self._threaded(work, done, progress=self._progress)

    _DAY_ORDER = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

    def _set_days_checkboxes(self, days):
        """Tick the day checkboxes from a stored comma-list (e.g. "Mon,Wed")."""
        checked = {d.strip() for d in str(days or "").split(",") if d.strip()}
        for name in self._DAY_ORDER:
            self.daily_day_cb[name].set_active(name in checked)

    def _days_from_checkboxes(self):
        """Comma-joined checked days in Mon..Sun order. None ticked = every
        day (the user can't produce a broken day string — no typos possible)."""
        picked = [n for n in self._DAY_ORDER
                  if self.daily_day_cb[n].get_active()]
        return ",".join(picked or list(self._DAY_ORDER))

    def _set_app_icon(self):
        """Window/taskbar icon from the embedded Horidoro logo (best-effort)."""
        try:
            import base64 as _b64
            from gi.repository import GdkPixbuf
            loader = GdkPixbuf.PixbufLoader.new()
            loader.write(_b64.b64decode(templates.APP_ICON_PNG))
            loader.close()
            self.set_icon(loader.get_pixbuf())
        except Exception:  # noqa: BLE001 — cosmetic only
            pass

    def build_about(self):
        page = self.section("About")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        logo = self._logo_image(260)
        if logo is not None:
            logo.set_margin_bottom(8)
            box.pack_start(logo, False, False, 0)
        # two-column layout: product info left, support/donations right ---
        cols = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=24)
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        left.pack_start(self.info_row("Product", f"{APP_NAME} v{VERSION}"),
                        False, False, 0)
        left.pack_start(self.info_row("Author", AUTHOR), False, False, 0)
        left.pack_start(self.info_row("Tagline", TAGLINE), False, False, 0)
        left.pack_start(self.info_row("Name origin", "Sranan Tongo (Suriname)"),
                        False, False, 0)
        name_why = Gtk.Label(
            label="In Sranan Tongo, the language of Suriname, 'horidoro' means "
                  "'to endure, to persevere, to hold on' — from hori ('to hold') "
                  "and doro ('door'). It is the word for what this app does: it "
                  "holds on, keeps protecting, and never gives up.",
            wrap=True, xalign=0.0)
        left.pack_start(name_why, False, False, 0)
        left.pack_start(self.info_row("Engine", ENGINE_CREDIT), False, False, 0)
        left.pack_start(self.info_row("License", "MIT (app & scripts)"),
                        False, False, 0)
        left.pack_start(self.info_row("Copyright", "© 2026 Commander Sabi"),
                        False, False, 0)
        left.pack_start(
            self.info_row("Privacy", "100% local — no data leaves your machine"),
            False, False, 0)
        why = Gtk.Label(
            label="Why Horidoro AV exists: antivirus protection should be simple. "
                  "This app was built to help people coming to Linux — one "
                  "double-click to install, no terminal, no config files to learn. "
                  "It protects what you choose, automatically and quietly, and "
                  "everything stays on your computer.",
            wrap=True, xalign=0.0)
        left.pack_start(why, False, False, 0)
        cols.pack_start(left, True, True, 0)

        # support / donations — right column ------------------------------
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        right.set_halign(Gtk.Align.START)
        right.set_valign(Gtk.Align.START)
        sup_frame = Gtk.Frame(label="  Support Horidoro AV  ")
        sup_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        for side in ("margin_start", "margin_end", "margin_top", "margin_bottom"):
            getattr(sup_box, "set_" + side)(8)
        sup_line = Gtk.Label(
            label="Horidoro AV is free and open-source. If it helps you, a "
                  "small donation helps free up time to keep improving it.",
            wrap=True, xalign=0.0)
        sup_box.pack_start(sup_line, False, False, 0)
        pbtn = Gtk.Button(label="Donate on Patreon")
        pbtn.connect("clicked", self.on_patreon_clicked)
        pbtn.set_halign(Gtk.Align.FILL)
        sup_box.pack_start(pbtn, False, False, 0)
        copy_usdc = Gtk.Button(
            label="Donate with crypto — copy USDC (Solana) address")
        copy_usdc.connect("clicked", self.on_copy_usdc)
        copy_usdc.set_halign(Gtk.Align.FILL)
        sup_box.pack_start(copy_usdc, False, False, 0)
        usdc_hint = Gtk.Label(
            label="Crypto donation — USDC on the Solana network only.",
            wrap=True, xalign=0.0)
        sup_box.pack_start(usdc_hint, False, False, 0)
        sup_frame.add(sup_box)
        right.pack_start(sup_frame, False, False, 0)
        cols.pack_start(right, False, False, 0)
        box.pack_start(cols, False, False, 0)
        # what's new -------------------------------------------------------
        # Shows the changelog for the CURRENT version; future releases add
        # their own entry and it appears here automatically.
        whatsnew = Gtk.Expander(label="What's new")
        wl = Gtk.Label(wrap=True, xalign=0.0)
        wl.set_markup(latest_changelog())  # markup, not literal <b>…</b>
        wl.set_margin_start(8)
        wl.set_margin_top(4)
        whatsnew.add(wl)
        box.pack_start(whatsnew, False, False, 0)
        # report a bug -----------------------------------------------------
        bug = Gtk.Button(label="Report a bug…")
        bug.connect("clicked", self.on_report_bug)
        box.pack_start(bug, False, False, 0)
        page.pack_start(box, False, False, 0)
        self._add_tab(page, "About")

    def on_patreon_clicked(self, _w):
        import shlex as _shlex
        run(f"xdg-open {_shlex.quote(PATREON_URL)} 2>/dev/null", timeout=15)

    def on_copy_usdc(self, _w):
        """Copy the USDC address, then show the address + a copy button in
        the popup (in case the first copy didn't take)."""
        def do_copy():
            try:
                Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(USDC_ADDRESS, -1)
            except Exception:  # noqa: BLE001 — clipboard is best-effort
                pass

        do_copy()
        dlg = Gtk.MessageDialog(transient_for=self, modal=True,
                                message_type=Gtk.MessageType.INFO,
                                buttons=Gtk.ButtonsType.NONE,
                                text="USDC (Solana) address copied to clipboard")
        dlg.format_secondary_text(
            "Send the USDC token on the Solana network only — sending it on "
            "another network can lose the funds.\n\n"
            f"Address:\n{USDC_ADDRESS}\n\n"
            "If the copy didn't work, press the button below to copy it again.")
        dlg.add_button("Close", Gtk.ResponseType.CLOSE)
        dlg.add_button("Copy address again", Gtk.ResponseType.YES)
        dlg.set_default_response(Gtk.ResponseType.CLOSE)
        if dlg.run() == Gtk.ResponseType.YES:
            do_copy()
        dlg.destroy()

    def on_report_bug(self, _w):
        """Build the sanitized local report (threaded: engine check may take a
        moment), then show the consent-first dialog."""
        if self._busy:
            return

        def work():
            return actions.build_bug_report(self.state)

        def done(report):
            self._show_bug_report_dialog(report)

        # NOTE: no progress= here — build_bug_report takes no progress arg
        # and _threaded passes it through when set (the TypeError that made
        # this button "do nothing, just give an error").
        self._threaded(work, done)

    def _show_bug_report_dialog(self, report):
        """Consent-first: shows EXACTLY what will be sent; nothing leaves the
        machine until the user presses Copy / Save / Open issue."""
        dlg = Gtk.Dialog(title="Report a bug", transient_for=self, modal=True)
        dlg.set_default_size(680, 560)
        area = dlg.get_content_area()
        area.set_spacing(8)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        for side in ("margin_start", "margin_end", "margin_top", "margin_bottom"):
            getattr(box, "set_" + side)(12)
        explain = Gtk.Label(
            label="This sends ONLY: app version, OS, system info (distrobox/"
                  "podman/engine state), install state, the last lines of "
                  "every app log, and (if an install failed) the install "
                  "diagnostics — plus what you type below. No files, no "
                  "personal data (your home path and username are removed)."
                  "\n\nNothing leaves your machine until you press Copy, "
                  "Save, or Open issue.",
            wrap=True, xalign=0.0)
        box.pack_start(explain, False, False, 0)

        desc_lab = Gtk.Label(label="What happened? (optional)", xalign=0.0)
        box.pack_start(desc_lab, False, False, 0)
        self._bug_desc = Gtk.TextView()
        self._bug_desc.set_wrap_mode(Gtk.WrapMode.WORD)
        dsw = Gtk.ScrolledWindow()
        dsw.set_min_content_height(90)
        dsw.add(self._bug_desc)
        box.pack_start(dsw, False, False, 0)

        exp = Gtk.Expander(label="Preview — exactly what will be sent")
        prev = Gtk.TextView()
        prev.set_editable(False)
        prev.set_wrap_mode(Gtk.WrapMode.WORD)
        prev.get_buffer().set_text(report)
        psw = Gtk.ScrolledWindow()
        psw.set_min_content_height(230)
        psw.add(prev)
        exp.add(psw)
        box.pack_start(exp, True, True, 0)

        def assemble():
            desc = ""
            tb = self._bug_desc.get_buffer()
            text = tb.get_text(tb.get_start_iter(), tb.get_end_iter(), False)
            if text.strip():
                desc = actions.sanitize_report("\n\n--- user description ---\n" + text)
            return (report + "\n" + desc) if desc else report

        def do_copy(_b):
            full = assemble()
            try:
                Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(full, -1)
            except Exception:  # noqa: BLE001 — clipboard is best-effort
                pass
            self.show_notice("Report a bug",
                             "Report copied to clipboard — paste it into a "
                             "GitHub issue.")
            dlg.destroy()

        def do_save(_b):
            fc = Gtk.FileChooserDialog("Save bug report", dlg,
                                       Gtk.FileChooserAction.SAVE,
                                       (Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                                        Gtk.STOCK_SAVE, Gtk.ResponseType.ACCEPT))
            fc.set_current_name("horidoro-bug-report.txt")
            if fc.run() == Gtk.ResponseType.ACCEPT:
                try:
                    with open(fc.get_filename(), "w", encoding="utf-8") as fh:
                        fh.write(assemble())
                    self.show_notice("Report a bug",
                                     f"Saved to {fc.get_filename()}")
                    dlg.destroy()
                except OSError as e:
                    self.show_notice("Report a bug", f"Could not save: {e}")
            fc.destroy()

        def do_issue(_b):
            import shlex as _shlex
            import urllib.parse as _urlparse
            full = assemble()
            url = (HOMEPAGE.rstrip("/") + "/issues/new?title=Horidoro+AV+bug+report&body="
                   + _urlparse.quote(full))
            run(f"xdg-open {_shlex.quote(url)} 2>/dev/null", timeout=15)
            dlg.destroy()

        brow = Gtk.Box(spacing=8)
        for label, fn in (("Copy to clipboard", do_copy),
                          ("Save to file…", do_save),
                          ("Open GitHub issue", do_issue),
                          ("Cancel", lambda b: dlg.destroy())):
            b = Gtk.Button(label=label)
            b.connect("clicked", fn)
            brow.pack_start(b, False, False, 0)
        box.pack_start(brow, False, False, 0)

        area.pack_start(box, True, True, 0)
        dlg.show_all()
        dlg.run()
        dlg.destroy()

    # --- handlers (wired to real actions in later iterations) --------------
    def on_browse(self, _w):
        dlg = Gtk.FileChooserDialog("Choose file or folder", self,
                                    Gtk.FileChooserAction.SELECT_FOLDER,
                                    (Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                                     Gtk.STOCK_OPEN, Gtk.ResponseType.OK))
        if dlg.run() == Gtk.ResponseType.OK:
            self.scan_entry.set_text(dlg.get_filename())
        dlg.destroy()

    def on_scan_clicked(self, btn):
        if not self._require_installed():
            return
        target = self.scan_entry.get_text().strip()
        if not target:
            return
        mode = "quick" if "Quick" in btn.get_label() else "verbose"
        self.scan_output.get_buffer().set_text(f"Scanning (mode: {mode}) — {target}\n\n")
        self.scan_spinner.start()
        self.scan_status.set_text("Scanning… this can take a while on large folders.")
        self.engine_spinner.start()
        self._status_engine.set_markup(
            "<span color='#2e9e4f'>✓ On — Scanning…</span>")

        def work():
            return actions.scan_path(target, mode)

        def done(out):
            self.scan_spinner.stop()
            self.engine_spinner.stop()
            self._status_engine.set_markup(
                "<span color='#2e9e4f'>✓ On — Idle</span>")
            text = out or ""
            if "another scan is already running" in text:
                self.scan_status.set_markup(
                    "<span color='#b36b00'>Skipped — another scan is "
                    "already running</span>")
            else:
                count = -1
                for line in text.splitlines():
                    if line.strip().startswith("Infected files:"):
                        try:
                            count = int(line.split(":")[1].strip())
                        except (ValueError, IndexError):
                            count = -1
                        break
                if count < 0:
                    self.scan_status.set_markup(
                        "<span color='#b36b00'>⚠ Scan failed — see the "
                        "output below</span>")
                    actions.play_sound("an-error-occurred", state=self.state)
                elif count == 0:
                    self.scan_status.set_markup(
                        "<span color='#2e9e4f'>✓ All clear — no threats found</span>")
                    actions.play_sound("scan-complete", state=self.state)
                else:
                    plural = "s" if count != 1 else ""
                    self.scan_status.set_markup(
                        f"<span color='#d43d2a'>⚠ {count} threat{plural} "
                        f"found — quarantined</span>")
                    actions.play_sound("threat-detected", state=self.state)
            buf = self.scan_output.get_buffer()
            buf.insert(buf.get_end_iter(), text[-40000:])

        self._threaded(work, done)

    def on_install_clicked(self, _w):
        if self._busy:
            return
        if self.state.is_installed():
            self.show_notice("Already installed",
                             "Horidoro AV is installed. Re-running verifies and repairs "
                             "everything — nothing is duplicated.")
        else:
            self._confirm(
                "Install Horidoro AV?",
                "This creates a 'clamav' container, installs ClamAV inside it, and sets up "
                "scanning, schedules, and right-click integration — all user-level and reversible.\n\n"
                "Provided as-is, no warranty, not guaranteed to detect all malware. "
                "You use it at your own risk.",
                self._do_install, yes_label="Install")

    def _do_install(self):
        def work(progress):
            install_all(self.state, progress=progress)
            return "Install complete — see the Dashboard.\n"

        def done(out):
            self.refresh_status()
            self.show_notice("Horidoro AV", out)

        self._threaded(work, done, progress=self._progress)

    def on_update_clicked(self, _w):
        if self._busy:
            return

        def work(progress):
            progress("Updating virus definitions… (first download takes a few minutes)")
            try:
                actions.update_db()
            except Exception:
                actions.play_sound("an-error-occurred", state=self.state)
                raise
            self.state.set("db_updated", True)
            progress("Virus definitions updated.")
            return "Virus definitions updated."

        def done(out):
            self.refresh_status()
            actions.play_sound("update-complete", state=self.state)
            self.show_notice("Update", out)

        self._threaded(work, done, progress=self._progress)

    def on_uninstall_clicked(self, _w):
        if self._busy:
            return
        dlg = Gtk.MessageDialog(transient_for=self, modal=True,
                                message_type=Gtk.MessageType.WARNING,
                                buttons=Gtk.ButtonsType.OK_CANCEL,
                                text="Uninstall Horidoro AV?")
        dlg.format_secondary_text(
            "This permanently deletes the app AND everything it created: "
            "settings, logs, quarantined files, sounds, schedules, and the "
            "engine container. This cannot be undone.\n\n"
            "Cancel if you might want any of it back.")
        dlg.set_default_response(Gtk.ResponseType.CANCEL)  # Enter is safe
        resp = dlg.run()
        if resp == Gtk.ResponseType.OK:
            def work(progress):
                uninstall_all(self.state, remove_container=True,
                              progress=progress)
                return "Uninstall complete — Horidoro AV and all its data " \
                       "are gone."
            self._threaded(work, self._do_uninstall, progress=self._progress)
        dlg.destroy()

    def _do_uninstall(self, out):
        self.refresh_status()
        self.show_notice("Horidoro AV", str(out))

    def _open_settings_tab(self, _w):
        page = self._tabs.get("Settings")
        if page is not None:
            self.notebook.set_current_page(self.notebook.page_num(page))

    def on_auto_update_toggle(self, switch, active):
        if active and not self.state.is_installed():
            # Reject via idle: switch snaps back to OFF, notice appears once
            # after this handler returns (no nested modal loop, no loop).
            GLib.idle_add(self._reject_switch, switch,
                          "Install Horidoro AV first — the Install button "
                          "is on the Dashboard.")
            return True
        auto = dict(self.state.get("auto_update") or {})
        auto["enabled"] = active
        self.state.set("auto_update", auto)
        self.refresh_summary()  # Dashboard row updates instantly
        if active:
            run("systemctl --user enable --now horidoro-update.timer 2>/dev/null")
        else:
            run("systemctl --user disable --now horidoro-update.timer 2>/dev/null")
        self.refresh_schedule_status()
        return False

    def on_sounds_toggle(self, switch, active):
        """Sound notifications toggle (default ON). Same reject-when-
        uninstalled guard as the other switches."""
        if active and not self.state.is_installed():
            GLib.idle_add(self._reject_switch, switch,
                          "Install Horidoro AV first — the Install button "
                          "is on the Dashboard.")
            return True
        self.state.set("sounds_enabled", active)
        if active:
            self.sounds_status.set_text(
                "On — plays on scan complete, threats, and updates")
        else:
            self.sounds_status.set_text("Off — everything silent")
        return False

    def on_app_update_toggle(self, switch, active):
        """Automatic app updates (default ON). Same reject-when-uninstalled
        guard as the other switches."""
        if active and not self.state.is_installed():
            GLib.idle_add(self._reject_switch, switch,
                          "Install Horidoro AV first — the Install button "
                          "is on the Dashboard.")
            return True
        self.state.set("auto_update_check", active)
        if active:
            run("systemctl --user enable --now horidoro-app-update.timer 2>/dev/null")
        else:
            run("systemctl --user disable --now horidoro-app-update.timer 2>/dev/null")
        return False

    def on_check_update_clicked(self, _w):
        if not updater.can_update():
            self.app_update_status.set_text(
                "Running from a dev copy — updates apply to the installed app "
                "(~/.local/bin/horidoro-av).")
            return
        self.app_update_status.set_text("Checking for updates…")

        def work():
            return updater.check_for_update()

        def done(result):
            status, release = result
            if status == "update":
                self._pending_update = release
                self.app_update_status.set_text(
                    f"New version {release.get('tag_name', '')} available — "
                    f"press 'Update now…'.")
            elif status == "current":
                self._pending_update = None
                self.app_update_status.set_text(f"Up to date (v{VERSION}).")
            else:
                self._pending_update = None
                self.app_update_status.set_text(
                    f"Couldn't reach GitHub — try again later.")

        self._threaded(work, done)

    def on_update_app_clicked(self, _w):
        if self._pending_update is None:
            self.app_update_status.set_text("Press 'Check now' first.")
            return
        tag = self._pending_update.get("tag_name", "")
        self._confirm(
            "Update Horidoro AV?",
            f"Download and install version {tag}? Your settings, quarantine, "
            "and logs are kept — only the app file is replaced. The download "
            "is verified against the published checksum before installing.",
            self._do_update_app, yes_label="Update")

    def _do_update_app(self):
        rel = self._pending_update

        def work():
            return updater.apply_update(rel)

        def done(ok):
            self._pending_update = None
            if ok:
                actions.play_sound("update-complete", state=self.state)
                self.show_notice(
                    "Horidoro AV",
                    "Update installed — restarting Horidoro AV now.")
                GLib.idle_add(self._relaunch_app)
            else:
                self.app_update_status.set_text(
                    "Update failed (download, checksum, or permissions) — "
                    "try again or download the zip from GitHub.")

        self._threaded(work, done, progress=self._progress)

    def _relaunch_app(self):
        import subprocess
        try:
            subprocess.Popen([str(APP_INSTALL_PATH)])
        except OSError:
            pass
        Gtk.main_quit()
        return False

    def _auto_check_update(self):
        """One silent background check; notice if an update exists."""
        if getattr(self, "_update_checking", False):
            return False
        self._update_checking = True

        def work():
            return updater.check_for_update()

        def done(result):
            self._update_checking = False
            status, release = result
            if status == "update":
                self.show_notice("Update available",
                                 f"Horidoro AV {release.get('tag_name', '')} "
                                 "is out — update it in Settings → App updates.")

        threading.Thread(target=lambda: GLib.idle_add(done, work()),
                         daemon=True).start()
        return False

    def _periodic_update_check(self):
        """Repeating timer (every 6h): return True to keep it alive."""
        self._auto_check_update()
        return True

    def _reject_switch(self, switch, msg):
        """Idle-only rejection: snap the switch back to OFF and explain why.
        Runs OUTSIDE the state-set emission, so nothing re-enters or loops and
        the visual state is guaranteed to return to off."""
        switch.set_active(False)
        self.show_notice("Install first", msg)
        return False  # one-shot idle

    # --- schedule toggles --------------------------------------------------
    def _schedule_guard(self, switch, active):
        if active and not self.state.is_installed():
            GLib.idle_add(self._reject_switch, switch,
                          "Scheduled scans need Horidoro installed "
                          "(Install on the Dashboard).")
            return False
        return True

    def on_daily_toggle(self, switch, active):
        if not self._schedule_guard(switch, active):
            return True  # reject the change — switch snaps back to off
        timers = list(self.state.get("timers_enabled") or [])
        if active and "horidoro-daily" not in timers:
            timers.append("horidoro-daily")
            run("systemctl --user enable --now horidoro-daily.timer 2>/dev/null")
        elif not active and "horidoro-daily" in timers:
            timers.remove("horidoro-daily")
            run("systemctl --user disable --now horidoro-daily.timer 2>/dev/null")
        self.state.set("timers_enabled", timers)
        self.refresh_summary()  # Dashboard row updates instantly
        self.refresh_schedule_status()
        return False

    def on_monthly_toggle(self, switch, active):
        if not self._schedule_guard(switch, active):
            return True  # reject the change — switch snaps back to off
        timers = list(self.state.get("timers_enabled") or [])
        if active and "horidoro-monthly" not in timers:
            timers.append("horidoro-monthly")
            run("systemctl --user enable --now horidoro-monthly.timer 2>/dev/null")
        elif not active and "horidoro-monthly" in timers:
            timers.remove("horidoro-monthly")
            run("systemctl --user disable --now horidoro-monthly.timer 2>/dev/null")
        self.state.set("timers_enabled", timers)
        self.refresh_summary()  # Dashboard row updates instantly
        self.refresh_schedule_status()
        return False

    # --- quarantine ----------------------------------------------------------
    def refresh_quarantine(self):
        self.quarantine_list.foreach(lambda w: self.quarantine_list.remove(w))
        for e in actions.list_quarantine():
            row = Gtk.ListBoxRow()
            cb = Gtk.CheckButton(
                label=f"{e['name']}   ({e['size']/1024:.0f} KB)")
            cb.set_active(False)
            row.add(cb)
            row._entry = e  # stash for the handlers
            row._cb = cb
            self.quarantine_list.add(row)
        self.quarantine_list.show_all()

    def _auto_refresh_quarantine(self):
        """Rebuild the list only when the entry count changed, so the user's
        selection is never stolen mid-click."""
        try:
            n = len(actions.list_quarantine())
        except Exception:  # noqa: BLE001
            return True
        if n != len(self.quarantine_list.get_children()):
            self.refresh_quarantine()
        return True

    def _auto_refresh_logs(self):
        """Reload the log only when the file changed (keeps scroll position)."""
        path = LOG_DIR / getattr(self, "_log_name", "daily_scan.log")
        try:
            m = path.stat().st_mtime if path.exists() else 0
        except OSError:
            return True
        if m != self._logs_mtime:
            self._logs_mtime = m
            self.refresh_logs()
        return True

    def _selected_quarantine(self):
        """Names of the quarantined files whose checkbox is ticked."""
        return [r._entry["name"] for r in self.quarantine_list.get_children()
                if getattr(r, "_cb", None) is not None and r._cb.get_active()]

    def on_restore_quarantined(self, _w):
        names = self._selected_quarantine()
        if not names:
            return
        origins = {actions.get_origin(n) for n in names}
        common_origin = origins.pop() if len(origins) == 1 else None
        if not common_origin:
            common_origin = None
        dlg = Gtk.Dialog(title="Restore", transient_for=self, modal=True)
        dlg.set_default_size(580, 200)
        area = dlg.get_content_area()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        for side in ("margin_start", "margin_end", "margin_top", "margin_bottom"):
            getattr(box, "set_" + side)(12)
        if common_origin:
            lab = Gtk.Label(
                label=f"Restore to its original location?\n\n{common_origin}\n\n"
                      "Or choose a different folder.",
                wrap=True, xalign=0.0)
        else:
            lab = Gtk.Label(
                label="The original location isn't recorded for this file — "
                      "choose a folder to restore to.",
                wrap=True, xalign=0.0)
        box.pack_start(lab, False, False, 0)
        cb = Gtk.CheckButton(
            label="Also add the restored location to Exclusions "
                  "(so it isn't flagged again)")
        cb.set_active(True)
        box.pack_start(cb, False, False, 0)
        area.pack_start(box, True, True, 0)
        dlg.add_button("Cancel", Gtk.ResponseType.CANCEL)
        if common_origin:
            dlg.add_button("Choose a folder…", Gtk.ResponseType.APPLY)
            dlg.add_button("Restore to original", Gtk.ResponseType.OK)
        else:
            dlg.add_button("Choose a folder…", Gtk.ResponseType.ACCEPT)
        resp = dlg.run()

        restored_paths = []
        if resp == Gtk.ResponseType.OK and common_origin:
            for n in names:
                p = actions.restore_quarantined_to_origin(n)
                if p:
                    restored_paths.append(p)
        elif resp in (Gtk.ResponseType.ACCEPT, Gtk.ResponseType.APPLY):
            fc = Gtk.FileChooserDialog("Restore to folder", dlg,
                                       Gtk.FileChooserAction.SELECT_FOLDER,
                                       (Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                                        Gtk.STOCK_OPEN, Gtk.ResponseType.OK))
            if fc.run() == Gtk.ResponseType.OK:
                dest = fc.get_filename()
                for n in names:
                    actions.restore_quarantined(n, dest)
                    restored_paths.append(os.path.join(dest, n))
            fc.destroy()
        dlg.destroy()
        if restored_paths and cb.get_active():
            self._add_exclusions(restored_paths)
        self.refresh_quarantine()
        self.refresh_status()

    def _add_exclusions(self, paths):
        """Add restored paths to the exclusion list (Settings shows them;
        removable there) and re-render the scan config."""
        ex = list(self.state.get("exclusions") or [])
        added = [p for p in paths if p not in ex]
        if not added:
            return
        ex += added
        self.state.set("exclusions", ex)
        for p in added:
            self._append_row(self.exclusion_list, p)

        def work(progress):
            apply_config(self.state, progress=progress)
            return "Exclusion added."

        def done(_out):
            self.refresh_status()

        self._threaded(work, done, progress=self._progress)

    def on_delete_quarantined(self, _w):
        names = self._selected_quarantine()
        if not names:
            return
        plural = "s" if len(names) > 1 else ""
        self._confirm(
            f"Delete {len(names)} quarantined file{plural}?",
            "They will be permanently deleted.",
            lambda: self._do_delete_quarantine(names), yes_label="Delete")

    def on_delete_all_quarantined(self, _w):
        names = [e["name"] for e in actions.list_quarantine()]
        if not names:
            return
        self._confirm(
            f"Delete ALL {len(names)} quarantined files?",
            "Everything in quarantine will be permanently deleted.",
            lambda: self._do_delete_quarantine(names), yes_label="Delete")

    def _do_delete_quarantine(self, names):
        for n in names:
            actions.delete_quarantined(n)
        self.refresh_quarantine()

    # --- logs ------------------------------------------------------------------
    def refresh_logs(self):
        name = getattr(self, "_log_name", "daily_scan.log")
        self.log_view.get_buffer().set_text(actions.read_log(name))

    def _on_log_chosen(self, _combo):
        idx = max(0, self.log_chooser.get_active())
        self._log_name = self._log_choices[idx][1]
        self._logs_mtime = 0  # force a fresh read of the newly chosen log
        self.refresh_logs()

    def on_delete_log_clicked(self, _w):
        idx = max(0, self.log_chooser.get_active())
        label, fname = self._log_choices[idx]
        self._confirm(
            "Delete this log?",
            f"Permanently delete the '{label}' log?\n\n"
            "This removes this log's scan history. Quarantined files keep "
            "their recorded original locations separately (for restore-to-"
            "origin), so current files are unaffected — only older history "
            "that predates that record is lost.",
            lambda: self._do_delete_log(fname), yes_label="Delete")

    def _do_delete_log(self, fname):
        actions.delete_log(fname)
        self._logs_mtime = 0
        self.refresh_logs()

    # --- signature databases ----------------------------------------------------
    def _auto_refresh_dbs(self):
        """Reload the databases list when the engine's DB set changed (new
        files added/removed), without stealing the selection — and WITHOUT
        blocking the GUI (the container call runs on a background thread)."""
        if getattr(self, "_db_checking", False):
            return True
        self._db_checking = True

        def work():
            try:
                return len(actions.list_dbs())
            except Exception:  # noqa: BLE001
                return None

        def done(n):
            self._db_checking = False
            if n is not None and n != len(self.db_list.get_children()):
                self.refresh_dbs()
            return False

        threading.Thread(target=lambda: GLib.idle_add(done, work()),
                         daemon=True).start()
        return True

    def refresh_dbs(self):
        """Rebuild the DB list WITHOUT blocking the GUI: the container call
        runs on a background thread (a slow or wedged distrobox must never
        freeze startup or the interface)."""
        if getattr(self, "_db_refreshing", False):
            return
        self._db_refreshing = True

        def work():
            try:
                return actions.list_dbs()
            except Exception:  # noqa: BLE001 — read-only best effort
                return None

        def done(names):
            self._db_refreshing = False
            if names is None:
                return
            self.db_list.foreach(lambda w: self.db_list.remove(w))
            for name in names:
                row = Gtk.ListBoxRow()
                row.add(Gtk.Label(label=name, xalign=0.0))
                row._name = name
                self.db_list.add(row)
            self.db_list.show_all()

        threading.Thread(target=lambda: GLib.idle_add(done, work()),
                         daemon=True).start()

    def on_db_add(self, _w):
        if self._busy or not self._require_installed():
            return
        dlg = Gtk.FileChooserDialog("Add signature database", self,
                                    Gtk.FileChooserAction.OPEN,
                                    (Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                                     Gtk.STOCK_OPEN, Gtk.ResponseType.OK))
        if dlg.run() == Gtk.ResponseType.OK:
            path = dlg.get_filename()
            self._db_op_start("Copying database file…")

            def work():
                actions.add_db(path)
                return (f"Database copied: {os.path.basename(path)}. "
                        "It loads when the engine next starts.")

            def done(out):
                GLib.idle_add(self._db_op_finish)
                self.refresh_dbs()
                self.refresh_status()
                self.show_notice("Signature databases", out)

            self._threaded(work, done)
        dlg.destroy()

    def _db_op_start(self, msg):
        self.db_spinner.start()
        self.db_status.set_text(msg)

    def _db_op_finish(self):
        self.db_spinner.stop()
        self.db_status.set_text("")
        return False

    def on_db_add_url(self, _w):
        if self._busy or not self._require_installed():
            return
        self._prompt_text(
            "Add database by URL",
            "Paste the download URL of a signature database feed. Horidoro will "
            "subscribe to it so it stays updated automatically with every "
            "definition update.\nExample: https://example.com/feed.ndb",
            self._do_db_add_url)

    def _do_db_add_url(self, url):
        self._db_op_start("Adding database URL…")

        def work():
            actions.add_db_url(url)
            return ("Database URL added — it will update automatically with "
                    "every definition update.")

        def done(out):
            GLib.idle_add(self._db_op_finish)
            self.refresh_dbs()
            self.refresh_status()
            self.show_notice("Signature databases", out)

        self._threaded(work, done)

    def on_engine_check(self, _w):
        if self._busy or not self._require_installed():
            return
        self.engine_update_label.set_text("Checking for engine updates…")

        def work():
            return actions.engine_update_status()

        def done(info):
            if info is None:
                self.engine_update_label.set_text(
                    "Could not check for updates (engine unreachable).")
            elif info["update"]:
                self.engine_update_label.set_text(
                    f"Installed: {info['installed']} — update available: "
                    f"{info['available']}. Use 'Update engine…' to apply it.")
            else:
                self.engine_update_label.set_text(
                    f"Installed: {info['installed']} — up to date.")

        self._threaded(work, done)

    def on_engine_update(self, _w):
        if self._busy or not self._require_installed():
            return
        self._confirm(
            "Update the ClamAV engine?",
            "Upgrades the engine inside the container to the latest version. "
            "Usually safe, but a major update could change behavior — if "
            "anything misbehaves afterward, the configs are in "
            "~/.local/share/horidoro/scripts and /etc/clamd.d/scan.conf. Continue?",
            self._do_engine_update, yes_label="Update")

    def _do_engine_update(self):
        self.engine_update_label.set_text("Updating engine… (a few minutes)")

        def work(progress):
            actions.update_engine(progress=progress)
            return "Engine updated."

        def done(out):
            self.refresh_status()
            self.engine_update_label.set_text(
                "Engine updated — check again for the new version.")
            self.show_notice("Engine", out)

        self._threaded(work, done, progress=self._progress)

    def on_db_thirdparty(self, _w):
        if self._busy or not self._require_installed():
            return
        self._confirm(
            "Add recommended databases?",
            "Adds the extra detection feeds RECOMMENDED BY THE HORIDORO AV AUTHOR "
            "(rfxn, urlhaus, sanesecurity, blurl, junk, shell) — well-known "
            "community databases that complement the official ClamAV ones — "
            "and downloads them. Takes a couple of minutes.",
            self._do_db_thirdparty, yes_label="Add")

    def _do_db_thirdparty(self):
        self._db_op_start("Adding recommended databases — this can take a "
                          "couple of minutes…")

        def work(progress):
            def both(msg):
                progress(msg)
                GLib.idle_add(lambda: (self.db_status.set_text(msg), False)[1])
            actions.add_third_party_dbs(progress=both)
            return "Recommended databases added and downloaded."

        def done(out):
            GLib.idle_add(self._db_op_finish)
            self.refresh_dbs()
            self.refresh_status()
            self.show_notice("Signature databases", out)

        self._threaded(work, done, progress=self._progress)

    def on_db_remove(self, _w):
        if not self._require_installed():
            return
        row = self.db_list.get_selected_row()
        if row is None:
            return
        name = row._name
        self._confirm(
            "Remove signature database?",
            f"'{name}' will be deleted from the engine and its detection rules "
            "stop applying (takes effect when the engine next starts). This does "
            "not uninstall Horidoro — it only removes this one database file.",
            lambda: self._do_db_remove(name), yes_label="Remove")

    def _do_db_remove(self, name):
        self._db_op_start(f"Removing {name}…")

        def work():
            actions.remove_db(name)
            return f"Removed: {name}"

        def done(out):
            GLib.idle_add(self._db_op_finish)
            self.refresh_dbs()
            self.refresh_status()
            self.show_notice("Signature databases", out)

        self._threaded(work, done)

    # --- helpers ----------------------------------------------------------------
    def _confirm(self, title, msg, on_yes, yes_label="OK"):
        """Confirmation dialog for destructive/important actions. Uses
        EXPLICIT text buttons — the YES_NO stock buttons misbehave on some
        Plasma/GTK setups (clicks do nothing, only Escape closes the dialog).
        Default response is Cancel, so Enter never confirms."""
        dlg = Gtk.MessageDialog(transient_for=self, modal=True,
                                message_type=Gtk.MessageType.WARNING,
                                buttons=Gtk.ButtonsType.NONE, text=title)
        dlg.format_secondary_text(msg)
        dlg.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dlg.add_button(yes_label, Gtk.ResponseType.YES)
        dlg.set_default_response(Gtk.ResponseType.CANCEL)
        if dlg.run() == Gtk.ResponseType.YES:
            on_yes()
        dlg.destroy()

    def _threaded(self, work, on_done, progress=None):
        self._busy = True
        self.progress_spinner.start()

        def runner():
            try:
                result = work(progress) if progress is not None else work()
                GLib.idle_add(on_done, result)
            except Exception as e:  # noqa: BLE001 — surface errors to the GUI
                GLib.idle_add(self.show_notice, "Error", str(e))
            finally:
                GLib.idle_add(self._operation_finished)
        threading.Thread(target=runner, daemon=True).start()

    def _operation_finished(self):
        self._busy = False
        self.progress_spinner.stop()
        return False  # one-shot idle callback

    def _require_installed(self):
        """Show an 'Install first' notice when the app isn't installed yet.
        Returns True when it's safe to proceed."""
        if not self.state.is_installed():
            self.show_notice("Install first",
                             "Install Horidoro AV first — the Install button "
                             "is on the Dashboard.")
            return False
        return True

    def _progress(self, msg):
        """Thread-safe live status line (must be called from the main thread)."""
        GLib.idle_add(self._set_progress, msg)

    def _set_progress(self, msg):
        self.progress_label.set_text(str(msg))
        self.refresh_summary()  # checkmarks move live as install steps land
        return False  # one-shot idle callback

    def show_notice(self, title, msg):
        dlg = Gtk.MessageDialog(transient_for=self, modal=True,
                                message_type=Gtk.MessageType.INFO,
                                buttons=Gtk.ButtonsType.OK, text=title)
        dlg.format_secondary_text(msg)
        dlg.run()
        dlg.destroy()


def main():
    # headless on-access mode (horidoro-watcher.service runs this)
    if "--watch" in sys.argv:
        extra = [a for a in sys.argv[1:] if a != "--watch"]
        summary = actions.run_watcher_forever(State(), extra_folders=extra)
        print(summary)
        # Exit non-zero when the engine couldn't start so systemd's
        # Restart=on-failure re-tries (bounded by the unit's StartLimit).
        raise SystemExit(1 if summary.startswith("Engine failed") else 0)
    # headless app-update check (horidoro-app-update.timer runs this):
    # downloads + verifies + swaps + restarts the watcher, all with sounds
    if "--update-check" in sys.argv:
        status, release = updater.check_for_update()
        if status == "update":
            if updater.apply_update(release):
                actions.play_sound("update-complete", state=State())
                print(f"Updated to {release.get('tag_name', '')}.")
            else:
                actions.play_sound("an-error-occurred", state=State())
                print("Update failed (download, checksum, or permissions).")
        elif status == "current":
            print("Already up to date.")
        else:
            print("Could not reach GitHub.")
        raise SystemExit(0)
    if not HAVE_GTK:
        print(f"{APP_NAME} needs GTK3 (python3-gobject). Install it and retry.")
        raise SystemExit(1)
    state = State()
    # First run: register the apps-menu entry so it's launchable like any app.
    ensure_self_installed(state)
    # Self-heal right-click integration + script exec bits on every launch
    # (no manual commands, no re-install needed after a partial/stale setup).
    repair_integration(state)
    if Gdk.Display.get_default() is None:
        print(f"{APP_NAME} needs a graphical session (cannot open display).",
              file=sys.stderr)
        raise SystemExit(1)
    window = MainWindow(state)
    window.show_all()
    if state.get("auto_update_check", True) and updater.can_update():
        GLib.timeout_add(2500, window._auto_check_update)      # first check
        # then keep checking while the app stays open (a machine that never
        # restarts still learns about updates)
        GLib.timeout_add_seconds(6 * 3600, window._periodic_update_check)
    Gtk.main()
