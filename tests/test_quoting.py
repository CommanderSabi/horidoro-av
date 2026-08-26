#!/usr/bin/env python3
"""Horidoro AV — quoting regression test.

Guards the shell-quoting pattern used for commands run inside the distrobox
(`distrobox enter ... -- bash -lc "..."` wrapper): inner arguments must be
single-quoted via shlex.quote, never double-quoted. A double quote inside the
wrapper terminates the outer string — the bug that broke real installs at the
sudo-rule step: `USER ALL=(ALL) NOPASSWD:...` left `(ALL)` unquoted and the
host shell died with "syntax error near unexpected token `('"..

Run:  python3 tests/test_quoting.py
"""
import os
import shlex
import subprocess
import sys
import tempfile

failures = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" — {detail}" if detail else ""))
    if not cond:
        failures.append(name)


def wrap(cmd):
    """Replicate shell.distrobox()'s wrapper exactly."""
    return f'distrobox enter -n clamav -- bash -lc "{cmd}"'


def syntax_ok(cmd):
    """True if the full wrapped command parses under the host shell."""
    p = subprocess.run(["bash", "-n", "-c", wrap(cmd)], capture_output=True,
                       text=True)
    return p.returncode == 0


# --- the OLD broken pattern (double quotes around the rule) ------------------
rule = ("USER ALL=(ALL) NOPASSWD: /usr/bin/freshclam, /usr/bin/clamd, "
        "/usr/bin/pkill")
old = (f"sudo bash -c 'echo \"{rule}\" > /etc/sudoers.d/horidoro-av && "
       f"chmod 440 /etc/sudoers.d/horidoro-av'")
check("OLD sudo-rule command fails to parse (the bug)", not syntax_ok(old))

# --- the NEW pattern (temp file + shlex.quote) -------------------------------
sandbox = tempfile.mkdtemp(prefix="bw-quote-")
staging = os.path.join(sandbox, "horidoro-sudoers")
open(staging, "w").write(rule + "\n")
new = (f"sudo cp {shlex.quote(staging)} /etc/sudoers.d/horidoro-av && "
       f"sudo chmod 440 /etc/sudoers.d/horidoro-av")
check("NEW sudo-rule command parses", syntax_ok(new))

conf = os.path.join(sandbox, "scan.conf")
check("scan.conf cp command parses",
      syntax_ok(f"sudo cp {shlex.quote(conf)} /etc/clamd.d/scan.conf"))
check("eicar clamdscan command parses",
      syntax_ok(f"clamdscan --multiscan --fdpass {shlex.quote(conf)} 2>&1"))
check("verbose scan command parses",
      syntax_ok(f"clamscan -r --exclude-dir={shlex.quote('^/home/x/quarantine')} "
                f"--move={shlex.quote(conf)} "
                f"--log={shlex.quote(conf)} {shlex.quote(conf)} 2>&1"))

# third-party DB config now runs a helper script via `sudo bash <file>` —
# no inline quoting at all (regression: the old inline form broke with
# "unexpected EOF" exactly like the sudo-rule bug)
helper = shlex.quote(os.path.join(sandbox, "horidoro-thirdparty.sh"))
third = f"sudo bash {helper} {shlex.quote(conf)} 2>&1"
check("third-party DB command parses", syntax_ok(third))

# custom database-URL subscription uses the same helper-script pattern
url_helper = shlex.quote(os.path.join(sandbox, "horidoro-dburl.sh"))
dburl = f"sudo bash {url_helper} {shlex.quote(conf)} 2>&1"
check("database-URL command parses", syntax_ok(dburl))

# manual engine update commands (no quotes needed — kept simple)
check("engine check-update parses",
      syntax_ok("sudo dnf check-update -q clamav clamd clamav-update 2>&1"))
check("engine upgrade parses",
      syntax_ok("sudo dnf upgrade -y clamav clamd clamav-update 2>&1"))

# --- prove the inner command actually executes through the wrapper ------------
dest = os.path.join(sandbox, "out-sudoers")
inner = (f"cp {shlex.quote(staging)} {shlex.quote(dest)} "
         f"&& chmod 440 {shlex.quote(dest)}")
rc = subprocess.run(["bash", "-lc", inner]).returncode
check("inner cp executes under bash -lc", rc == 0)
ok = os.path.exists(dest) and open(dest).read() == rule + "\n"
check("sudoers file content correct", ok)

# --- every rendered shell script must parse (bash -n) ------------------------
# guards the class of bug where an unescaped inner quote breaks a rendered
# script (found in the daily/monthly "no scan targets" skip branch).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "horidoro"))
import installer  # noqa: E402
import templates  # noqa: E402
_toks = {"LOG_DIR": "/tmp/h/logs", "TMP_DIR": "/tmp/h/tmp",
         "QUARANTINE_DIR": "/tmp/h/q", "SCRIPT_DIR": "/tmp/h/scripts",
         "SOUNDS_DIR": "/tmp/h/sounds", "CONTAINER": "clamav",
         "CONF": "/etc/clamd.d/scan.conf", "DAILY_PATHS": "",
         "DAILY_PATHS_SET": "0", "MONTHLY_PATHS": "",
         "MONTHLY_PATHS_SET": "0"}
for _name, _tpl in (("update.sh", templates.UPDATE_SH),
                    ("daily_scan.sh", templates.DAILY_SH),
                    ("monthly_scan.sh", templates.MONTHLY_SH),
                    ("right_click_scan.sh", templates.RIGHT_CLICK_SH),
                    ("scan_helper.sh", templates.SCAN_HELPER_SH)):
    _s = _tpl
    for _k, _v in _toks.items():
        _s = _s.replace(f"@{_k}@", str(_v))
    _f = os.path.join(sandbox, _name)
    open(_f, "w").write(_s)
    check(f"script parses: {_name}",
          subprocess.run(["bash", "-n", _f]).returncode == 0)

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("ALL QUOTING TESTS PASSED")
