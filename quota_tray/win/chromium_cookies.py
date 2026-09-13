"""Pull a cookie for a given host out of an Electron/Chromium cookie store.

Claude Desktop is an Electron app: its cookies live in
%APPDATA%\\Claude\\Network\\Cookies, values are AES-256-GCM encrypted, and the
key sits in %APPDATA%\\Claude\\Local State wrapped with DPAPI.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)


class CookieError(RuntimeError):
    pass


# ------------------------------------------------------------------ discovery


def app_roots(app_folder: str) -> list[Path]:
    roots: list[Path] = []
    for env in ("APPDATA", "LOCALAPPDATA"):
        base = os.environ.get(env)
        if base:
            roots.append(Path(base) / app_folder)
    if sys.platform == "darwin":
        roots.append(Path.home() / "Library" / "Application Support" / app_folder)
    roots.append(Path.home() / f".config/{app_folder}")
    return [r for r in roots if r.is_dir()]


def find_cookie_dbs(root: Path, limit: int = 8) -> list[Path]:
    """Find every Cookies file under the app data dir (including partitions),
    newest first."""
    found: list[Path] = []
    direct = [root / "Network" / "Cookies", root / "Cookies"]
    for p in direct:
        if p.is_file():
            found.append(p)
    try:
        for p in root.rglob("Cookies"):
            if p.is_file() and p not in found:
                found.append(p)
                if len(found) >= limit:
                    break
    except OSError:
        pass
    found.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
    return found


def find_local_state(root: Path) -> Path | None:
    p = root / "Local State"
    if p.is_file():
        return p
    try:
        for cand in root.rglob("Local State"):
            if cand.is_file():
                return cand
    except OSError:
        pass
    return None


# ------------------------------------------------------------------ decryption


def master_key(local_state: Path) -> bytes:
    try:
        data = json.loads(local_state.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CookieError(f"cannot read Local State: {exc}") from exc

    encoded = (data.get("os_crypt") or {}).get("encrypted_key")
    if not encoded:
        raise CookieError(
            "Local State has no os_crypt.encrypted_key (the app may not encrypt cookies)"
        )

    blob = base64.b64decode(encoded)
    if blob.startswith(b"DPAPI"):
        blob = blob[5:]
    from .dpapi import unprotect

    return unprotect(blob)


def _aesgcm_decrypt(key: bytes, blob: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce, payload = blob[3:15], blob[15:]
    return AESGCM(key).decrypt(nonce, payload, None)


def decrypt_value(raw: bytes, key: bytes | None) -> str:
    """Decrypt one cookie's encrypted_value."""
    if not raw:
        return ""
    prefix = raw[:3]
    if prefix in (b"v10", b"v11"):
        if key is None:
            raise CookieError("no master key, cannot decrypt a v10/v11 cookie")
        plain = _aesgcm_decrypt(key, raw)
        # Chrome 130+ prepends a 32-byte domain hash to the plaintext.
        if len(plain) > 32 and not plain[:32].isascii():
            plain = plain[32:]
        return plain.decode("utf-8", "replace")
    if prefix == b"v20":
        raise CookieError(
            "this cookie uses App-Bound encryption (v20), which is not supported. "
            "Use Claude Code credentials instead, or set session_key in config.json."
        )
    # Legacy format: the whole blob is DPAPI-protected.
    from .dpapi import unprotect

    try:
        return unprotect(raw).decode("utf-8", "replace")
    except OSError as exc:
        raise CookieError(f"DPAPI decryption failed: {exc}") from exc


# ------------------------------------------------------------------ reading


def read_cookies(db_path: Path, host_like: str, key: bytes | None) -> dict[str, str]:
    """Return {cookie name: value}. The DB is copied first since it may be locked."""
    tmp_dir = tempfile.mkdtemp(prefix="quotatray-")
    tmp = Path(tmp_dir) / "Cookies"
    try:
        try:
            shutil.copy2(db_path, tmp)
            for suffix in ("-wal", "-shm"):
                side = db_path.with_name(db_path.name + suffix)
                if side.exists():
                    shutil.copy2(side, tmp.with_name(tmp.name + suffix))
        except OSError as exc:
            raise CookieError(f"could not copy the cookie DB: {exc}") from exc

        out: dict[str, str] = {}
        try:
            conn = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True, timeout=5)
        except sqlite3.Error as exc:
            raise CookieError(f"could not open the cookie DB: {exc}") from exc
        try:
            rows = conn.execute(
                "SELECT name, value, encrypted_value, expires_utc FROM cookies "
                "WHERE host_key LIKE ? ORDER BY expires_utc DESC",
                (host_like,),
            ).fetchall()
        except sqlite3.Error as exc:
            raise CookieError(f"cookie query failed: {exc}") from exc
        finally:
            conn.close()

        last_error: Exception | None = None
        for name, value, enc, _exp in rows:
            if name in out:
                continue
            if value:
                out[name] = value
                continue
            try:
                out[name] = decrypt_value(bytes(enc or b""), key)
            except Exception as exc:                            # noqa: BLE001
                last_error = exc
        if not out and last_error:
            raise CookieError(str(last_error))
        return out
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def get_cookie(app_folder: str, host_like: str, name: str) -> tuple[str | None, list[str]]:
    """Look for one cookie inside an Electron app. Returns (value, diagnostics)."""
    notes: list[str] = []
    roots = app_roots(app_folder)
    if not roots:
        notes.append(f"no data directory found for {app_folder}")
        return None, notes

    for root in roots:
        ls = find_local_state(root)
        key: bytes | None = None
        if ls:
            try:
                key = master_key(ls)
            except Exception as exc:                            # noqa: BLE001
                notes.append(f"{root.name}: master key failed ({exc})")
        else:
            notes.append(f"{root.name}: no Local State")

        dbs = find_cookie_dbs(root)
        if not dbs:
            notes.append(f"{root.name}: no Cookies DB found")
            continue
        for db in dbs:
            try:
                cookies = read_cookies(db, host_like, key)
            except Exception as exc:                            # noqa: BLE001
                notes.append(f"{db.parent.name}/Cookies: {exc}")
                continue
            if name in cookies and cookies[name]:
                notes.append(f"matched {db}")
                return cookies[name], notes
            notes.append(f"{db.parent.name}/Cookies: {len(cookies)} cookies but no {name}")
    return None, notes
