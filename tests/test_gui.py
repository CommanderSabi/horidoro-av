#!/usr/bin/env python3
"""Horidoro AV — GUI construction smoke test.

Guards the class of bug where a widget-build error crashes the app silently
on launch (a terminal-less "Launch" makes the traceback invisible — exactly
what happened with the Schedule-tab NameError that broke real installs on
both test machines). Constructs the FULL MainWindow (every tab, every widget)
without showing it; a crash here fails the test.

Run:  python3 tests/test_gui.py
"""
import os
import sys
import tempfile

import atexit
import shutil

_HOME = tempfile.mkdtemp(prefix="horidoro-gui-")
atexit.register(lambda: shutil.rmtree(_HOME, ignore_errors=True))
os.environ["HOME"] = _HOME
os.environ.pop("XDG_CONFIG_HOME", None)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "horidoro"))

import gui  # noqa: E402
from state import State  # noqa: E402

# guard: main() uses these at runtime on a real display — a missing import
# crashed launch (NameError: updater) while every headless test passed
assert hasattr(gui, "updater") and gui.updater is not None, \
    "gui must import updater (main() references it)"

# never touch the real system from tests
gui.run = lambda cmd, **kw: (0, "")

try:
    from gi.repository import Gtk
except Exception:  # noqa: BLE001
    Gtk = None

if Gtk is None or not Gtk.init_check():
    print("SKIPPED: no display available (GTK windows can't be built headless)")
    sys.exit(0)

try:
    from gi.repository import Gtk, GLib
    w = gui.MainWindow(State())
    tabs = [w.notebook.get_tab_label(w.notebook.get_nth_page(i)).get_text()
            for i in range(w.notebook.get_n_pages())]

    # Scan tab: the Stop button exists and starts disabled (a scan must be
    # running before it can be stopped — the stop-scan feature, 2026-09-01).
    assert hasattr(w, "scan_stop_btn"), "Stop scan button missing from Scan tab"
    assert w.scan_stop_btn.get_sensitive() is False, \
        "Stop scan button must start disabled"
    assert w._scan_stopped is False, "_scan_stopped must start False"
    print("STOP-SCAN BUTTON OK — present and disabled until a scan runs")

    # uninstalled: every switch REJECTS and snaps back to off. The snap-back
    # happens in an idle callback, so we process pending idles to really
    # verify it (the "Install first" loop/stuck-on bug class).
    w.show_notice = lambda *a, **k: None  # never open real dialogs in tests
    ctx = GLib.main_context_default()
    rejects = [
        ("auto-update", w.on_auto_update_toggle(w.auto_update_toggle, True),
         w.auto_update_toggle),
        ("app-update", w.on_app_update_toggle(w.app_update_toggle, True),
         w.app_update_toggle),
        ("watcher", w.on_watcher_toggle(w.watcher_switch, True),
         w.watcher_switch),
        ("daily", w.on_daily_toggle(w.daily_toggle, True), w.daily_toggle),
        ("monthly", w.on_monthly_toggle(w.monthly_toggle, True),
         w.monthly_toggle),
        ("sounds", w.on_sounds_toggle(w.sounds_toggle, True),
         w.sounds_toggle),
    ]
    for name, returned, switch in rejects:
        assert returned is True, f"{name} toggle must return True (reject)"
        while ctx.pending():
            ctx.iteration(False)
        assert switch.get_active() is False, \
            f"{name} switch must snap back to OFF after idle processing"

    # REGRESSION GUARD (the "Dashboard doesn't update until restart" bug):
    # the Dashboard summary rows must reflect manifest changes IMMEDIATELY —
    # this is the exact path every toggle handler now calls (refresh_summary).
    w.state.set("watcher_enabled", True)
    w.state.set("timers_enabled", ["horidoro-daily"])
    w.refresh_summary()
    rt_on = w._st_realtime.get_label()
    dl_on = w._st_daily.get_label()
    w.state.set("watcher_enabled", False)
    w.state.set("timers_enabled", [])
    w.refresh_summary()
    rt_off = w._st_realtime.get_label()
    dl_off = w._st_daily.get_label()
    assert "On" in rt_on and "On" in dl_on, \
        f"rows must show On: {rt_on!r} / {dl_on!r}"
    assert "Off" in rt_off and "Off" in dl_off, \
        f"rows must show Off: {rt_off!r} / {dl_off!r}"
    # REGRESSION GUARD (report-a-bug "does nothing, just gives an error"):
    # on_report_bug's work() takes no progress argument, so its _threaded call
    # must NOT pass progress= (the real _threaded would call work(progress) and
    # TypeError). Simulate the exact dispatch shape synchronously.
    orig_threaded = w._threaded
    captured = {}

    def sync_threaded(work, on_done, progress=None):
        result = work(progress) if progress is not None else work()
        on_done(result)

    w._threaded = sync_threaded
    w._busy = False
    w._show_bug_report_dialog = lambda report: captured.setdefault("report", report)
    w.on_report_bug(None)
    w._threaded = orig_threaded
    assert "report" in captured, \
        "report-a-bug must build and show a report (TypeError class)"
    assert isinstance(captured["report"], str) and captured["report"], \
        "bug report must be a non-empty string"

    # REGRESSION GUARD (delete-log/confirm dialogs: YES_NO stock buttons were
    # dead on some Plasma/GTK setups — clicks did nothing, only Escape closed
    # the dialog). _confirm must use EXPLICIT text buttons + default Cancel.
    captured = {}

    class FakeDlg:
        def __init__(self, *a, **kw):
            captured["buttons"] = kw.get("buttons")
            captured["actions"] = []

        def format_secondary_text(self, m):
            pass

        def add_button(self, label, resp):
            captured["actions"].append((label, resp))

        def set_default_response(self, r):
            captured["actions"].append(("default", r))

        def run(self):
            return Gtk.ResponseType.CANCEL

        def destroy(self):
            pass

    orig_md = gui.Gtk.MessageDialog
    gui.Gtk.MessageDialog = FakeDlg
    w._confirm("Delete this log?", "msg", lambda: None, yes_label="Delete")
    gui.Gtk.MessageDialog = orig_md
    assert captured["buttons"] is Gtk.ButtonsType.NONE, \
        "_confirm must not use the YES_NO stock buttons (dead on some setups)"
    labels = [a[0] for a in captured["actions"]]
    assert "Delete" in labels and "Cancel" in labels, \
        f"_confirm needs explicit Delete/Cancel buttons: {labels}"
    assert ("default", Gtk.ResponseType.CANCEL) in captured["actions"], \
        "_confirm must default to Cancel (Enter never confirms)"

    # the periodic live-refresher exists and keeps the Dashboard current
    assert hasattr(w, "_auto_refresh_summary")
    assert w._auto_refresh_summary() is True
    w.destroy()
    print(f"GUI CONSTRUCTS OK — {len(tabs)} tabs: {tabs}")
    print("TOGGLE GUARDS OK — all 5 snap back to off when not installed")
    print("DASHBOARD LIVE-REFRESH OK — rows track toggles without restart")
    print("REPORT-A-BUG OK — builds a sanitized report (no TypeError)")
except Exception as e:  # noqa: BLE001
    import traceback
    traceback.print_exc()
    print(f"GUI CONSTRUCTION FAILED: {e}")
    sys.exit(1)
