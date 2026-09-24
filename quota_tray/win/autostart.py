"""Run-at-login via HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run."""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from .. import APP_NAME

log = logging.getLogger(__name__)
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _python_exe() -> Path:
    """Which interpreter to use in source mode: prefer the venv's pythonw.exe
    so no console window flashes at login."""
    root = Path(__file__).resolve().parent.parent.parent
    candidates = [
        root / ".venv" / "Scripts" / "pythonw.exe",
        root / "venv" / "Scripts" / "pythonw.exe",
        Path(sys.executable).with_name("pythonw.exe"),
        Path(sys.executable),
    ]
    for cand in candidates:
        try:
            if cand.exists():
                return cand
        except OSError:
            continue
    return Path(sys.executable)


def launch_command() -> str:
    """How this copy of the program should be started, quoted for the registry."""
    # --autostart: started at login, so stay quietly in the tray instead of
    # popping the panel open the way a manual launch does.
    if getattr(sys, "frozen", False):
        return f'"{Path(sys.executable).resolve()}" --autostart'
    root = Path(__file__).resolve().parent.parent.parent
    entry = root / "run.pyw"
    if not entry.exists():                     # fallback: module form
        return f'"{_python_exe()}" -m quota_tray --autostart'
    return f'"{_python_exe()}" "{entry}" --autostart'


def registered_command() -> str | None:
    """The command currently stored in the Run key, if any."""
    if sys.platform != "win32":
        return None
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, APP_NAME)
            return str(value) if value else None
    except OSError:
        return None


def is_enabled() -> bool:
    if sys.platform != "win32":
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, APP_NAME)
            return bool(value)
    except OSError:
        return False


def enable() -> bool:
    if sys.platform != "win32":
        return False
    import winreg

    cmd = launch_command()
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, cmd)
        log.info("run-at-login enabled: %s", cmd)
        return True
    except OSError:
        log.warning("failed to write the autostart registry value", exc_info=True)
        return False


def disable() -> bool:
    if sys.platform != "win32":
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, APP_NAME)
        log.info("run-at-login disabled")
        return True
    except FileNotFoundError:
        return True
    except OSError:
        log.warning("failed to delete the autostart registry value", exc_info=True)
        return False


def open_folder(path: os.PathLike | str) -> None:
    try:
        if sys.platform == "win32":
            os.startfile(str(path))                             # noqa: S606
    except OSError:
        log.warning("failed to open %s", path, exc_info=True)
