# Security

## Reporting a problem

Found a bug, a crash, or something that behaves incorrectly? Open an issue on
the GitHub repository with:

- Which version you are running (About tab shows the version)
- What you did, what you expected, and what happened instead
- Any relevant log lines from `~/Documents/horidoro-av/logs/`

For **security-sensitive issues** (something that could be exploited), please
report privately before opening a public issue.

## False positives

Horidoro AV is signature-based, so it can flag legitimate files (bundled
JavaScript, app.asar archives, installers with packers, and similar). If you
believe a file was flagged incorrectly:

1. **Restore** it from the Quarantine tab (you choose the destination folder).
2. If it happens repeatedly, add an **exclusion** in Settings → Exclusions.
3. Consider reporting the false positive to the ClamAV project so the
   signature can be improved for everyone.

## Signature updates

Virus definitions come from ClamAV's public mirrors:

- Automatically **before every scheduled scan** (when scans are enabled).
- On demand via the **Update virus DB** button.
- The **Add recommended databases** button adds well-known community feeds
  (rfxn, urlhaus, sanesecurity, blurl, junk, shell) that complement the
  official ClamAV databases.

Keep your definitions current — protection is only as good as the signatures.

## Engine credit

The antivirus engine is **ClamAV**, an open-source antivirus toolkit
distributed under the GPL. Horidoro AV orchestrates it; ClamAV is developed
and maintained by its own project. https://www.clamav.net/
