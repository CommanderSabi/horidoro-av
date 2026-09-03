#!/usr/bin/env python3
"""Horidoro AV — sandboxed tests for watcher.py (HashCache + real inotify).

Run:  python3 tests/test_watcher.py
Everything happens in temp dirs — no app state, no containers, no systemd.
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "horidoro"))
from watcher import HashCache, Watcher  # noqa: E402

failures = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" — {detail}" if detail else ""))
    if not cond:
        failures.append(name)


# ---- HashCache ------------------------------------------------------------
d = tempfile.mkdtemp(prefix="horidoro-cache-")
cache = HashCache(os.path.join(d, "cache.db"))
p = os.path.join(d, "f.txt")

open(p, "w").write("hello world")
check("cache: not known before first scan", not cache.known_clean(p))
check("cache: record clean", cache.record_clean(p) is True)
check("cache: known clean after record", cache.known_clean(p))
check("cache: verify passes without scanning", cache.verify_clean(p) is True)

time.sleep(1.05)  # ensure mtime changes
os.utime(p, (time.time() + 2, time.time() + 2))  # metadata-only change
check("cache: metadata change breaks stat fast-path", not cache.known_clean(p))
check("cache: verify_clean still True for content-identical",
      cache.verify_clean(p) is True)
check("cache: verify refreshed the signature", cache.known_clean(p) is True)

open(p, "a").write(" more")  # real content change
check("cache: modified file not known clean", not cache.known_clean(p))
check("cache: verify fails on changed content", cache.verify_clean(p) is False)

open(p, "w").write("totally different content")
cache.record_clean(p)
check("cache: re-record works after change", cache.known_clean(p))

cache.invalidate(p)
check("cache: invalidated path forgotten", not cache.known_clean(p))
check("cache: count reflects cleanup", cache.count() == 0)

# >2GB file never recorded (sparse, no actual disk usage)
big = os.path.join(d, "big.bin")
with open(big, "wb") as f:
    f.truncate(2 * 1024**3 + 1)
check("cache: >2GB file refused (never scannable)", cache.record_clean(big) is False)
check("cache: >2GB not known clean", not cache.known_clean(big))
cache.close()

# ---- Watcher (real inotify) ----------------------------------------------
w = tempfile.mkdtemp(prefix="horidoro-watch-")
seen = []
overflow = []
watcher = Watcher([w], seen.append, on_overflow=lambda n: overflow.append(n))
watcher.start()
time.sleep(0.5)  # initial tree walk

# file created + written
open(os.path.join(w, "a.txt"), "w").write("alpha")
# file created via move (the way downloads/editors land files)
tmpf = os.path.join(w, ".tmp-x")
open(tmpf, "w").write("beta")
os.rename(tmpf, os.path.join(w, "b.txt"))
# new subdirectory then file inside it
os.makedirs(os.path.join(w, "sub"))
open(os.path.join(w, "sub", "c.txt"), "w").write("gamma")

time.sleep(4.0)  # debounce (1.5s) + loop slices
watcher.stop()

def path_set(found):
    return {os.path.basename(x) for x in found}

check("watcher: saw a.txt", "a.txt" in path_set(seen), str(seen))
check("watcher: saw moved-in b.txt", "b.txt" in path_set(seen), str(seen))
check("watcher: recursive subdir c.txt", "c.txt" in path_set(seen), str(seen))
check("watcher: watch_count tracked", watcher.watch_count >= 2, str(watcher.watch_count))

# debounce: rapid rewrites fire once
w3 = tempfile.mkdtemp(prefix="horidoro-deb-")
seen3 = []
watcher3 = Watcher([w3], seen3.append)
watcher3.start()
time.sleep(0.5)
for i in range(20):
    open(os.path.join(w3, "burst.txt"), "w").write(f"v{i}")
time.sleep(3.0)
watcher3.stop()
check("watcher: burst writes debounced to one event", len(seen3) == 1, str(seen3))

# a directory moved into a watched folder emits its existing files
w4 = tempfile.mkdtemp(prefix="horidoro-move-")
incoming = os.path.join(w4, "incoming")
os.makedirs(incoming)
open(os.path.join(incoming, "pre.txt"), "w").write("pre-existing")
seen4 = []
watcher4 = Watcher([w4], seen4.append)
watcher4.start()
time.sleep(0.5)
os.rename(incoming, os.path.join(w4, "moved-in"))
time.sleep(3.0)
watcher4.stop()
check("watcher: moved-in dir files are emitted",
      any("pre.txt" in x for x in seen4), str(seen4))

# download-in-progress markers are skipped; the final rename is scanned
w5 = tempfile.mkdtemp(prefix="horidoro-crdl-")
seen5 = []
watcher5 = Watcher([w5], seen5.append)
watcher5.start()
time.sleep(0.5)
partial = os.path.join(w5, "Unconfirmed 10722.crdownload")
open(partial, "w").write("half of a download")
time.sleep(2.5)
os.rename(partial, os.path.join(w5, "eicar_com.zip"))
time.sleep(3.0)
watcher5.stop()
check("watcher: .crdownload partial is ignored",
      not any("crdownload" in x for x in seen5), str(seen5))
check("watcher: final rename after download is scanned",
      any("eicar_com.zip" in x for x in seen5), str(seen5))

# 6. Chromium-style download temps (random names, .org.chromium.Chromium.xxxx)
# must ALSO be ignored — scanning them mid-download raced the browser and
# produced "Can't access file" ERRORs + error sounds on every download
w6 = tempfile.mkdtemp(prefix="horidoro-chromium-")
seen6 = []
watcher6 = Watcher([w6], seen6.append)
watcher6.start()
time.sleep(0.5)
chromium_partial = os.path.join(w6, ".org.chromium.Chromium.6rZ7YK")
open(chromium_partial, "w").write("half of a download")
time.sleep(2.5)
os.rename(chromium_partial, os.path.join(w6, "eicar_com.zip"))
time.sleep(3.0)
watcher6.stop()
check("watcher: Chromium temp partial is ignored",
      not any(".org.chromium.Chromium" in x for x in seen6), str(seen6))
check("watcher: final rename after Chromium download is scanned",
      any("eicar_com.zip" in x for x in seen6), str(seen6))

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("ALL WATCHER/CACHE TESTS PASSED")
