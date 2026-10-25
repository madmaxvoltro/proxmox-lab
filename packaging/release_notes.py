"""Write the GitHub release notes for a tag: that version's CHANGELOG.md section first, then the install commands.

    python packaging/release_notes.py v0.5.0 OWNER/REPO > notes.md

Fails (exit 1) when CHANGELOG.md has no non-empty section for the version, so a release can never go out
without its changelog."""

import re
import sys
from pathlib import Path

INSTALL = """\
## Install

**Proxmox host** (as root):
```bash
curl -fsSL https://raw.githubusercontent.com/{repo}/main/install.sh | bash
```

**Windows PC** (PowerShell):
```powershell
irm https://raw.githubusercontent.com/{repo}/main/install.ps1 | iex
```

**macOS or Linux computer**:
```bash
curl -fsSL https://raw.githubusercontent.com/{repo}/main/install-client.sh | bash
```

Every installer checks the download against `SHA256SUMS`. The programs are not code-signed.
"""


def section(changelog: str, version: str) -> str:
    """The body under `## <version>` (a heading may carry a suffix like `(first release)`)."""
    m = re.search(rf"^## {re.escape(version)}(?![\w.])[^\n]*\n(.*?)(?=^## |\Z)", changelog, re.S | re.M)
    return m.group(1).strip() if m else ""


def notes(changelog: str, tag: str, repo: str) -> str:
    version = tag.lstrip("v")
    body = section(changelog, version)
    if not body:
        raise SystemExit(f"CHANGELOG.md has no entry for {version}: write it before tagging {tag}")
    return f"## What's changed in {tag}\n\n{body}\n\n{INSTALL.format(repo=repo)}"


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: release_notes.py TAG OWNER/REPO")
    sys.stdout.write(notes(Path("CHANGELOG.md").read_text(encoding="utf-8"), sys.argv[1], sys.argv[2]))
