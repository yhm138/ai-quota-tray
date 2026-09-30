"""In-process updater: the running app checks, downloads, verifies and
installs the new version while it keeps running and showing progress, and
only then hands over to the new version.

A running .exe cannot be overwritten on Windows, but it can be renamed. So
the one-file build moves itself into a hidden .quotatray-trash folder (the
new version deletes it for good once it runs) and puts the new file in its
place before exiting; the portable build unpacks next to itself and a small
helper copies it over once this process has exited; a source install is
updated in place (Python has already loaded what it needs).
"""
from __future__ import annotations

import hashlib
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Callable

from . import __version__
from .config import install_dir

log = logging.getLogger(__name__)

# progress(step, total_steps, text, fraction_or_None)
Progress = Callable[[int, int, str, "float | None"], None]
STEPS = 5          # check, download, verify, install, restart

DETACHED = 0x00000008 | 0x00000200 | 0x08000000   # DETACHED | NEW_GROUP | NO_WINDOW


class UpdateError(RuntimeError):
    pass


def install_kind() -> str:
    if not getattr(sys, "frozen", False):
        return "source"
    return "portable" if (Path(sys.executable).parent / "_internal").is_dir() else "exe"


def arch() -> str:
    machine = (platform.machine() or "").lower()
    return "arm64" if machine in ("arm64", "aarch64") else "x64"


def asset_names(tag: str, kind: str) -> tuple[list[str], list[str]]:
    """(payload candidates, checksum-file candidates), newest naming first;
    releases before v1.3.2 used plain names."""
    base = f"QuotaTray-{tag}-windows-{arch()}"
    payload = {
        "exe": [f"{base}.exe", "QuotaTray.exe"],
        "portable": [f"{base}-portable.zip", "QuotaTray-portable.zip"],
    }.get(kind, [])
    return payload, [f"QuotaTray-{tag}-SHA256SUMS.txt", "SHA256SUMS.txt"]


def parse_sums(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        m = re.match(r"^\s*([0-9a-fA-F]{64})\s+\*?(\S.*?)\s*$", line)
        if m:
            out[m.group(2)] = m.group(1).lower()
    return out


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _mb(n: float) -> str:
    return f"{n / 1048576:.1f} MB"


def _download(url: str, dest: Path, progress: Callable[[int, int], None] | None = None) -> bool:
    """Stream url to dest. False on 404 (asset not in this release)."""
    from .providers.base import session

    resp = session().get(url, stream=True, timeout=30,
                         headers={"User-Agent": f"QuotaTray/{__version__}"})
    try:
        if resp.status_code == 404:
            return False
        if resp.status_code != 200:
            raise UpdateError(f"download failed: HTTP {resp.status_code} for {url.rsplit('/', 1)[-1]}")
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        tmp = dest.with_name(dest.name + ".part")
        with tmp.open("wb") as fh:
            for chunk in resp.iter_content(chunk_size=1 << 16):
                if not chunk:
                    continue
                fh.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
        os.replace(tmp, dest)
        return True
    finally:
        resp.close()


def _fetch_first(repo: str, tag: str, names: list[str], dest_dir: Path,
                 progress: Callable[[int, int], None] | None = None) -> Path:
    base = f"https://github.com/{repo}/releases/download/{tag}"
    for name in names:
        dest = dest_dir / name
        if _download(f"{base}/{name}", dest, progress):
            return dest
    raise UpdateError(f"{tag} has none of: {', '.join(names)}")


def _quote_ps(path: Path | str) -> str:
    return "'" + str(path).replace("'", "''") + "'"


class Plan:
    """What to do once the new version is in place: how to start it."""

    def __init__(self, command: list[str], note: str):
        self.command = command
        self.note = note

    def launch(self) -> None:
        from .win.proc import independent_env

        kwargs = {"creationflags": DETACHED} if sys.platform == "win32" else {}
        # A clean environment, or the new exe would reuse (and lose) our
        # unpacked files; see independent_env.
        subprocess.Popen(self.command, close_fds=True, env=independent_env(), **kwargs)


def install(release_tag: str, repo: str, progress: Progress) -> Plan:
    """Do everything short of exiting. Raises UpdateError with a readable
    reason; on failure the running version is left exactly as it was."""
    kind = install_kind()
    pid = str(os.getpid())
    # --fresh: started with a clean environment (see independent_env).
    after = ["--wait-pid", pid, "--updated-from", __version__, "--fresh"]

    if kind == "source":
        return _install_source(release_tag, repo, progress, after)

    exe = Path(sys.executable)
    folder = exe.parent
    if not os.access(folder, os.W_OK):
        raise UpdateError(f"{folder} is not writable; move QuotaTray to a folder you own")

    # 1. check
    progress(1, STEPS, f"Checking {release_tag}...", None)
    payload_names, sums_names = asset_names(release_tag, kind)
    work = Path(tempfile.mkdtemp(prefix="quotatray-update-", dir=str(folder.parent)
                                 if kind == "portable" else None))
    try:
        sums_file = _fetch_first(repo, release_tag, sums_names, work)
        sums = parse_sums(sums_file.read_text(encoding="utf-8", errors="replace"))

        # 2. download (next to the exe, so the final rename stays on one volume)
        def on_bytes(done: int, total: int) -> None:
            if total:
                progress(2, STEPS, f"Downloading {_mb(done)} / {_mb(total)}", done / total)
            else:
                progress(2, STEPS, f"Downloading {_mb(done)}", None)

        # Into a private folder beside the exe: same volume for the final
        # rename, and never under the running file's own name.
        target_dir = folder / ".quotatray-update" if kind == "exe" else work
        target_dir.mkdir(exist_ok=True)
        payload = _fetch_first(repo, release_tag, payload_names, target_dir, on_bytes)

        # 3. verify
        progress(3, STEPS, "Verifying the download...", None)
        want = sums.get(payload.name)
        if not want:
            raise UpdateError(f"the checksum list has no entry for {payload.name}")
        got = sha256(payload)
        if got != want:
            raise UpdateError(f"checksum mismatch for {payload.name}")

        # 4. install
        progress(4, STEPS, "Installing...", None)
        if kind == "exe":
            new = exe.with_name(exe.name + ".new")
            os.replace(payload, new)
            # A running exe cannot be deleted, only renamed: park it in a
            # hidden folder that the new version empties once this one exits.
            trash = folder / TRASH_DIR
            trash.mkdir(exist_ok=True)
            _hide(trash)
            old = trash / f"{exe.name}.{os.getpid()}"
            os.replace(exe, old)
            try:
                os.replace(new, exe)
            except OSError:
                os.replace(old, exe)             # put the running version back
                raise
            return Plan([str(exe)] + after, f"Restarting into {release_tag}...")

        # portable: unpack beside the install; copy over once we have exited
        staged = folder.with_name(folder.name + ".update")
        shutil.rmtree(staged, ignore_errors=True)
        with zipfile.ZipFile(payload) as zf:
            zf.extractall(staged)
        if not (staged / exe.name).is_file() and not (staged / "QuotaTray.exe").is_file():
            raise UpdateError("the portable zip does not contain QuotaTray.exe")
        if not (staged / exe.name).is_file():
            os.replace(staged / "QuotaTray.exe", staged / exe.name)
        script = (
            f"Wait-Process -Id {pid} -Timeout 60 -ErrorAction SilentlyContinue; Start-Sleep 1; "
            f"robocopy {_quote_ps(staged)} {_quote_ps(folder)} /E /R:10 /W:1 /NFL /NDL /NJH /NJS /NP | Out-Null; "
            f"if ($LASTEXITCODE -lt 8) {{ Remove-Item -LiteralPath {_quote_ps(staged)} -Recurse -Force }}; "
            f"Start-Process -FilePath {_quote_ps(exe)} -ArgumentList '--updated-from {__version__}'"
        )
        return Plan(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                     "-WindowStyle", "Hidden", "-Command", script],
                    f"Restarting into {release_tag}...")
    finally:
        shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(folder / ".quotatray-update", ignore_errors=True)
        for leftover in folder.glob(exe.name + ".new"):
            try:
                leftover.unlink()
            except OSError:
                pass


def _install_source(tag: str, repo: str, progress: Progress, after: list[str]) -> Plan:
    root = install_dir()
    progress(1, STEPS, f"Checking {tag}...", None)
    work = Path(tempfile.mkdtemp(prefix="quotatray-update-"))
    try:
        def on_bytes(done: int, total: int) -> None:
            progress(2, STEPS, f"Downloading source {_mb(done)}", (done / total) if total else None)

        archive = work / "source.zip"
        if not _download(f"https://github.com/{repo}/archive/refs/tags/{tag}.zip", archive, on_bytes):
            raise UpdateError(f"no source archive for {tag}")
        progress(3, STEPS, "Unpacking...", None)
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(work / "src")
        inner = next((p for p in (work / "src").iterdir() if p.is_dir()), None)
        if inner is None or not (inner / "run.pyw").is_file():
            raise UpdateError("the source archive does not look like QuotaTray")
        progress(4, STEPS, "Installing...", None)
        skip = {".git", ".venv", "venv", "__pycache__"}
        for item in inner.iterdir():
            if item.name in skip:
                continue
            dest = root / item.name
            if item.is_dir():
                shutil.copytree(item, dest, dirs_exist_ok=True)
            else:
                shutil.copy2(item, dest)
        venv_py = root / ".venv" / "Scripts" / "python.exe"
        if venv_py.is_file():
            progress(4, STEPS, "Updating dependencies...", None)
            subprocess.run([str(venv_py), "-m", "pip", "install", "-q", "--disable-pip-version-check",
                            "-r", str(root / "requirements.txt")], timeout=600,
                           creationflags=0x08000000 if sys.platform == "win32" else 0)
        pyw = root / ".venv" / "Scripts" / "pythonw.exe"
        runner = str(pyw) if pyw.is_file() else sys.executable
        return Plan([runner, str(root / "run.pyw")] + after, f"Restarting into {tag}...")
    finally:
        shutil.rmtree(work, ignore_errors=True)


TRASH_DIR = ".quotatray-trash"


def _hide(path: Path) -> None:
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.kernel32.SetFileAttributesW(str(path), 0x2)   # HIDDEN
        except Exception:                                       # noqa: BLE001
            pass


def cleanup_after_update(folder: Path | None = None, attempts: int = 30, delay: float = 2.0) -> bool:
    """Permanently delete what an update left next to the exe: the previous
    version (parked in .quotatray-trash, or <name>.old from v1.3.2-1.3.4) and
    stray downloads. The previous process may still be exiting, so retry for
    a while. Returns True when nothing is left."""
    import time

    if folder is None:
        if not getattr(sys, "frozen", False):
            return True
        folder = Path(sys.executable).parent
    for _ in range(max(1, attempts)):
        leftovers = [folder / TRASH_DIR, folder / ".quotatray-update",
                     *folder.glob("*.exe.old"), *folder.glob("*.exe.new")]
        remaining = []
        for item in leftovers:
            if not item.exists():
                continue
            try:
                if item.is_dir():
                    shutil.rmtree(item)
                else:
                    item.unlink()
                log.info("removed update leftover %s", item.name)
            except OSError:
                remaining.append(item)
        if not remaining:
            return True
        time.sleep(delay)
    log.info("could not remove %s yet; will retry next start", [p.name for p in remaining])
    return False
