"""Run subprocesses and PowerShell without flashing a console window."""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys

log = logging.getLogger(__name__)

CREATE_NO_WINDOW = 0x08000000


def independent_env(base: dict | None = None) -> dict:
    """Environment for starting a *separate* QuotaTray (after an update, or a
    self-restart).

    A PyInstaller one-file exe started from inside another one inherits its
    _PYI_* variables, takes itself for that app's child and reuses the
    parent's %TEMP%\\_MEIxxxx folder instead of unpacking its own. When the
    parent exits it deletes that folder under the new process ("Failed to
    remove temporary directory"), whose later imports then fail (Tcl,
    cryptography). PYINSTALLER_RESET_ENVIRONMENT=1 (PyInstaller 6.9+) tells
    the bootloader to start fresh; dropping the variables covers older ones.
    """
    env = {
        k: v for k, v in (os.environ if base is None else base).items()
        if not k.upper().startswith("_PYI_") and k.upper() not in ("_MEIPASS2", "_PYI_SPLASH_IPC")
    }
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return env


def _kwargs() -> dict:
    kw: dict = {}
    if sys.platform == "win32":
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = subprocess.SW_HIDE
        kw["startupinfo"] = si
        kw["creationflags"] = CREATE_NO_WINDOW
    return kw


def run(cmd, timeout: float = 20.0, encoding: str = "utf-8") -> tuple[int, str, str]:
    """Run a command and return (returncode, stdout, stderr). Never raises."""
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout,
            **_kwargs(),
        )
    except FileNotFoundError:
        return 127, "", "command not found"
    except subprocess.TimeoutExpired:
        return 124, "", "timed out"
    except Exception as exc:                                    # noqa: BLE001
        return 1, "", str(exc)

    def dec(raw: bytes) -> str:
        for enc in (encoding, "utf-8", "gbk", "utf-16-le"):
            try:
                return raw.decode(enc)
            except (UnicodeDecodeError, LookupError):
                continue
        return raw.decode("utf-8", "replace")

    return proc.returncode, dec(proc.stdout or b""), dec(proc.stderr or b"")


def powershell_json(script: str, timeout: float = 25.0):
    """Run a PowerShell snippet and parse stdout as JSON. None on failure."""
    if sys.platform != "win32":
        return None
    exe = "powershell.exe"
    code, out, err = run(
        [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
        timeout=timeout,
    )
    if code != 0 and not out.strip():
        log.debug("powershell failed rc=%s err=%s", code, err.strip()[:400])
        return None
    text = out.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        log.debug("powershell output was not JSON: %s", text[:300])
        return None


def popen(cmd, **extra):
    """For long-lived children such as `codex app-server`."""
    return subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        **_kwargs(),
        **extra,
    )
