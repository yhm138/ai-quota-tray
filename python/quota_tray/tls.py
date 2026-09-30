"""Keep HTTPS working when Windows cleans the temp folder under us.

The one-file build unpacks its support files into %TEMP%\\_MEIxxxxxx at start.
Storage Sense / Disk Cleanup delete files there that are not held open, and
certifi's cacert.pem is only opened per request, so after a few days every
HTTPS call failed with "Could not find a suitable TLS CA certificate bundle".
"""
from __future__ import annotations

import logging
import os
import shutil
import sys
from pathlib import Path

log = logging.getLogger(__name__)

_CA: str | None = None


def ca_bundle() -> str | None:
    """A CA bundle path that stays valid: certifi's file copied into the app
    data folder. A bundle the user configured through the environment wins."""
    global _CA
    for var in ("REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "SSL_CERT_FILE"):
        custom = os.environ.get(var)
        if custom and Path(custom).is_file() and not _is_ours(custom):
            return custom
    if _CA and Path(_CA).is_file():
        return _CA
    from .config import app_dir

    target = app_dir() / "cacert.pem"
    try:
        import certifi

        src = Path(certifi.where())
        if src.is_file() and (not target.is_file() or target.stat().st_size != src.stat().st_size):
            shutil.copyfile(src, target)
    except Exception:                                           # noqa: BLE001
        log.warning("could not refresh the stable CA bundle copy", exc_info=True)
    if target.is_file():
        _CA = str(target)
        return _CA
    return None


def _is_ours(path: str) -> bool:
    return "_MEI" in path or path == _CA


def bundle_damaged() -> bool:
    """True when the one-file build's unpacked folder lost files."""
    base = getattr(sys, "_MEIPASS", None)
    if not getattr(sys, "frozen", False) or not base:
        return False
    root = Path(base)
    return not root.is_dir() or not (root / "certifi" / "cacert.pem").is_file()
