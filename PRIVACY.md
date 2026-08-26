# Privacy

**Horidoro AV is 100% local. Nothing you scan is ever sent anywhere.**

There is no account, no cloud service, no telemetry, and no analytics.

## The only network contacts (both optional in practice)

1. **Virus definition updates** — the app downloads signature updates from
   ClamAV's public mirrors (either automatically, before scheduled scans, or
   when you press "Update virus DB"). This is what makes detection work.
2. **App update check** — the app checks GitHub's public Releases page for a
   newer version. With the "Check for updates automatically" toggle on
   (default), this runs in the background roughly every 12 hours (via a
   systemd timer), plus on launch and every 6 hours while the app is open,
   and there's a "Check now" button in Settings → App updates. The check
   reads only the release version number — it sends nothing. Update
   downloads are verified against the published SHA-256 checksum before
   installing, only replace the app file itself, and play a completion sound
   (unless sounds are off).

Everything else happens entirely on your machine.

## What is stored, and where

All data lives under your own user account:

| What | Where |
|---|---|
| State / settings (install state, scan targets, schedule, exclusions) | `~/.config/horidoro-av/state.json` |
| Scan cache (file hashes, so unchanged files are not rescanned) | `~/.config/horidoro-av/cache.db` |
| Quarantined files | `~/.local/share/horidoro/quarantine/` |
| Scan logs (bounded, never unlimited) | `~/.local/share/horidoro/logs/` |
| Scan scripts | `~/.local/share/horidoro/scripts/` |
| Notification sounds | `~/.local/share/horidoro/sounds/` |

## What happens to files you scan

- Files are read only to scan them. Their **content never leaves your
  computer**.
- Detected files are moved into your local quarantine folder (renamed, not
  uploaded).
- The hash cache stores only a SHA-256 digest and file metadata — not the
  file contents.
- Where a quarantined file came from is recorded locally (`origin.json` in
  the quarantine folder) so you can restore it to its original location.

## The engine container

The ClamAV engine runs inside a `clamav` container managed by
distrobox/podman on your machine. It never connects anywhere except to
ClamAV's signature mirrors to update the virus definitions.

## Removing everything

Uninstall (Settings) permanently removes the app and everything it created —
settings, logs, quarantined files, and the engine container. A warning
dialog confirms first, because this cannot be undone.
