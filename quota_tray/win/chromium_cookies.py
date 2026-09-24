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
    # Microsoft Store / MSIX installs get their AppData virtualised into
    # %LOCALAPPDATA%\Packages\<Package>_<hash>\LocalCache\{Roaming,Local}.
    local = os.environ.get("LOCALAPPDATA")
    if local:
        try:
            for pkg in sorted(Path(local, "Packages").glob(f"*{app_folder}*")):
                for sub in ("Roaming", "Local"):
                    roots.append(pkg / "LocalCache" / sub / app_folder)
        except OSError:
            pass
    if sys.platform == "darwin":
        roots.append(Path.home() / "Library" / "Application Support" / app_folder)
    roots.append(Path.home() / f".config/{app_folder}")
    return [r for r in roots if r.is_dir()]


def find_cookie_dbs(root: Path, limit: int = 8) -> list[Path]:
    """Find every Cookies file under the app data dir (including partitions),
    newest first."""
    found: list[Path] = []
    candidates = [root / "Network" / "Cookies", root / "Cookies"]
    # Session partitions (webviews) keep their own cookie jars. A full rglob
    # over the app dir is avoided: Claude Desktop keeps large VM bundles there.
    for pattern in ("Partitions/*/Network/Cookies", "Partitions/*/Cookies"):
        try:
            candidates.extend(root.glob(pattern))
        except OSError:
            pass
    for p in candidates:
        try:
            if p.is_file() and p not in found:
                found.append(p)
        except OSError:
            continue
    found = found[:limit]
    found.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
    return found


def find_local_state(root: Path) -> Path | None:
    p = root / "Local State"
    return p if p.is_file() else None


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


def decrypt_value(raw: bytes, key: bytes | None, *, has_domain_hash: bool | None = None) -> str:
    """Decrypt one cookie's encrypted_value.

    has_domain_hash: True when the DB schema (meta.version >= 24) says every
    plaintext starts with a 32-byte SHA-256 of the host; None means guess.
    """
    if not raw:
        return ""
    prefix = raw[:3]
    if prefix in (b"v10", b"v11"):
        if key is None:
            raise CookieError("no master key, cannot decrypt a v10/v11 cookie")
        plain = _aesgcm_decrypt(key, raw)
        if has_domain_hash is None:
            has_domain_hash = len(plain) > 32 and not _printable(plain[:32])
        if has_domain_hash and len(plain) >= 32:
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


def _printable(b: bytes) -> bool:
    return all(0x20 <= c < 0x7F for c in b)


# ------------------------------------------------------------------ reading


def _copy_shared(src: Path, dst: Path) -> None:
    """Copy a file the owning app holds open.

    Chromium/Electron keep the cookie DB open while running, and a plain
    open() then fails with a sharing violation. Asking for every share mode
    (read/write/delete) through CreateFileW usually succeeds anyway.
    """
    try:
        shutil.copyfile(src, dst)
        return
    except OSError as first:
        if sys.platform != "win32":
            raise
        err = first

    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    GENERIC_READ = 0x80000000
    SHARE_ALL = 0x1 | 0x2 | 0x4
    OPEN_EXISTING = 3
    handle = kernel32.CreateFileW(str(src), GENERIC_READ, SHARE_ALL, None, OPEN_EXISTING, 0, None)
    if handle in (None, wintypes.HANDLE(-1).value):
        raise OSError(f"{err} (shared open also failed, code={ctypes.get_last_error()})")
    fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    with os.fdopen(fd, "rb") as fin, open(dst, "wb") as fout:
        shutil.copyfileobj(fin, fout)


def read_cookies(db_path: Path, host_like: str, key: bytes | None) -> dict[str, str]:
    """Return {cookie name: value}. The DB is copied first since it may be locked."""
    tmp_dir = tempfile.mkdtemp(prefix="quotatray-")
    tmp = Path(tmp_dir) / "Cookies"
    try:
        try:
            _copy_shared(db_path, tmp)
        except OSError as exc:
            raise CookieError(
                f"could not copy the cookie DB ({exc}); quit Claude Desktop once and retry"
            ) from exc
        # Recent writes may still sit in the write-ahead log; best effort.
        for suffix in ("-wal", "-journal"):
            side = db_path.with_name(db_path.name + suffix)
            if side.exists():
                try:
                    _copy_shared(side, tmp.with_name(tmp.name + suffix))
                except OSError:
                    pass

        out: dict[str, str] = {}
        # A plain path, not a file: URI -- Windows backslashes break URIs, and
        # this is our private copy so a writable handle is harmless.
        try:
            conn = sqlite3.connect(str(tmp), timeout=5)
        except sqlite3.Error as exc:
            raise CookieError(f"could not open the cookie DB: {exc}") from exc
        try:
            has_hash: bool | None = None
            try:
                row = conn.execute("SELECT value FROM meta WHERE key = 'version'").fetchone()
                if row and str(row[0]).isdigit():
                    has_hash = int(row[0]) >= 24
            except sqlite3.Error:
                pass
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
                out[name] = decrypt_value(bytes(enc or b""), key, has_domain_hash=has_hash)
            except Exception as exc:                            # noqa: BLE001
                last_error = exc
        if not out and last_error:
            raise CookieError(str(last_error))
        return out
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def get_cookie(app_folder: str, host_like: str, name: str) -> tuple[str | None, list[str]]:
    """Look for one cookie inside an Electron app. Returns (value, diagnostics)."""
    cookies, notes = get_cookies(app_folder, host_like, name)
    return cookies.get(name), notes


def get_cookies(
    app_folder: str, host_like: str, required: str
) -> tuple[dict[str, str], list[str]]:
    """Return every cookie for host_like from the first jar that has `required`."""
    notes: list[str] = []
    roots = app_roots(app_folder)
    if not roots:
        notes.append(f"no data directory found for {app_folder}")
        return {}, notes

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
            if cookies.get(required):
                notes.append(f"matched {db}")
                return cookies, notes
            notes.append(f"{db.parent.name}/Cookies: {len(cookies)} cookies but no {required}")
    return {}, notes
