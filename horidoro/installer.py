# -*- coding: utf-8 -*-
"""Horidoro AV — installer / uninstaller.

Install = folders -> container -> packages -> scan.conf -> scripts -> timers
        -> sudo rule -> right-click integration -> app menu entry -> DB update
        -> EICAR self-test. Manifest-tracked and idempotent: re-running
        verifies/repairs, never duplicates.

Uninstall reverses the manifest and ASKS before touching user data
(quarantine contents). The app only ever manages what it created itself —
it never inspects or touches anything pre-existing on the system.
"""

import base64
import getpass
import os
import re
import shlex
import shutil
import sys
from pathlib import Path

from branding import (APP_NAME, CONFIG_DIR, CONTAINER_IMAGE, CONTAINER_NAME,
                      DOCS_DIR, PACKAGES, VERSION)
from shell import distrobox, in_container, run
from state import DEFAULT_STATE
import templates

HOME = os.path.realpath(str(Path.home()))          # /var/home/USER on Fedora Atomic
USER = getpass.getuser()
DATA_DIR = Path(HOME) / ".local/share/horidoro"
LOG_DIR = DATA_DIR / "logs"
QUARANTINE_DIR = DATA_DIR / "quarantine"
SCRIPT_DIR = DATA_DIR / "scripts"
TMP_DIR = DATA_DIR / "tmp"
SOUNDS_DIR = DATA_DIR / "sounds"
CONF = "/etc/clamd.d/scan.conf"
APP_INSTALL_PATH = Path(HOME) / ".local/bin/horidoro-av"
TIMERS = ["horidoro-daily", "horidoro-monthly"]

DEFAULT_SCHEDULE = {
    "daily": {"time": "02:00", "days": "Tue,Wed,Thu,Fri,Sat,Sun"},
    "monthly": {"time": "02:00", "day": "1"},
}

class InstallError(Exception):
    pass


# ---------------------------------------------------------------------------
# rendering helpers
# ---------------------------------------------------------------------------
def _tokens(paths_daily, paths_monthly, exclusions, schedule=None):
    sched = schedule or DEFAULT_SCHEDULE
    return {
        "HOME": HOME,
        "USER": USER,
        "DOCS_DIR": DOCS_DIR,                  # ~/.local/share/horidoro
        "SOUNDS_DIR": SOUNDS_DIR,
        "LOG_DIR": LOG_DIR,
        "QUARANTINE_DIR": QUARANTINE_DIR,
        "SCRIPT_DIR": SCRIPT_DIR,
        "TMP_DIR": TMP_DIR,
        "CONTAINER": CONTAINER_NAME,
        "CONF": CONF,
        "APP_PATH": APP_INSTALL_PATH,
        "EXCLUSIONS": "\n".join(f"ExcludePath {e}" for e in exclusions),
        "DAILY_PATHS": _path_lines(paths_daily),
        "MONTHLY_PATHS": _path_lines(paths_monthly),
        "DAILY_PATHS_CLEAN": "\n".join(str(p) for p in paths_daily),
        "MONTHLY_PATHS_CLEAN": "\n".join(str(p) for p in paths_monthly),
        "DAILY_PATHS_SET": "1" if paths_daily else "0",
        "MONTHLY_PATHS_SET": "1" if paths_monthly else "0",
        "DAILY_CALENDAR": _daily_calendar(sched.get("daily", {})),
        "MONTHLY_CALENDAR": _monthly_calendar(sched.get("monthly", {})),
        "UPDATE_CALENDAR": f"*-*-* {_calendar_time((sched.get('auto_update') or {}).get('time'))}:00",
    }


def _calendar_time(value):
    """Normalize an HH:MM entry; fall back to 02:00 on garbage."""
    t = str(value or "").strip()
    if re.fullmatch(r"(?:[01]?\d|2[0-3]):[0-5]\d", t):
        return t
    return "02:00"


def _daily_calendar(daily):
    days = str(daily.get("days") or DEFAULT_SCHEDULE["daily"]["days"]).strip()
    return f"{days} *-*-* {_calendar_time(daily.get('time'))}:00"


def _monthly_calendar(monthly):
    dom = str(monthly.get("day") or "1").strip()
    if not dom.isdigit() or not (1 <= int(dom) <= 28):
        dom = "1"
    return f"*-*-{dom} {_calendar_time(monthly.get('time'))}:00"


def _path_lines(paths, indent="              "):
    """Format a scan path list for script insertion (quote-armored).
    Empty list -> empty string (no implicit default: the user chooses what
    gets scanned; scripts skip with a notification when nothing is set).

    CRITICAL: the output lands INSIDE the generated scripts' double-quoted
    `bash -c "..."` distrobox payload, so the path must survive TWO shells:
      - the HOST shell (the payload is a double-quoted string: $, `, \\, "
        must be escaped for it)
      - the CONTAINER's bash (single-quote the path so $, `, \\ are literal;
        the '"';"' idiom carries a literal single quote through safely)
    An unescaped quote terminates the outer string and collapses the whole
    payload (the recurring CRITICAL BUG #3 class — found live in the
    daily/monthly scans: "syntax error: unexpected end of file from 'if'")."""
    paths = [str(p) for p in paths if p]
    lines = []
    for i, p in enumerate(paths):
        end = " \\" if i < len(paths) - 1 else ""
        inner = "'" + p.replace("'", "'\"'\"'") + "'"  # single-quoted for the inner shell
        safe = (inner.replace("\\", "\\\\").replace('"', '\\"')
                 .replace("$", "\\$").replace("`", "\\`"))  # host-shell escaping
        lines.append(f'{indent}{safe}{end}')
    return "\n".join(lines)


def _render(template, tokens):
    out = template
    for key, value in tokens.items():
        out = out.replace(f"@{key}@", str(value))
    return out


def _default_paths(include_mounts=False):
    """Sensible per-user defaults: home (+ detected mounted drives)."""
    paths = [HOME]
    if include_mounts:
        paths += mounted_drives()
    return paths


def mounted_drives():
    """User data drives mounted under /var/mnt or /run/media (deduped)."""
    drives = []
    rc, out = run("mount")
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 3:
            mnt = parts[2]
            if mnt.startswith("/var/mnt/") or mnt.startswith("/run/media/"):
                if mnt not in drives:
                    drives.append(mnt)
    return drives


# ---------------------------------------------------------------------------
# container sudo bootstrap (passwordless, GUI-safe)
# ---------------------------------------------------------------------------
_BOOTSTRAP_DIAG = []  # (strategy, rc, output) from the last bootstrap attempt


def _bootstrap_container_sudo(rule, verify):
    """Try each passwordless strategy; after each, `verify()` must return
    True (the caller runs `sudo -n true` inside the container). A strategy
    that writes the rule but doesn't make sudo accept it is NOT a success —
    the rule must actually take effect. Returns True on the first VERIFIED
    win, False when nothing works.

    Why exec-as-root comes first: in a rootless container the host user maps
    to the CONTAINER USER (same uid), so a file written via a rootfs mount is
    user-owned inside the container — and sudo silently IGNORES sudoers.d
    files that aren't root-owned (found on Aurora: rc=0 write, yet "sudo: a
    password is required"). `podman exec -u 0` writes as the container's
    REAL root, so the rule lands root-owned and sudo accepts it.

    The rule is written for the sudo/wheel GROUPS (not a username):
    distrobox-init adds the container user to those groups, so the rule
    matches whatever the machine's username is — a username mismatch would
    otherwise make sudo silently ignore it.

    Strategies, in order:
      1. rootless podman: exec as root inside the (started) container,
         after waiting for distrobox-init to finish (sudo installed AND the
         container user created — a race here made the first check pass as
         root before the user existed, then fail for the real user)
      2. rootless podman: write through the container's rootfs mount
      3. host with passwordless sudo (Universal Blue-style hosts) — last
         resort, only works where the host genuinely allows it
    Every attempt is recorded in _BOOTSTRAP_DIAG so a failure can be
    diagnosed instead of guessed at."""
    diag = []
    sudoers = (f'echo {shlex.quote(rule)} > /etc/sudoers.d/horidoro-av '
               f'&& chmod 440 /etc/sudoers.d/horidoro-av')
    mount_sudoers = (f'echo {shlex.quote(rule)} > "$M/etc/sudoers.d/horidoro-av" '
                     f'&& chmod 440 "$M/etc/sudoers.d/horidoro-av"')
    mount_script = (f'M=$(podman mount {CONTAINER_NAME}) || exit 1\n'
                    f'{mount_sudoers}\n'
                    f'podman unmount {CONTAINER_NAME}\n')
    strategies = [
        # 1. exec as REAL root, after distrobox-init finishes (up to 3 min):
        #    wait until sudo exists AND the container user exists
        #    (id -u <host user> succeeds = init created it).
        ("rootless-exec",
         f"podman start {CONTAINER_NAME} 2>/dev/null; "
         f"for i in $(seq 1 180); do "
         f"podman exec -u 0 {CONTAINER_NAME} bash -lc "
         f"'test -x /usr/bin/sudo && id -u {USER} >/dev/null 2>&1 "
         f"&& [ -f /etc/sudoers.d/sudoers ]' "
         f"2>/dev/null && break; sleep 1; done; "
         f"podman exec -u 0 {CONTAINER_NAME} bash -lc "
         f"{shlex.quote(sudoers)}"),
        ("rootless-mount",
         f"podman unshare bash -lc {shlex.quote(mount_script)}"),
        ("host-sudo",
         f"sudo distrobox enter -n {CONTAINER_NAME} -- bash -lc "
         f"{shlex.quote(sudoers)}"),
    ]
    for name, cmd in strategies:
        rc, out = run(cmd, timeout=240)
        diag.append((name, rc, out[-300:]))
        if rc == 0 and verify():
            _BOOTSTRAP_DIAG[:] = diag
            return True
    _BOOTSTRAP_DIAG[:] = diag
    return False


def _remove_container_sudoers():
    """Best-effort removal of the rule we may have written — a failed
    bootstrap must never leave the container's sudo half-broken."""
    rm = "rm -f /etc/sudoers.d/horidoro-av"
    script = (f'M=$(podman mount {CONTAINER_NAME}) 2>/dev/null || exit 0\n'
              f'rm -f "$M/etc/sudoers.d/horidoro-av" 2>/dev/null\n'
              f'podman unmount {CONTAINER_NAME} 2>/dev/null\n')
    run(f"podman unshare bash -lc {shlex.quote(script)}", timeout=60)
    run(f"podman exec -u 0 {CONTAINER_NAME} bash -lc {shlex.quote(rm)} "
        f"2>/dev/null", timeout=60)
    run(f"sudo distrobox enter -n {CONTAINER_NAME} -- bash -lc "
        f"{shlex.quote(rm)} 2>/dev/null", timeout=60)


def _dump_install_diagnostics(state):
    """Write /tmp/horidoro-install-debug.log with EVERYTHING known about a
    failed install — distrobox/podman versions, whether the container is
    rootless or rootful, the sudo check output, and every bootstrap attempt.
    Written BEFORE the rollback (to /tmp, which cleanup never touches) so the
    reason is never destroyed with the cleanup."""
    try:
        lines = ["HORIDORO INSTALL DEBUG", "=" * 30]
        for cmd, t in (("distrobox --version", 30),
                       ("podman --version", 30),
                       ("podman container exists " + CONTAINER_NAME +
                        " && echo container-exists || echo container-missing", 30)):
            rc, out = run(cmd, timeout=t)
            lines.append(f"$ {cmd} -> rc={rc}\n{out.strip()}")
        rc, out = run("podman info 2>/dev/null | grep -iE 'rootless|store|driver' | head -5",
                      timeout=30)
        lines.append(f"$ podman info (rootless/rootful?) -> rc={rc}\n{out.strip()}")
        rc, out = distrobox("sudo -n true 2>&1", timeout=60)
        lines.append(f"$ sudo -n true inside container -> rc={rc}\n{out.strip()}")
        rc, out = distrobox("id -un 2>&1", timeout=60)
        lines.append(f"$ container user (id -un) -> rc={rc}\n{out.strip()}")
        rc, out = run(
            f"podman exec -u 0 {CONTAINER_NAME} bash -lc "
            f"'ls -ln /etc/sudoers.d/ 2>&1; echo ---; "
            f"cat /etc/sudoers.d/horidoro-av 2>&1' 2>/dev/null", timeout=60)
        lines.append(f"$ rule file state (root view) -> rc={rc}\n{out.strip()}")
        lines.append("bootstrap attempts:")
        for name, rc, out in _BOOTSTRAP_DIAG:
            lines.append(f"  {name}: rc={rc} -> {out.strip()}")
        if _BOOTSTRAP_DIAG:
            _BOOTSTRAP_DIAG.clear()
        Path("/tmp/horidoro-install-debug.log").write_text("\n\n".join(lines))
    except Exception:  # noqa: BLE001 — diagnostics must never crash the error
        pass


# ---------------------------------------------------------------------------
# install
# ---------------------------------------------------------------------------
def install_all(state, progress=None):
    """Full install (see _install_all). If ANY step fails, the app rolls back
    everything it created or changed — container, folders, timers, files,
    icons, state — exactly like the manual cleanup commands, so no broken
    half-install is ever left behind. The error is then shown normally, with
    a diagnostics log saved first (see _dump_install_diagnostics)."""
    try:
        return _install_all(state, progress)
    except InstallError:
        _dump_install_diagnostics(state)
        try:
            # keep_app: the running app's own launcher file survives the
            # rollback — deleting it left desktop shortcuts pointing at a
            # missing file after the user retried ("Could not find the
            # program …/horidoro-av").
            uninstall_all(state, progress=progress, keep_app=True)
        except Exception:  # noqa: BLE001 — cleanup must never mask the error
            pass
        raise


def _install_all(state, progress=None):
    """Full install. progress(msg) is an optional GUI callback."""
    def p(msg):
        if progress:
            progress(msg)

    paths_daily = state.get("scan_paths", {}).get("daily") or []
    paths_monthly = state.get("scan_paths", {}).get("monthly") or []
    exclusions = state.get("exclusions") or []
    tokens = _tokens(paths_daily, paths_monthly, exclusions, state.get("schedule"))

    # 1. folder skeleton ----------------------------------------------------
    p("Step 1/12 — Setting up Horidoro's folders…")
    shutil.rmtree(TMP_DIR, ignore_errors=True)  # self-clean: no half-install junk
    for d in (LOG_DIR, QUARANTINE_DIR, SCRIPT_DIR, TMP_DIR):
        d.mkdir(parents=True, exist_ok=True)
    state.set("docs_created", True)
    # Install enforces the DEFAULTS itself (not the uninstall): sounds on and
    # app auto-update on, matching the definitions toggle — a fresh install
    # always starts with sounds ON no matter what the old settings file held.
    state.set("sounds_enabled", True)
    state.set("auto_update_check", True)

    # 1b. bundled notification sounds (ship with everyone — visible wav files
    # in the user's sounds folder; delete one and the app falls back silently)
    try:
        import base64 as _b64
        SOUNDS_DIR.mkdir(parents=True, exist_ok=True)
        for name, b64 in templates.SOUND_FILES.items():
            (SOUNDS_DIR / f"{name}.wav").write_bytes(_b64.b64decode(b64))
    except Exception:  # noqa: BLE001 — sounds are cosmetic
        pass

    # 2. container -----------------------------------------------------------
    if not shutil.which("distrobox"):
        raise InstallError(
            "distrobox is not installed. Install it first "
            "('sudo apt install distrobox' on Debian/Ubuntu-based, "
            "'sudo dnf install distrobox' on Fedora) and run Install again.")
    created_now = not in_container()
    if created_now:
        p("Step 2/12 — Creating the engine container (clamav)…")
        rc, out = run(f"distrobox create -n {CONTAINER_NAME} -i {CONTAINER_IMAGE} --yes",
                      timeout=1200)
        if rc != 0:
            raise InstallError(f"Container creation failed: {out[-400:]}")
    state.set("container_created", True)

    # 2b. ensure passwordless sudo INSIDE the container. distrobox versions
    # differ: some give the container user a NOPASSWD rule, some don't. Without
    # it, every later 'sudo' (dnf install, clamd, pkill…) run from the GUI
    # (no terminal) fails with "a terminal is required to read the password".
    #
    # FIRST: wait for distrobox-init to finish its first-start setup. On a
    # fresh container that takes minutes (installs sudo, creates the user,
    # writes /etc/sudoers.d/sudoers, grants NOPASSWD for rootless
    # containers) — racing it is why installs flaked on Aurora and why a
    # manual second attempt always worked (init had finished in between).
    p("Waiting for the container's first-time setup to finish…")
    distrobox(
        "bash -lc 'for i in $(seq 1 150); do [ -x /usr/bin/sudo ] && "
        "[ -f /etc/sudoers.d/sudoers ] && break; sleep 2; done'",
        timeout=360)

    # The fallback rule covers the container user by NAME and by group, so it
    # matches whatever state init left the user in. It lives inside the
    # isolated engine container, so its scope is that container only.
    rule = (f"{USER} ALL=(ALL) NOPASSWD: ALL\n"
            "%wheel ALL=(ALL) NOPASSWD: ALL\n"
            "%sudo ALL=(ALL) NOPASSWD: ALL")

    def _sudo_ok():
        return distrobox("sudo -n true 2>&1", timeout=60)[0] == 0

    if not _sudo_ok():
        p("Setting up engine permissions inside the container…")
        ok = _bootstrap_container_sudo(rule, _sudo_ok)
        if not ok and created_now:
            # the container we just made can't provide sudo — rebuild it once
            p("Container didn't come up right — rebuilding it…")
            run(f"distrobox rm -f {CONTAINER_NAME}", timeout=120)
            rc, out = run(f"distrobox create -n {CONTAINER_NAME} -i {CONTAINER_IMAGE} --yes",
                          timeout=1200)
            if rc == 0:
                ok = _bootstrap_container_sudo(rule, _sudo_ok)
        if not _sudo_ok():
            _, sudo_out = distrobox("sudo -n true 2>&1", timeout=60)
            _remove_container_sudoers()  # never leave sudo half-broken
            tail = " ".join(str(sudo_out).split()[-25:])  # no mid-word cuts
            raise InstallError(
                "Could not enable passwordless sudo inside the container. "
                "This computer's container setup needs a password the app "
                "can't provide. In a terminal run:  distrobox rm -f clamav\n"
                "Then press Install again — the app rebuilds it. "
                f"(Details: {tail})\n\n"
                "Diagnostics saved to /tmp/horidoro-install-debug.log — send "
                "that file with your bug report.")

    # 3. packages -------------------------------------------------------------
    p("Step 3/12 — Installing the antivirus engine (ClamAV) inside the container…")
    rc, out = distrobox(f"sudo dnf install -y {' '.join(PACKAGES)}", timeout=1800)
    if rc != 0:
        raise InstallError(f"Package install failed: {out[-400:]}")
    state.set("packages_installed", True)

    # 4. scan.conf -------------------------------------------------------------
    p("Step 4/12 — Writing the scan configuration…")
    staging = TMP_DIR / "scan.conf"
    staging.write_text(_render(templates.SCAN_CONF, tokens))
    rc, out = distrobox(f'sudo cp {shlex.quote(str(staging))} {CONF}')
    staging.unlink(missing_ok=True)  # cleanup — even when the copy fails
    if rc != 0:
        raise InstallError(f"Could not write {CONF}: {out[-300:]}")
    state.set("config_written", True)

    # 5. scripts ----------------------------------------------------------------
    p("Step 5/12 — Installing the scan scripts…")
    scripts = {
        "update.sh": templates.UPDATE_SH,
        "daily_scan.sh": templates.DAILY_SH,
        "monthly_scan.sh": templates.MONTHLY_SH,
        "right_click_scan.sh": templates.RIGHT_CLICK_SH,
        "scan_helper.sh": templates.SCAN_HELPER_SH,
    }
    for name, template in scripts.items():
        target = SCRIPT_DIR / name
        target.write_text(_render(template, tokens))
        target.chmod(0o755)
    state.set("scripts_written", True)

    # 6. timers -------------------------------------------------------------------
    p("Step 6/12 — Setting up the scheduled scans…")
    units = {
        "horidoro-daily.timer": templates.TIMER_DAILY_UNIT,
        "horidoro-daily.service": templates.SERVICE_DAILY_UNIT,
        "horidoro-monthly.timer": templates.TIMER_MONTHLY_UNIT,
        "horidoro-monthly.service": templates.SERVICE_MONTHLY_UNIT,
    }
    unit_dir = Path(HOME) / ".config/systemd/user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    for name, template in units.items():
        (unit_dir / name).write_text(_render(template, tokens))
    run("systemctl --user daemon-reload")
    # Timers are written but NOT enabled: nothing runs automatically until the
    # user turns them on (Schedule tab) and sets scan targets (Settings).
    run("systemctl --user disable --now horidoro-daily.timer "
        "horidoro-monthly.timer 2>/dev/null")
    state.set("timers_enabled", [])

    # 6c. definition-update timer — ON by default so signatures stay fresh
    # even when the user never enables scheduled scans
    for name, template in (("horidoro-update.timer", templates.TIMER_UPDATE_UNIT),
                           ("horidoro-update.service", templates.SERVICE_UPDATE_UNIT)):
        (unit_dir / name).write_text(_render(template, tokens))
    if (state.get("auto_update") or {}).get("enabled", True):
        run("systemctl --user enable --now horidoro-update.timer")
    else:
        run("systemctl --user disable horidoro-update.timer 2>/dev/null")

    # 6d. app self-update timer — headless, ON by default (Settings toggle
    # controls it); downloads + verifies + swaps + restarts the watcher
    for name, template in (("horidoro-app-update.timer", templates.TIMER_APP_UPDATE_UNIT),
                           ("horidoro-app-update.service", templates.SERVICE_APP_UPDATE_UNIT)):
        (unit_dir / name).write_text(_render(template, tokens))
    if state.get("auto_update_check", True):
        run("systemctl --user enable --now horidoro-app-update.timer")
    else:
        run("systemctl --user disable horidoro-app-update.timer 2>/dev/null")

    # 6b. on-access watcher service unit (enabled below, after self-install) --
    (unit_dir / "horidoro-watcher.service").write_text(
        _render(templates.WATCHER_SERVICE, tokens))

    # 7. sudo rule inside the container ---------------------------------------------
    p("Step 7/12 — Configuring engine permissions…")
    # Written via a temp file + cp (never inline-quoted): the rule contains
    # parentheses which break shell quoting inside the distrobox wrapper.
    rule = _render(templates.SUDOERS_RULE, tokens).strip() + "\n"
    staging = TMP_DIR / "horidoro-sudoers"
    staging.write_text(rule)
    rc, out = distrobox(
        f'sudo cp {shlex.quote(str(staging))} /etc/sudoers.d/horidoro-av && '
        f'sudo chmod 440 /etc/sudoers.d/horidoro-av')
    staging.unlink(missing_ok=True)  # cleanup — nothing left behind
    if rc != 0:
        tail = " ".join(str(out).split()[-25:])  # no mid-word cuts
        raise InstallError(f"Sudo rule failed: {tail}")
    state.set("sudo_rule_installed", True)

    # 8. right-click integration -----------------------------------------------------
    p("Step 8/12 — Installing right-click scanning…")
    desktop = _install_desktop_integration()
    state.set("desktop_integration", desktop)

    # 9. app menu entry (self-install) --------------------------------------------------
    p("Step 9/12 — Adding Horidoro AV to the apps menu…")
    _self_install()
    _install_desktop_shortcut()  # desktop launcher appears only after install
    state.set("app_self_installed", True)

    # 9b. watcher service state (after self-install so ExecStart exists) ---------------
    # First kill any STALE watcher (new name AND pre-rename name): a process
    # left over from a previous install keeps watching and logging even though
    # its unit file is gone. Also clear its Bouncer lock entry.
    run("pkill -f '[h]oridoro-av --watch' 2>/dev/null")
    run("pkill -f '[b]ulwark-av --watch' 2>/dev/null")
    for stale in (TMP_DIR / "horidoro_scan.lock",
                  TMP_DIR / "horidoro_scan.lock.flock"):
        try:
            stale.unlink(missing_ok=True)
        except OSError:
            pass
    if state.get("watcher_enabled"):
        run("systemctl --user enable --now horidoro-watcher.service")
    else:
        run("systemctl --user disable --now horidoro-watcher.service 2>/dev/null")

    # 10. optional shell aliases ---------------------------------------------------------
    if state.get("bashrc_requested"):
        _install_bashrc(tokens)
        state.set("bashrc_modified", True)

    # 11. virus DB ------------------------------------------------------------------------
    p("Step 10/12 — Updating the virus definitions (first download takes a few minutes)…")
    rc, out = distrobox("sudo freshclam", timeout=1800)
    if rc != 0:
        raise InstallError(f"Virus database update failed: {out[-400:]}")
    state.set("db_updated", True)

    # 12. self-test ------------------------------------------------------------------------
    p("Step 11/12 — Running the self-test…")
    ok = _eicar_self_test()
    state.set("self_test_passed", ok)
    if not ok:
        raise InstallError("Self-test failed — engine did not detect the test file.")
    p("Step 12/12 — Done. Horidoro AV is installed and verified.")
    return True


# ---------------------------------------------------------------------------
# apply user-configurable settings without a full reinstall
# ---------------------------------------------------------------------------
def apply_config(state, progress=None, runner=distrobox, host_runner=run):
    """Re-apply scan targets, exclusions, and the schedule to the generated
    files (scan.conf, daily/monthly scripts, timer units). Idempotent.

    `runner` executes container-side commands (default distrobox);
    `host_runner` executes host commands (default run) — the timers are
    host-level user units and must NOT run inside the container.
    """
    def p(msg):
        if progress:
            progress(msg)

    paths_daily = state.get("scan_paths", {}).get("daily") or []
    paths_monthly = state.get("scan_paths", {}).get("monthly") or []
    exclusions = state.get("exclusions") or []
    tokens = _tokens(paths_daily, paths_monthly, exclusions,
                     state.get("schedule"))

    p("Writing scan configuration…")
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    staging = TMP_DIR / "scan.conf"
    staging.write_text(_render(templates.SCAN_CONF, tokens))
    rc, out = runner(f'sudo cp {shlex.quote(str(staging))} {CONF}')
    staging.unlink(missing_ok=True)  # cleanup — nothing left behind
    if rc != 0:
        raise RuntimeError(f"Could not write {CONF}: {out[-300:]}")

    p("Writing scan scripts…")
    SCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    for name, template in (("daily_scan.sh", templates.DAILY_SH),
                           ("monthly_scan.sh", templates.MONTHLY_SH)):
        target = SCRIPT_DIR / name
        target.write_text(_render(template, tokens))
        target.chmod(0o755)

    p("Writing schedule…")
    unit_dir = Path(HOME) / ".config/systemd/user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    for name, template in (("horidoro-daily.timer", templates.TIMER_DAILY_UNIT),
                           ("horidoro-monthly.timer", templates.TIMER_MONTHLY_UNIT),
                           ("horidoro-update.timer", templates.TIMER_UPDATE_UNIT),
                           ("horidoro-update.service", templates.SERVICE_UPDATE_UNIT)):
        (unit_dir / name).write_text(_render(template, tokens))
    host_runner("systemctl --user daemon-reload")
    if (state.get("auto_update") or {}).get("enabled", True):
        host_runner("systemctl --user enable --now horidoro-update.timer "
                    "2>/dev/null")
    else:
        host_runner("systemctl --user disable --now horidoro-update.timer "
                    "2>/dev/null")
    if state.get("timers_enabled"):
        host_runner("systemctl --user restart horidoro-daily.timer "
                    "horidoro-monthly.timer 2>/dev/null")
    p("Configuration applied.")
    return True


# ---------------------------------------------------------------------------
# install sub-steps
# ---------------------------------------------------------------------------
def _install_desktop_integration():
    desktop_env = (os.environ.get("XDG_CURRENT_DESKTOP", "") +
                   os.environ.get("XDG_SESSION_TYPE", "")).lower()
    if "kde" in desktop_env or "plasma" in desktop_env:
        d = Path(HOME) / ".local/share/kio/servicemenus"
        d.mkdir(parents=True, exist_ok=True)
        target = d / "horidoro-av.desktop"
        target.write_text(_render(templates.KDE_SERVICEMENU, _tokens([], [], [])))
        # KDE requires servicemenu .desktop files to carry the executable bit,
        # otherwise Dolphin refuses: "You are not authorized to execute this file"
        target.chmod(0o755)
        return "kde"
    if "gnome" in desktop_env or "cinnamon" in desktop_env \
            or "mate" in desktop_env:
        d = Path(HOME) / ".local/share/nautilus/scripts"
        d.mkdir(parents=True, exist_ok=True)
        target = d / "Horidoro AV"
        target.write_text(_render(templates.GNOME_SCRIPT, _tokens([], [], [])))
        target.chmod(0o755)
        return "gnome"
    return None  # unknown desktop — GUI reports it; scans still work


def _self_install():
    # copy the running app to ~/.local/bin and register an app-menu entry
    APP_INSTALL_PATH.parent.mkdir(parents=True, exist_ok=True)
    src = Path(sys.argv[0]).resolve()
    if src.exists() and src.name.endswith(".py") \
            and src != APP_INSTALL_PATH.resolve():
        shutil.copy2(src, APP_INSTALL_PATH)
        APP_INSTALL_PATH.chmod(0o755)

    # The logo is installed at every common size (16-256 px) so desktop and
    # menu icons render at normal size. Stale placeholder SVGs are removed so
    # upgrades show the correct icon (KDE would prefer the SVG otherwise).
    _write_icon_set()
    for stale in (Path(HOME) / ".local/share/icons/hicolor/scalable/apps/horidoro-av.svg",):
        try:
            stale.unlink(missing_ok=True)
        except OSError:
            pass

    apps_dir = Path(HOME) / ".local/share/applications"
    apps_dir.mkdir(parents=True, exist_ok=True)
    (apps_dir / "horidoro-av.desktop").write_text(
        _render(templates.APP_DESKTOP_ENTRY, _tokens([], [], [])))

    run("gtk-update-icon-cache -f -t ~/.local/share/icons/hicolor 2>/dev/null", timeout=60)


def _write_icon_set():
    """Install the Horidoro logo at every common icon size (16-256 px) so the
    desktop and app-menu icons render at NORMAL size, crisp, on any theme.

    The embedded logo has large transparent margins around the artwork (the
    art fills only ~45% of the 256px canvas) — uncropped, even a 48px icon
    shows a tiny ~21px logo. The margins are trimmed first so the artwork
    fills each icon square."""
    try:
        import base64 as _b64
        import gi
        gi.require_version("GdkPixbuf", "2.0")
        from gi.repository import GdkPixbuf
        loader = GdkPixbuf.PixbufLoader.new()
        loader.write(_b64.b64decode(templates.APP_ICON_PNG))
        loader.close()
        pix = loader.get_pixbuf()
        pix = _crop_transparent_margins(pix)
        for size in (16, 22, 24, 32, 48, 64, 128, 256):
            d = Path(HOME) / f".local/share/icons/hicolor/{size}x{size}/apps"
            d.mkdir(parents=True, exist_ok=True)
            scaled = pix.scale_simple(size, size, GdkPixbuf.InterpType.BILINEAR)
            scaled.savev(str(d / "horidoro-av.png"), "png", [], [])
    except Exception:  # noqa: BLE001 — cosmetic only
        pass


def _crop_transparent_margins(pix):
    """Trim fully-transparent borders so the artwork fills the canvas.
    Returns the original pixbuf if nothing visible (or on any error)."""
    try:
        w, h = pix.get_width(), pix.get_height()
        px = pix.get_pixels()
        rs = pix.get_rowstride()
        nch = pix.get_n_channels()
        minx, miny, maxx, maxy = w, h, -1, -1
        for y in range(h):
            row = y * rs
            for x in range(w):
                if px[row + x * nch + 3] > 4:  # visible pixel (incl. faint glow)
                    if x < minx:
                        minx = x
                    if x > maxx:
                        maxx = x
                    if y < miny:
                        miny = y
                    if y > maxy:
                        maxy = y
        if maxx < minx:
            return pix  # fully transparent — nothing to crop
        return pix.new_subpixbuf(minx, miny, maxx - minx + 1, maxy - miny + 1)
    except Exception:  # noqa: BLE001 — cosmetic only
        return pix


def _install_desktop_shortcut():
    """Desktop shortcut — only after a real install (not on first launch)."""
    desktop_dir = Path(HOME) / "Desktop"
    try:
        if desktop_dir.is_dir():
            shortcut = desktop_dir / "Horidoro AV.desktop"
            shortcut.write_text(
                _render(templates.APP_DESKTOP_ENTRY, _tokens([], [], [])))
            shortcut.chmod(0o755)
    except OSError:
        pass


def ensure_self_installed(state):
    """Register the app-menu entry + ~/.local/bin copy on first launch.

    Lets a non-technical user get Horidoro AV into their apps menu by simply
    running the app once — no terminal knowledge needed. Best-effort and
    idempotent; uninstall removes everything it created.
    """
    if state.get("app_self_installed"):
        return
    src = Path(sys.argv[0]).resolve()
    if src.name not in ("horidoro-av.py", "horidoro-av"):
        return  # dev launcher / unknown entry point — do not register
    try:
        _self_install()
        state.set("app_self_installed", True)
    except OSError:
        pass  # best-effort: the app still runs fine without the menu entry


def repair_integration(state):
    """Self-heal desktop/right-click integration on every launch.

    A stale or partially-broken install (missing menu file, missing/absent
    executable bits) is fixed the moment the app starts — no manual commands,
    no re-install. Cheap: a few stat/chmod calls, idempotent. Never touches
    anything outside the app's own files.
    """
    kde = Path(HOME) / ".local/share/kio/servicemenus/horidoro-av.desktop"
    gnome = Path(HOME) / ".local/share/nautilus/scripts/Horidoro AV"
    integration = state.get("desktop_integration")
    try:
        if integration == "kde" and not kde.exists():
            kde.parent.mkdir(parents=True, exist_ok=True)
            kde.write_text(_render(templates.KDE_SERVICEMENU, _tokens([], [], [])))
        if integration == "gnome" and not gnome.exists():
            gnome.parent.mkdir(parents=True, exist_ok=True)
            gnome.write_text(_render(templates.GNOME_SCRIPT, _tokens([], [], [])))
    except OSError:
        pass
    candidates = [kde, gnome]
    if SCRIPT_DIR.is_dir():
        candidates += sorted(SCRIPT_DIR.glob("*.sh"))
    for f in candidates:
        try:
            if f.exists():
                f.chmod(0o755)  # KDE refuses non-executable servicemenus
        except OSError:
            pass
    # keep the correct app icon in place on EVERY launch: the PNG logo at all
    # common sizes (16-256), never the stale placeholder SVG (which KDE prefers
    # in its icon lookup and which renders the logo tiny at small sizes)
    _write_icon_set()
    try:
        for stale in (Path(HOME) / ".local/share/icons/hicolor/scalable/apps/horidoro-av.svg",):
            stale.unlink(missing_ok=True)
    except OSError:
        pass
    # refresh the icon theme cache so desktops pick up the rewritten icons
    # immediately (old uncropped logos self-heal without a logout)
    run("gtk-update-icon-cache -f -t ~/.local/share/icons/hicolor 2>/dev/null",
        timeout=60)


def _install_bashrc(tokens):
    rc_path = Path(HOME) / ".bashrc"
    block = _render(templates.BASHRC_BLOCK, tokens)
    if rc_path.exists() and "HORIDORO AV BEGIN" in rc_path.read_text():
        return  # already present — idempotent
    with rc_path.open("a") as f:
        f.write(block)


def _eicar_self_test():
    """Write an EICAR test file, scan it, expect a detection, clean up."""
    test_file = TMP_DIR / "horidoro-eicar-test.txt"
    test_file.write_text("X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-"
                         "ANTIVIRUS-TEST-FILE!$H+H*\n")
    distrobox("sudo clamd -c " + CONF + " 2>/dev/null")
    distrobox("clamdscan --ping --wait > /dev/null 2>&1")
    rc, out = distrobox(f'clamdscan --multiscan --fdpass {shlex.quote(str(test_file))} 2>&1')
    detected = "FOUND" in out and "Infected files: 1" in out
    distrobox("sudo pkill -9 clamd 2>/dev/null")
    test_file.unlink(missing_ok=True)  # cleanup — nothing left behind
    return detected


# ---------------------------------------------------------------------------
# uninstall
# ---------------------------------------------------------------------------
def uninstall_all(state, progress=None, remove_container=True, keep_app=False):
    """Remove everything Horidoro created. `keep_app=True` is used by the
    install-failure rollback: it cleans the install artifacts but KEEPS the
    app's own launcher (~/.local/bin/horidoro-av) so shortcuts still work
    and a retry can reinstall normally."""
    def p(msg):
        if progress:
            progress(msg)

    # timers + watcher service (disable --now ALSO stops a running watcher —
    # removing only the unit file leaves the process alive and logging)
    p("Removing scheduled scans…")
    run("systemctl --user disable --now horidoro-daily.timer horidoro-monthly.timer "
        "horidoro-watcher.service horidoro-update.timer "
        "horidoro-app-update.timer 2>/dev/null")
    run("pkill -f '[h]oridoro-av --watch' 2>/dev/null")
    unit_dir = Path(HOME) / ".config/systemd/user"
    for name in ("horidoro-daily.timer", "horidoro-daily.service",
                 "horidoro-monthly.timer", "horidoro-monthly.service",
                 "horidoro-watcher.service", "horidoro-update.timer",
                 "horidoro-update.service",
                 "horidoro-app-update.timer", "horidoro-app-update.service"):
        (unit_dir / name).unlink(missing_ok=True)
    run("systemctl --user daemon-reload")
    state.set("timers_enabled", [])
    state.set("watcher_enabled", False)

    # desktop integration + app menu entry
    p("Removing app menu and right-click integration…")
    for f in (Path(HOME) / ".local/share/kio/servicemenus/horidoro-av.desktop",
              Path(HOME) / ".local/share/nautilus/scripts/Horidoro AV",
              Path(HOME) / ".local/share/applications/horidoro-av.desktop",
              Path(HOME) / ".local/share/icons/hicolor/scalable/apps/horidoro-av.svg",
              Path(HOME) / ".local/share/icons/hicolor/256x256/apps/horidoro-av.png",
              Path(HOME) / "Desktop/Horidoro AV.desktop"):
        try:
            f.unlink(missing_ok=True)
        except OSError:
            pass
    if not keep_app:
        # the app's own launcher file. A real uninstall removes it; the
        # install-failure ROLLBACK keeps it (keep_app=True) — it's the
        # installer itself, and deleting it while the app runs from it left
        # desktop shortcuts pointing at a missing file after a reinstall.
        try:
            APP_INSTALL_PATH.unlink(missing_ok=True)
        except OSError:
            pass
    # all icon sizes (16-256) + refresh the cache — nothing left behind
    for size in (16, 22, 24, 32, 48, 64, 128, 256):
        png = Path(HOME) / f".local/share/icons/hicolor/{size}x{size}/apps/horidoro-av.png"
        try:
            png.unlink(missing_ok=True)
        except OSError:
            pass
    run("gtk-update-icon-cache -f -t ~/.local/share/icons/hicolor 2>/dev/null",
        timeout=60)

    # bashrc block
    rc_path = Path(HOME) / ".bashrc"
    if rc_path.exists():
        text = rc_path.read_text()
        start = text.find("# >>>>> HORIDORO AV BEGIN")
        end = text.find("# <<<<< HORIDORO AV END")
        if start != -1 and end != -1:
            end = text.find("\n", end) + 1
            rc_path.write_text(text[:start] + text[end:])

    # container-level artifacts (bounded timeouts: a wedged container must
    # never freeze the uninstall for minutes — it reports and moves on)
    if in_container():
        p("Removing engine permissions…")
        distrobox("sudo rm -f /etc/sudoers.d/horidoro-av 2>/dev/null", timeout=60)
        distrobox("sudo pkill -9 clamd 2>/dev/null", timeout=60)

    # user data: ALWAYS removed on uninstall — the warning dialog already
    # told the user this deletes settings, logs, quarantined files, and
    # sounds, and they chose OK. Nothing is left behind.
    p("Removing Horidoro's data…")
    shutil.rmtree(DATA_DIR, ignore_errors=True)
    # a legacy pre-move location (never shipped, but cheap to sweep)
    shutil.rmtree(Path(HOME) / "Documents/horidoro", ignore_errors=True)

    # container
    if remove_container and in_container():
        p("Removing the ClamAV container…")
        rc, out = run(f"distrobox rm -f {CONTAINER_NAME}", timeout=90)
        if rc != 0:
            p("Container removal failed — run 'podman rm -f clamav' manually "
              "if it is wedged.")

    state.set("container_created", False)
    state.set("packages_installed", False)
    state.set("config_written", False)
    state.set("scripts_written", False)
    state.set("sudo_rule_installed", False)
    state.set("desktop_integration", None)
    state.set("app_self_installed", False)
    state.set("docs_created", False)
    state.set("db_updated", False)
    state.set("self_test_passed", False)
    state.set("bashrc_modified", False)
    state.set("scan_paths", {"daily": [], "monthly": []})
    state.set("exclusions", [])
    state.set("watcher_folders", [])
    # reset the keys uninstall forgot — they stayed in the in-memory State and
    # were re-saved during the NEXT install, so a "fresh" install inherited
    # the old schedule (Mon/Wed/Fri/Sun days came back) and the old sound
    # toggle (sounds started OFF though the user never turned them off).
    state.set("schedule", DEFAULT_STATE["schedule"])
    state.set("sounds_enabled", True)
    state.set("auto_update_check", True)
    state.set("bashrc_requested", False)
    # config dir (state.json + watcher cache.db) — removed AFTER all state
    # writes so the manifest file isn't re-created by save()
    shutil.rmtree(CONFIG_DIR, ignore_errors=True)
    p("Uninstall complete.")
    return True




