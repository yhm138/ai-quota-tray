"""Self-update: find a newer GitHub release and hand off to update.ps1.

The heavy lifting (download, checksum, swapping files, restarting) lives in
update.ps1, the same script people run by hand, so there is one update path.
The running app only has to find it, launch it detached, and exit.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import __version__
from .config import install_dir

log = logging.getLogger(__name__)

DEFAULT_REPO = "yhm138/ai-quota-tray"


@dataclass
class Release:
    tag: str
    url: str


def parse_version(text: str) -> tuple[int, ...]:
    """'v1.2.3' / '1.2' / 'v1.2.3-beta' -> (1, 2, 3). Unparseable -> ()."""
    m = re.match(r"^\s*v?(\d+(?:\.\d+)*)", text or "")
    if not m:
        return ()
    parts = [int(p) for p in m.group(1).split(".")]
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def is_newer(tag: str, current: str = __version__) -> bool:
    new, cur = parse_version(tag), parse_version(current)
    return bool(new) and bool(cur) and new > cur


def latest_release(repo: str = DEFAULT_REPO) -> Release | None:
    """The newest published release, or None when it can't be determined."""
    from .providers.base import session

    try:
        resp = session().get(
            f"https://api.github.com/repos/{repo}/releases/latest",
            headers={"Accept": "application/vnd.github+json", "User-Agent": "QuotaTray-updater"},
            timeout=15,
        )
    except Exception:                                           # noqa: BLE001
        log.info("update check failed", exc_info=True)
        return None
    if resp.status_code != 200:
        log.info("update check: HTTP %s", resp.status_code)
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    tag = data.get("tag_name")
    if not isinstance(tag, str) or not tag:
        return None
    return Release(tag=tag, url=data.get("html_url") or f"https://github.com/{repo}/releases")


def check(repo: str = DEFAULT_REPO) -> Release | None:
    """The latest release if it is newer than this build, else None."""
    rel = latest_release(repo)
    return rel if rel and is_newer(rel.tag) else None


def program_dir() -> Path:
    """Folder update.ps1 should update: the exe's folder or the source root."""
    return install_dir()


def _update_script(repo: str, tag: str) -> Path:
    """Fetch update.ps1 as published with the target release; fall back to ours."""
    from .providers.base import session

    target = Path(tempfile.gettempdir()) / "QuotaTray-update.ps1"
    try:
        resp = session().get(
            f"https://raw.githubusercontent.com/{repo}/{tag}/update.ps1", timeout=20
        )
        if resp.status_code == 200 and "Invoke-QuotaTrayUpdate" in resp.text:
            target.write_text(resp.text, encoding="utf-8-sig")
            return target
    except Exception:                                           # noqa: BLE001
        log.info("could not download update.ps1", exc_info=True)
    local = program_dir() / "update.ps1"
    if local.is_file():
        return local
    raise RuntimeError("update.ps1 is not available; download the release manually")


def launch(release: Release, repo: str = DEFAULT_REPO) -> None:
    """Start update.ps1 detached. The caller must exit right after this."""
    if sys.platform != "win32":
        raise RuntimeError("self-update is only supported on Windows")
    script = _update_script(repo, release.tag)
    args = [
        "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-WindowStyle", "Hidden", "-File", str(script),
        "-Repo", repo, "-Tag", release.tag,
        "-InstallDir", str(program_dir()),
        "-WaitPid", str(os.getpid()),
    ]
    flags = 0x00000008 | 0x00000200 | 0x08000000   # DETACHED | NEW_GROUP | NO_WINDOW
    log.info("launching updater for %s: %s", release.tag, script)
    subprocess.Popen(args, creationflags=flags, close_fds=True, cwd=str(program_dir()))
