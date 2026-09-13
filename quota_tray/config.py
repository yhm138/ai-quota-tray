"""Config, logging, cache and single-instance lock.

Everything lives under %APPDATA%\\QuotaTray (non-Windows falls back to
~/.quota-tray so the module stays importable for testing).
"""
from __future__ import annotations

import json
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from . import APP_NAME

# --------------------------------------------------------------- locations


def app_dir() -> Path:
    appdata = os.environ.get("APPDATA")
    if appdata and sys.platform == "win32":
        base = Path(appdata) / APP_NAME
    else:
        base = Path.home() / ".quota-tray"
    base.mkdir(parents=True, exist_ok=True)
    return base


CONFIG_PATH = lambda: app_dir() / "config.json"          # noqa: E731
CACHE_PATH = lambda: app_dir() / "cache.json"            # noqa: E731
LOG_PATH = lambda: app_dir() / "quota-tray.log"          # noqa: E731
DIAG_PATH = lambda: app_dir() / "diagnostics.txt"        # noqa: E731


def install_dir() -> Path:
    """Directory the program itself lives in (the .exe folder once frozen)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


# --------------------------------------------------------------- defaults

DEFAULTS: dict[str, Any] = {
    "refresh_seconds": 300,
    "refresh_seconds_min": 60,
    "hide_not_installed": True,          # skip products that aren't installed
    "warn_percent": 75,                  # amber threshold
    "danger_percent": 90,                # red threshold
    "icon_style": "bars",                # bars | ring
    "providers": {
        "claude": {
            "enabled": True,
            "percent_scale": "auto",     # auto | percent | fraction
            "oauth_token": "",           # manual fallback: Claude Code OAuth token
            "session_key": "",           # manual fallback: claude.ai sessionKey cookie
            "credentials_path": "",      # manual fallback: .credentials.json path
            "scan_wsl": True,
            "order": ["oauth", "desktop_cookie", "manual_cookie"],
        },
        "codex": {
            "enabled": True,
            "codex_home": "",            # manual fallback: alternate ~/.codex path
            "order": ["wham", "app_server", "jsonl", "sqlite"],
            "app_server_timeout": 20,
            "jsonl_max_days": 14,
        },
        "antigravity": {
            "enabled": True,
            "csrf_token": "",            # manual fallback
            "port": 0,                   # manual fallback
            "show_models": True,
            "max_models": 6,
        },
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class Config:
    def __init__(self, data: dict | None = None):
        self.data = _deep_merge(DEFAULTS, data or {})

    @classmethod
    def load(cls) -> "Config":
        path = CONFIG_PATH()
        if path.exists():
            try:
                return cls(json.loads(path.read_text(encoding="utf-8")))
            except Exception:                                   # noqa: BLE001
                logging.getLogger(__name__).warning(
                    "config.json could not be parsed, falling back to defaults", exc_info=True
                )
        cfg = cls()
        cfg.save()
        return cfg

    def save(self) -> None:
        try:
            CONFIG_PATH().write_text(
                json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except Exception:                                       # noqa: BLE001
            logging.getLogger(__name__).warning("failed to write config.json", exc_info=True)

    # convenience accessors
    def provider(self, pid: str) -> dict:
        return self.data.get("providers", {}).get(pid, {})

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    @property
    def refresh_seconds(self) -> int:
        try:
            v = int(self.data.get("refresh_seconds", 300))
        except (TypeError, ValueError):
            v = 300
        return max(int(self.data.get("refresh_seconds_min", 60)), v)


# --------------------------------------------------------------- cache


def load_cache() -> dict:
    path = CACHE_PATH()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:                                           # noqa: BLE001
        return {}


def save_cache(payload: dict) -> None:
    try:
        CACHE_PATH().write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except Exception:                                           # noqa: BLE001
        pass


# --------------------------------------------------------------- logging


def setup_logging(verbose: bool = False) -> None:
    root = logging.getLogger()
    if root.handlers:
        return
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    try:
        fh = RotatingFileHandler(LOG_PATH(), maxBytes=512_000, backupCount=2, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except Exception:                                           # noqa: BLE001
        pass
    if sys.stderr is not None and sys.stderr.isatty():
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)


# --------------------------------------------------------------- single instance


_MUTEX_HANDLE = None


def acquire_single_instance() -> bool:
    """True when this process got the lock; False when one is already running."""
    global _MUTEX_HANDLE
    if sys.platform != "win32":
        return True
    import ctypes
    from ctypes import wintypes

    ERROR_ALREADY_EXISTS = 183
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    handle = kernel32.CreateMutexW(None, False, f"Global\\{APP_NAME}-singleton")
    if not handle:
        return True
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        return False
    _MUTEX_HANDLE = handle
    return True
