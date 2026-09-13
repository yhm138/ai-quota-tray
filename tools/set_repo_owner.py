"""Replace the OWNER placeholder in README badges and clone URLs.

publish.bat calls this once it knows the authenticated GitHub account, so the
badges and git clone line point at the real repository.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: set_repo_owner.py <owner> <repo>")
        return 2
    owner, repo = sys.argv[1].strip(), sys.argv[2].strip()
    if not owner or not repo:
        print("owner and repo must not be empty")
        return 2

    changed = []
    for name in ("README.md",):
        path = ROOT / name
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        updated = text.replace("OWNER/QuotaTray", f"{owner}/{repo}")
        if updated != text:
            path.write_text(updated, encoding="utf-8")
            changed.append(name)

    print(f"repository set to {owner}/{repo}")
    print("updated: " + (", ".join(changed) if changed else "nothing (already set)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
