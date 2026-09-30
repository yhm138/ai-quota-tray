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


def fetch_latest(repo: str = DEFAULT_REPO) -> tuple[Release | None, str | None]:
    """(newest release, None) or (None, why it could not be determined).

    Asks the GitHub API first; when that fails (it allows 60 anonymous calls
    an hour, and some networks block it) the release page's redirect to
    /releases/tag/<tag> on github.com gives the same answer.
    """
    from .providers.base import session

    problems = []
    try:
        resp = session().get(
            f"https://api.github.com/repos/{repo}/releases/latest",
            headers={"Accept": "application/vnd.github+json", "User-Agent": "QuotaTray-updater"},
            timeout=15,
        )
        if resp.status_code == 200:
            data = resp.json()
            tag = data.get("tag_name") if isinstance(data, dict) else None
            if isinstance(tag, str) and tag:
                return Release(tag, data.get("html_url") or f"https://github.com/{repo}/releases"), None
            problems.append("GitHub API: no tag in the reply")
        else:
            problems.append(f"GitHub API: HTTP {resp.status_code}")
    except Exception as exc:                                    # noqa: BLE001
        problems.append(f"GitHub API: {_short(exc)}")

    try:
        resp = session().get(
            f"https://github.com/{repo}/releases/latest",
            headers={"User-Agent": "QuotaTray-updater"},
            allow_redirects=False,
            timeout=15,
        )
        where = resp.headers.get("Location", "") if resp.status_code in (301, 302, 303, 307, 308) else ""
        m = re.search(r"/releases/tag/([^/?#]+)", where)
        if m:
            return Release(m.group(1), where), None
        problems.append(f"github.com: HTTP {resp.status_code}")
    except Exception as exc:                                    # noqa: BLE001
        problems.append(f"github.com: {_short(exc)}")

    log.info("update check failed: %s", "; ".join(problems))
    return None, "; ".join(problems)


def _short(exc: Exception) -> str:
    text = str(exc) or exc.__class__.__name__
    return text if len(text) <= 120 else text[:117] + "..."


def latest_release(repo: str = DEFAULT_REPO) -> Release | None:
    """The newest published release, or None when it can't be determined."""
    return fetch_latest(repo)[0]


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


def launch(release: Release, repo: str = DEFAULT_REPO):
    """Start update.ps1 detached; returns (process, ready file).

    Keep running until the ready file appears: the script writes it once the
    new version is downloaded and verified, and only then may the app exit
    (the script waits for that, swaps the files and starts the new version).
    If the process ends first, the update failed and the app should stay.
    """
    if sys.platform != "win32":
        raise RuntimeError("self-update is only supported on Windows")
    from .config import app_dir

    script = _update_script(repo, release.tag)
    ready = app_dir() / f"update-ready-{os.getpid()}.txt"
    try:
        ready.unlink()
    except OSError:
        pass
    args = [
        "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-WindowStyle", "Hidden", "-File", str(script),
        "-Repo", repo, "-Tag", release.tag,
        "-InstallDir", str(program_dir()),
        "-WaitPid", str(os.getpid()),
        "-ReadyFile", str(ready),
    ]
    if getattr(sys, "frozen", False):
        # Keep the installed file name (QuotaTray.exe or its download name).
        args += ["-ExeName", Path(sys.executable).name]
    flags = 0x00000008 | 0x00000200 | 0x08000000   # DETACHED | NEW_GROUP | NO_WINDOW
    log.info("launching updater for %s: %s", release.tag, script)
    proc = subprocess.Popen(args, creationflags=flags, close_fds=True, cwd=str(program_dir()))
    return proc, ready
