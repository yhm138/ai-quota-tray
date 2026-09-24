"""Find (and stop) QuotaTray copies running from a different install folder.

Only one QuotaTray can run at a time. When someone starts a copy from a new
location, for example a fresh QuotaTray.exe next to an old source checkout,
they mean to switch to it, so the new copy stops the old one rather than
quietly exiting.
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from .proc import powershell_json, run

log = logging.getLogger(__name__)

_PS = (
    "Get-CimInstance Win32_Process -Filter \"Name='QuotaTray.exe' OR Name='pythonw.exe' "
    "OR Name='python.exe'\" | Select-Object ProcessId,Name,ExecutablePath,CommandLine "
    "| ConvertTo-Json -Compress"
)
_RUN_PYW = re.compile(r'"([^"]*run\.pyw)"|(\S*run\.pyw)', re.IGNORECASE)


def _same(a: Path, b: Path) -> bool:
    try:
        return os.path.normcase(str(a.resolve())) == os.path.normcase(str(b.resolve()))
    except OSError:
        return os.path.normcase(str(a)) == os.path.normcase(str(b))


def parse_processes(rows, own_dir: Path, own_pids: set[int]) -> list[tuple[int, Path]]:
    """(pid, install folder) for every QuotaTray process that is not ours."""
    if isinstance(rows, dict):
        rows = [rows]
    out: list[tuple[int, Path]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        pid = row.get("ProcessId")
        if not isinstance(pid, int) or pid in own_pids:
            continue
        name = str(row.get("Name") or "").lower()
        folder: Path | None = None
        if name == "quotatray.exe" and row.get("ExecutablePath"):
            folder = Path(row["ExecutablePath"]).parent
        else:
            m = _RUN_PYW.search(str(row.get("CommandLine") or ""))
            if m:
                folder = Path(m.group(1) or m.group(2)).parent
                # Some other program's run.pyw is none of our business.
                if not (folder / "quota_tray").is_dir():
                    folder = None
        if folder is None or _same(folder, own_dir):
            continue
        out.append((pid, folder))
    return out


def other_copies(own_dir: Path) -> list[tuple[int, Path]]:
    rows = powershell_json(_PS, timeout=20)
    return parse_processes(rows, own_dir, {os.getpid(), os.getppid()})


def stop(pid: int) -> bool:
    code, _out, err = run(["taskkill", "/PID", str(pid), "/T", "/F"], timeout=10)
    if code != 0:
        log.warning("could not stop pid %s: %s", pid, err.strip()[:200])
    return code == 0
