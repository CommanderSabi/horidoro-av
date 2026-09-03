# -*- coding: utf-8 -*-
"""Horidoro AV — on-access watcher + hash cache.

On-access: watch user-chosen folders with inotify (kernel file events) and
scan new files the moment they land. Fully local, no polling.

Hash cache: SQLite record of (path -> sha256 + inode/size/mtime signature) so
unchanged files are never rescanned — the big repeat-scan speedup.

Design notes:
  - inotify via ctypes (stdlib only; no python3-inotify dependency — the app
    must run on stock Fedora Atomic).
  - The Watcher is recursive: subdirectories are watched as they appear.
  - Per-path debounce coalesces burst writes into a single scan.
  - Excluded directories (the app's own quarantine/logs/tmp, user exclusions)
    are never watched, so the app can never scan its own output.
"""

import ctypes
import errno
import hashlib
import os
import select
import sqlite3
import stat
import struct
import threading
import time
from pathlib import Path

from branding import MAX_SCAN_BYTES

# --- inotify constants -------------------------------------------------------
IN_ACCESS = 0x00000001
IN_MODIFY = 0x00000002
IN_ATTRIB = 0x00000004
IN_CLOSE_WRITE = 0x00000008
IN_CLOSE_NOWRITE = 0x00000010
IN_OPEN = 0x00000020
IN_MOVED_FROM = 0x00000040
IN_MOVED_TO = 0x00000080
IN_CREATE = 0x00000100
IN_DELETE = 0x00000200
IN_DELETE_SELF = 0x00000400
IN_MOVE_SELF = 0x00000800
IN_UNMOUNT = 0x00002000
IN_Q_OVERFLOW = 0x00004000
IN_IGNORED = 0x00008000
IN_ISDIR = 0x40000000
IN_ONLYDIR = 0x01000000

# Events we care about:
#   IN_CLOSE_WRITE — a file finished being written (the reliable "done" event)
#   IN_MOVED_TO    — a file/dir landed (rename into place)
#   IN_CREATE      — needed to notice NEW subdirectories (IN_ISDIR set)
#   IN_DELETE      — a watched directory disappeared
#   IN_DELETE_SELF / IN_MOVE_SELF — the watched root itself moved/deleted
_WATCH_MASK = (IN_CLOSE_WRITE | IN_MOVED_TO | IN_CREATE | IN_DELETE
               | IN_DELETE_SELF | IN_MOVE_SELF | IN_ONLYDIR)

# Browser/downloader in-progress markers: never scanned. The completed
# download arrives as a rename to the final name — THAT is what gets scanned
# (scanning the partials is wasteful and can race the downloader).
_SKIP_SUFFIXES = (".crdownload", ".part", ".partial", ".download",
                  ".opdownload", ".tmp")
# Chromium-style download temps have RANDOM names (.org.chromium.Chromium.xxxx)
# — no suffix to catch, so match the basename prefix (found live on Bazzite:
# scanning them mid-download raced the browser -> "Can't access file" ERRORs
# + an-error-occurred sound on every download).
_SKIP_PREFIXES = (".org.chromium.Chromium.", ".com.google.Chrome.",
                  ".com.brave.Browser.", ".com.microsoft.Edge.")

_libc = ctypes.CDLL(None, use_errno=True)
_libc.inotify_init1.argtypes = [ctypes.c_int]
_libc.inotify_init1.restype = ctypes.c_int
_libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
_libc.inotify_add_watch.restype = ctypes.c_int
_libc.inotify_rm_watch.argtypes = [ctypes.c_int, ctypes.c_int]
_libc.inotify_rm_watch.restype = ctypes.c_int
_libc.read.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t]
_libc.read.restype = ctypes.c_ssize_t
_libc.close.argtypes = [ctypes.c_int]
_libc.close.restype = ctypes.c_int

# inotify_event: wd (int32), mask (uint32), cookie (uint32), len (uint32)
_INOTIFY_EVENT = struct.Struct("@iIII")


# ---------------------------------------------------------------------------
# HashCache — SQLite record of scanned files
# ---------------------------------------------------------------------------
class HashCache:
    """Remember (path -> sha256 + signature + result) to skip rescans.

    Fast path: a file whose (inode, size, mtime) signature matches a stored
    clean record is skipped with a single stat() — no hashing, no scanning.
    If only metadata changed (touch/move), verify_clean() re-hashes and, on a
    content match, refreshes the signature without scanning.
    """

    MAX_ROWS = 200_000  # prune the cache to the newest N entries
    _PRUNE_EVERY = 50   # prune once per N writes

    def __init__(self, db_path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), timeout=30,
                                     check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")   # readers never block
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS scans (
                path       TEXT PRIMARY KEY,
                sha256     TEXT NOT NULL,
                ino        INTEGER NOT NULL,
                size       INTEGER NOT NULL,
                mtime      REAL NOT NULL,
                result     TEXT NOT NULL,
                scanned_at REAL NOT NULL,
                db_key     TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_scans_scanned_at ON scans(scanned_at);
        """)
        # migrate pre-db_key databases (old rows -> '' -> treated as stale,
        # so they are rescanned once and re-recorded with the current key)
        try:
            self._conn.execute(
                "ALTER TABLE scans ADD COLUMN db_key TEXT NOT NULL DEFAULT ''")
            self._conn.commit()
        except sqlite3.Error:
            pass  # column already exists
        self._writes = 0
        self._lock = threading.Lock()

    def close(self):
        try:
            self._conn.close()
        except sqlite3.Error:
            pass

    # --- internals ---------------------------------------------------------
    @staticmethod
    def _sig(path):
        """(ino, size, mtime) for regular, scannable files, else None."""
        try:
            st = os.stat(path)
        except OSError:
            return None
        if not stat.S_ISREG(st.st_mode):
            return None
        if st.st_size > MAX_SCAN_BYTES:   # ClamAV hard limit — never scannable
            return None
        return (st.st_ino, st.st_size, st.st_mtime)

    @staticmethod
    def _sha256(path):
        """Content hash (1 MiB chunks). None if the file is unreadable."""
        h = hashlib.sha256()
        try:
            with open(path, "rb") as f:
                while True:
                    chunk = f.read(1 << 20)
                    if not chunk:
                        break
                    h.update(chunk)
        except OSError:
            return None
        return h.hexdigest()

    def _prune(self):
        self._writes += 1
        if self._writes % self._PRUNE_EVERY:
            return
        try:
            self._conn.execute(
                "DELETE FROM scans WHERE path NOT IN "
                "(SELECT path FROM scans ORDER BY scanned_at DESC LIMIT ?)",
                (self.MAX_ROWS,))
            self._conn.commit()
        except sqlite3.Error:
            pass

    # --- public API ----------------------------------------------------------
    def known_clean(self, path, db_key=""):
        """True if the file is unchanged since a clean scan (stat-only).
        Requires the stored signature key to match: when the definitions
        change, cached-clean files are re-scanned with the new signatures."""
        sig = self._sig(path)
        if sig is None:
            return False
        ino, size, mtime = sig
        with self._lock:
            row = self._conn.execute(
                "SELECT ino, size, mtime, result, db_key FROM scans WHERE path=?",
                (str(path),)).fetchone()
        return bool(row and row[3] == "clean" and row[4] == db_key
                    and row[0] == ino and row[1] == size and row[2] == mtime)

    def verify_clean(self, path, db_key=""):
        """Re-hash a changed-metadata file; skip the scan if content matches
        the stored clean record (and refresh its signature). Only hashes files
        that were previously recorded — brand-new paths return False with no
        wasted hashing (keeps first-run scans fast)."""
        sig = self._sig(path)
        if sig is None:
            return False
        with self._lock:
            row = self._conn.execute(
                "SELECT sha256, result, db_key FROM scans WHERE path=?",
                (str(path),)).fetchone()
        if row is None:
            return False  # never scanned before — nothing to verify against
        if row[2] != db_key:
            return False  # definitions changed — rescan with new signatures
        digest = self._sha256(path)
        if digest is None:
            return False
        if row[1] == "clean" and row[0] == digest:
            with self._lock:
                ino, size, mtime = sig
                self._conn.execute(
                    "UPDATE scans SET ino=?, size=?, mtime=?, scanned_at=? "
                    "WHERE path=?",
                    (ino, size, mtime, time.time(), str(path)))
                self._conn.commit()
                self._prune()
            return True
        return False

    def record_clean(self, path, db_key=""):
        """Hash the file and store a clean result. False if unhashable."""
        sig = self._sig(path)
        if sig is None:
            return False
        digest = self._sha256(path)
        if digest is None:
            return False
        ino, size, mtime = sig
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO scans "
                "(path, sha256, ino, size, mtime, result, scanned_at, db_key) "
                "VALUES (?, ?, ?, ?, ?, 'clean', ?, ?)",
                (str(path), digest, ino, size, mtime, time.time(), db_key))
            self._conn.commit()
            self._prune()
        return True

    def invalidate(self, path):
        """Forget a path (used when a file was moved to quarantine)."""
        with self._lock:
            self._conn.execute("DELETE FROM scans WHERE path=?", (str(path),))
            self._conn.commit()

    def count(self):
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM scans").fetchone()[0]


# ---------------------------------------------------------------------------
# Watcher — recursive inotify listener
# ---------------------------------------------------------------------------
class Watcher:
    """Watches folders recursively; calls on_file(path) for new/modified files.

    Events are debounced per path (DEBOUNCE seconds) so burst writes coalesce
    into one callback. The optional HashCache short-circuits files that were
    already scanned and unchanged.
    """

    DEBOUNCE = 1.5          # seconds to wait for a path to settle
    _LOOP_SLICE = 0.25      # drain/debounce resolution
    _SELECT_TIMEOUT = 0.5   # select() wait — keeps stop() responsive

    def __init__(self, folders, on_file, exclude=(), on_overflow=None):
        self.folders = [str(Path(f).resolve()) for f in folders]
        self.on_file = on_file
        self.on_overflow = on_overflow
        self.exclude = [str(Path(e).resolve()) for e in exclude]
        self._fd = -1
        self._watches = {}      # wd -> directory path
        self._paths = {}        # directory path -> wd
        self._pending = {}      # file path -> deadline (debounce)
        self._stop = threading.Event()
        self._thread = None
        self.watch_count = 0
        self.overflow_events = 0
        self.degraded = None    # set if the inotify watch limit was hit

    # --- helpers ------------------------------------------------------------
    def _is_excluded(self, path):
        path = str(Path(path).resolve())
        return any(path == e or path.startswith(e + os.sep) for e in self.exclude)

    def _add_watch(self, path):
        if path in self._paths or self._is_excluded(path):
            return
        wd = _libc.inotify_add_watch(self._fd, os.fsencode(path), _WATCH_MASK)
        if wd < 0:
            err = ctypes.get_errno()
            if err in (errno.ENOSPC, errno.ENOMEM) and not self.degraded:
                self.degraded = (
                    "inotify watch limit reached — raise "
                    "/proc/sys/fs/inotify/max_user_watches for full coverage")
            return
        self._watches[wd] = path
        self._paths[path] = wd
        self.watch_count += 1

    def _add_tree(self, root, emit_existing=False):
        """Breadth-first watch of every subdirectory (skips excluded dirs).

        With emit_existing=True (new directory appeared), pre-existing regular
        files are emitted too — they are new to the watched tree and closing
        the race where files land before the subdir watch is in place.
        """
        root = str(Path(root).resolve())
        if root not in self._paths:
            self._add_watch(root)
        stack = [root]
        while stack:
            base = stack.pop()
            try:
                names = os.listdir(base)
            except OSError:
                continue
            for name in names:
                child = os.path.join(base, name)
                try:
                    if os.path.isdir(child) and not os.path.islink(child):
                        if not self._is_excluded(child):
                            self._add_watch(child)
                            stack.append(child)
                    elif emit_existing and os.path.isfile(child):
                        self._emit(child)
                except OSError:
                    continue

    def _forget_tree(self, root):
        root = str(Path(root).resolve())
        for path, wd in list(self._paths.items()):
            if path == root or path.startswith(root + os.sep):
                _libc.inotify_rm_watch(self._fd, wd)
                del self._watches[wd]
                del self._paths[path]
                self.watch_count -= 1

    def _queue(self, path):
        self._pending[path] = time.monotonic() + self.DEBOUNCE

    def _drain(self):
        now = time.monotonic()
        ready = [p for p, deadline in self._pending.items() if deadline <= now]
        for p in ready:
            del self._pending[p]
        for p in sorted(ready):
            self._emit(p)

    def _emit(self, path):
        if self._is_excluded(path):
            return
        base = os.path.basename(path)
        if path.lower().endswith(_SKIP_SUFFIXES) or \
                base.startswith(_SKIP_PREFIXES):
            return  # download-in-progress marker — the rename will be scanned
        try:
            st = os.stat(path)
        except OSError:
            return
        if not stat.S_ISREG(st.st_mode):
            return
        if st.st_size > MAX_SCAN_BYTES:   # ClamAV hard limit — cannot scan
            return
        try:
            self.on_file(path)
        except Exception:  # Fight the Fairies — callback errors never kill the watcher
            pass

    def _handle(self, wd, mask, name):
        directory = self._watches.get(wd)
        if directory is None:
            return
        path = os.path.join(directory, name) if name else directory
        if mask & IN_ISDIR:
            if mask & (IN_CREATE | IN_MOVED_TO):
                self._add_tree(path, emit_existing=True)
            elif mask & (IN_DELETE | IN_MOVED_FROM):
                self._forget_tree(path)
            return
        if mask & (IN_CLOSE_WRITE | IN_MOVED_TO):
            self._queue(path)
        elif mask & (IN_DELETE_SELF | IN_MOVE_SELF):
            self._forget_tree(directory)

    # --- lifecycle ------------------------------------------------------------
    def start(self, on_ready=None):
        """Begin watching in a background thread. on_ready(count) fires after
        the initial recursive tree walk."""
        if self._thread and self._thread.is_alive():
            return self._thread
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, args=(on_ready,), daemon=True)
        self._thread.start()
        return self._thread

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        if self._fd >= 0:
            _libc.close(self._fd)
            self._fd = -1

    def _run(self, on_ready):
        self._fd = _libc.inotify_init1(0)
        if self._fd < 0:
            return
        for folder in self.folders:
            self._add_tree(folder)
        if on_ready:
            try:
                on_ready(self.watch_count)
            except Exception:  # noqa: BLE001
                pass

        buf = ctypes.create_string_buffer(65536)
        while not self._stop.is_set():
            if select.select([self._fd], [], [], self._SELECT_TIMEOUT)[0]:
                n = _libc.read(self._fd, buf, len(buf))
                if n <= 0:
                    continue
                data = buf.raw[:n]
                off = 0
                while off + _INOTIFY_EVENT.size <= n:
                    wd, mask, _cookie, ln = _INOTIFY_EVENT.unpack_from(data, off)
                    name = ""
                    if ln:
                        name = data[off + _INOTIFY_EVENT.size:
                                    off + _INOTIFY_EVENT.size + ln]
                        name = name.split(b"\0", 1)[0].decode("utf-8", "replace")
                    off += _INOTIFY_EVENT.size + ln
                    if mask & IN_Q_OVERFLOW:
                        self.overflow_events += 1
                        if self.on_overflow:
                            try:
                                self.on_overflow(self.overflow_events)
                            except Exception:  # noqa: BLE001
                                pass
                        continue
                    if mask & IN_IGNORED:
                        continue
                    self._handle(wd, mask, name)
            self._drain()
