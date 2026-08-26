#!/usr/bin/env python3
"""Horidoro AV — container-sudo bootstrap tests (all mocked, nothing real runs).

Guards the class of bug that broke real installs on Aurora: a fresh
distrobox container without passwordless sudo, where the old bootstrap
relied on HOST passwordless sudo (which most machines don't have) and died
with "Could not enable passwordless sudo inside the container".

The new bootstrap writes the rule through rootless podman (mount/exec) —
no password anywhere — and a container the app itself just created that
still can't provide sudo is rebuilt once instead of being left broken.

Run:  python3 tests/test_bootstrap.py
"""
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

import atexit
import shutil

_HOME = tempfile.mkdtemp(prefix="horidoro-boot-")
atexit.register(lambda: shutil.rmtree(_HOME, ignore_errors=True))
atexit.register(lambda: Path("/tmp/horidoro-install-debug.log").unlink(missing_ok=True))
os.environ["HOME"] = _HOME
os.environ.pop("XDG_CONFIG_HOME", None)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "horidoro"))

import installer  # noqa: E402
from state import State  # noqa: E402

failures = []
calls = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" — {detail}" if detail else ""))
    if not cond:
        failures.append(name)


def bash_n(script):
    """bash -n the given script text (syntax check)."""
    r = subprocess.run(["bash", "-n"], input=script, text=True,
                       capture_output=True)
    return r.returncode == 0


def inner_script(full_cmd):
    """'podman unshare bash -lc <quoted script>' -> the unquoted script."""
    parts = shlex.split(full_cmd)
    return parts[-1]


RULE = "%wheel ALL=(ALL) NOPASSWD: ALL\n%sudo ALL=(ALL) NOPASSWD: ALL"

# ---------------------------------------------------------------------------
# 1. strategy-1 command: rootless podman mount write — syntax + content
# ---------------------------------------------------------------------------
# 1. strategy order + VERIFICATION: rc=0 is NOT success — sudo must accept it
# ---------------------------------------------------------------------------
calls.clear()


def fake_run_ok(cmd, timeout=None, **kw):
    calls.append(cmd)
    return (0, "")


installer.run = fake_run_ok
ok = installer._bootstrap_container_sudo(RULE, verify=lambda: True)
check("bootstrap succeeds when exec-as-root works (verified)", ok is True)
check("tries exec-as-root FIRST (root-owned = sudo accepts it)",
      "podman exec -u 0" in calls[0])
check("rule is group-based (username-agnostic — the Aurora regression)",
      "%wheel ALL=(ALL) NOPASSWD: ALL" in calls[0])
check("waits for distrobox-init (sudo + user) before writing",
      "id -u" in calls[0] and "seq 1 180" in calls[0])
check("exec strategy script passes bash -n", bash_n(inner_script(calls[0])),
      inner_script(calls[0])[:80])
check("writes the NOPASSWD rule to the right file",
      "NOPASSWD: ALL" in calls[0]
      and "etc/sudoers.d/horidoro-av" in calls[0])
check("chmods 440", "chmod 440" in calls[0])

# THE AURORA REGRESSION: the old code returned True on rc==0 without checking
# sudo actually accepted the rule — a rootless mount-write lands user-owned
# and sudo silently ignores it, so the install failed while the log said
# "rootless-mount: rc=0". With verification, a write that doesn't make sudo
# work must fall through to the next strategy, not declare victory.
calls.clear()
installer.run = fake_run_ok  # every strategy "succeeds" (rc=0)...
ok = installer._bootstrap_container_sudo(RULE, verify=lambda: False)
check("rc=0 WITHOUT sudo accepting it is NOT success (Aurora bug guard)",
      ok is False)
check("falls through to ALL strategies when verification fails",
      len(calls) == 3
      and any("podman exec -u 0" in c for c in calls)
      and any("podman unshare" in c for c in calls)
      and any("sudo distrobox enter" in c for c in calls))
for cmd in calls:
    check(f"strategy script parses: {cmd[:45]}…", bash_n(inner_script(cmd)))

# ---------------------------------------------------------------------------
# 2. strategy fallback (each verified): exec fails -> mount -> host sudo
# ---------------------------------------------------------------------------
def _fake_seq(*fails_on):
    def f(cmd, timeout=None, **kw):
        calls.append(cmd)
        if any(t in cmd for t in fails_on):
            return (1, "fail")
        return (0, "")
    return f

calls.clear()
installer.run = _fake_seq("podman exec")
ok = installer._bootstrap_container_sudo(RULE, verify=lambda: True)
check("falls through to rootless mount when exec fails",
      ok is True and "podman unshare" in "\n".join(calls))
check("mount strategy script parses", bash_n(inner_script(calls[1])),
      inner_script(calls[1])[:80])

calls.clear()
installer.run = _fake_seq("podman exec", "podman unshare")
ok = installer._bootstrap_container_sudo(RULE, verify=lambda: True)
check("falls through to host sudo when exec + mount fail",
      ok is True and "sudo distrobox enter" in "\n".join(calls))
check("host-sudo strategy script parses", bash_n(inner_script(calls[2])),
      inner_script(calls[2])[:80])

calls.clear()
installer.run = _fake_seq("podman", "sudo distrobox")
ok = installer._bootstrap_container_sudo(RULE, verify=lambda: True)
check("reports False when every strategy fails", ok is False)

# ---------------------------------------------------------------------------
# 3. install_all on a fresh container WITHOUT sudo -> auto-rebuild + error
# ---------------------------------------------------------------------------
calls.clear()
container_exists = {"v": False}
installer.in_container = lambda: container_exists["v"]
installer.shutil.which = lambda _x: "/usr/bin/distrobox"


def fake_run_hard(cmd, timeout=None, **kw):
    calls.append(cmd)
    if "distrobox create" in cmd:
        container_exists["v"] = True
        return (0, "")
    if "distrobox rm" in cmd:
        container_exists["v"] = False
        return (0, "")
    return (1, "sudo: a password is required")


def fake_distrobox_hard(cmd, timeout=600):
    calls.append("DISTROBOX: " + cmd)
    return (1, "sudo: a password is required")


installer.run = fake_run_hard
installer.distrobox = fake_distrobox_hard
st = State()
launcher = installer.APP_INSTALL_PATH
launcher.parent.mkdir(parents=True, exist_ok=True)
launcher.write_text("#!/usr/bin/env python3\n# fake launcher\n")
raised = False
msg = ""
try:
    installer.install_all(st)
except installer.InstallError as e:
    raised = True
    msg = str(e)
check("fresh container without sudo -> InstallError (not silent)",
      raised is True)
check("error explains the terminal fix",
      "distrobox rm -f clamav" in msg)
check("auto-rebuild happened before giving up (container removed + recreated)",
      calls.count("distrobox rm -f clamav") >= 1
      and sum("distrobox create -n clamav" in c for c in calls) >= 2)
check("ROLLBACK removed everything (container gone again)",
      calls.count("distrobox rm -f clamav") >= 2)
check("ROLLBACK reset the manifest",
      st.get("container_created") is False and st.is_installed() is False
      and st.get("packages_installed") is False)
check("ROLLBACK deleted config + data dirs (nothing left behind)",
      not installer.CONFIG_DIR.exists() and not installer.DATA_DIR.exists())
check("ROLLBACK keeps the app launcher (shortcuts stay valid)",
      launcher.exists(), "the running app's own file must survive a rollback")
check("diagnostics log written (survives the rollback)",
      Path("/tmp/horidoro-install-debug.log").exists()
      and "distrobox" in Path("/tmp/horidoro-install-debug.log").read_text())
try:
    Path("/tmp/horidoro-install-debug.log").unlink()
except OSError:
    pass

# ---------------------------------------------------------------------------
# 4. install_all on a container WITH working sudo -> full install completes
# ---------------------------------------------------------------------------
calls.clear()


def fake_run_all_ok(cmd, timeout=None, **kw):
    calls.append(cmd)
    return (0, "")


def fake_distrobox_ok(cmd, timeout=600):
    calls.append("DISTROBOX: " + cmd)
    if "horidoro-eicar-test" in cmd and "clamdscan" in cmd:
        return (0, "eicar: Eicar-Test-Signature FOUND\nInfected files: 1\n")
    return (0, "")


installer.run = fake_run_all_ok
installer.distrobox = fake_distrobox_ok
st2 = State()
st2.set("sounds_enabled", False)   # stale "off" from an old install
st2.set("auto_update_check", False)
junk = installer.TMP_DIR / "stale-junk"
junk.parent.mkdir(parents=True, exist_ok=True)
junk.write_text("old half-install junk")
installer.install_all(st2)
check("container with working sudo installs cleanly",
      st2.is_installed() and st2.get("db_updated")
      and st2.get("self_test_passed") is True)
check("install ENFORCES sounds on (the sound-default regression)",
      st2.get("sounds_enabled") is True)
check("install ENFORCES app auto-update on",
      st2.get("auto_update_check") is True)
check("sudo check ran before proceeding",
      any("sudo -n true" in c for c in calls))
check("tmp dir self-cleaned before install (stale junk removed)",
      not junk.exists())

# ---------------------------------------------------------------------------
# 5. install fails at a LATER step (packages) -> FULL rollback, nothing left
# ---------------------------------------------------------------------------
calls.clear()
installer.in_container = lambda: True  # container exists (was created)


def fake_run_pkg(cmd, timeout=None, **kw):
    calls.append(cmd)
    if "distrobox rm" in cmd:
        installer.in_container = lambda: False
    return (0, "")


def fake_distrobox_pkg(cmd, timeout=600):
    calls.append("DISTROBOX: " + cmd)
    if "sudo dnf install" in cmd:
        return (1, "dnf: could not download")
    return (0, "")


installer.run = fake_run_pkg
installer.distrobox = fake_distrobox_pkg
st3 = State()
launcher3 = installer.APP_INSTALL_PATH
launcher3.parent.mkdir(parents=True, exist_ok=True)
launcher3.write_text("#!/usr/bin/env python3\n# fake launcher\n")
raised = False
msg3 = ""
try:
    installer.install_all(st3)
except installer.InstallError as e:
    raised = True
    msg3 = str(e)
check("later-step failure -> InstallError surfaced",
      raised is True and "Package install failed" in msg3)
check("rollback removed the container",
      calls.count("distrobox rm -f clamav") >= 1)
check("rollback removed the sudo rule from the container",
      any("sudoers.d/horidoro-av" in c and "rm -f" in c for c in calls))
check("rollback reset the manifest (not installed)",
      st3.is_installed() is False and st3.get("db_updated") is False)
check("rollback deleted config + data dirs",
      not installer.CONFIG_DIR.exists() and not installer.DATA_DIR.exists())
check("rollback keeps the app launcher", launcher3.exists())
check("rollback left no temp junk", not installer.TMP_DIR.exists())

# ---------------------------------------------------------------------------
# 6. UNINSTALL must reset EVERYTHING in memory — including schedule,
# sounds_enabled, auto_update_check. The user uninstalls then reinstalls
# (app stays open the whole time); leftover keys were re-saved during the
# reinstall, so a "fresh" install inherited old scan days (Mon/Wed/Fri/Sun)
# and the sound toggle came back OFF though it was never turned off.
# ---------------------------------------------------------------------------
calls.clear()
installer.in_container = lambda: False
installer.run = lambda cmd, timeout=None, **kw: (calls.append(cmd) or (0, ""))
installer.distrobox = lambda cmd, timeout=600: (0, "")
st4 = State()
st4.set("schedule", {"daily": {"time": "02:00",
                                "days": "Mon,Wed,Fri,Sun"},
                      "monthly": {"time": "02:00", "day": "1"},
                      "auto_update": {"enabled": True, "time": "03:00"}})
st4.set("sounds_enabled", False)
st4.set("auto_update_check", False)
st4.set("bashrc_requested", True)
installer.uninstall_all(st4)
check("uninstall resets schedule (no stale scan days)",
      st4.get("schedule") == installer.DEFAULT_STATE["schedule"],
      str(st4.get("schedule")))
check("uninstall resets sounds_enabled to ON (default)",
      st4.get("sounds_enabled") is True)
check("uninstall resets auto_update_check to ON (default)",
      st4.get("auto_update_check") is True)
check("uninstall resets bashrc_requested",
      st4.get("bashrc_requested") is False)

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("ALL BOOTSTRAP TESTS PASSED")
