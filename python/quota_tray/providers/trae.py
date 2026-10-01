"""TRAE (ByteDance IDE) quota collection.

TRAE keeps its login (a Cloud-IDE JWT) in the VS Code global storage:
%APPDATA%\\Trae CN\\User\\globalStorage\\storage.json, under keys such as
"iCubeAuthInfo://icube.cloudide". Newer builds store the value as base64 of a
"byte crypto" blob (AES-128-CBC with a key derived from a random prefix); older
builds keep plain JSON. With the token, the pay endpoints report the plan and
the fast-request usage, the same numbers the IDE's usage panel shows.

  1. read + decrypt storage.json (Trae CN and Trae, Windows and each WSL user)
  2. POST /trae/api/v1/pay/ide_user_pay_status  -> the plan
     POST /trae/api/v1/pay/ide_user_ent_usage   -> the entitlement packs (usage)

No sign-in: the token already on disk is only sent to TRAE's own host.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
from pathlib import Path

from ..model import InfoRow, ProviderResult, QuotaWindow, SourceAttempt, now_utc, parse_time
from .account import fmt_date, group_pages
from .base import Provider, session

log = logging.getLogger(__name__)

PAY_STATUS_PATH = "/trae/api/v1/pay/ide_user_pay_status"
ENT_USAGE_PATH = "/trae/api/v1/pay/ide_user_ent_usage"
CN_HOSTS = ("https://api.trae.cn", "https://api.trae.com.cn")
GLOBAL_HOSTS = ("https://grow-normal.trae.ai", "https://growsg-normal.trae.ai")

AUTH_PREFIX = "iCubeAuthInfo://"
SERVER_PREFIX = "iCubeServerData://"
DEFAULT_PROVIDER = "icube.cloudide"
USERTAG_KEY = "iCubeAuthInfo://usertag"
DEVICE_PREFIX = "iCubeAuthInfo://icube-dc:"

PRODUCT_TYPES = {6: "Ultra", 4: "Pro+", 1: "Pro", 9: "Pro", 8: "Lite", 0: "Free"}

# TRAE CN (Windows folder name) and international TRAE.
APPS = (("Trae CN", True), ("Trae", False))


# ------------------------------------------------------------ byte crypto

_HEADER = 6
_KEYLEN = 32
_PREFIX_AES = bytes([116, 99, 5, 16, 0, 0])
_PREFIX_AES_PRIVATE = bytes([18, 57, 32, 32, 2, 3])
_AES_A = bytes([82, 9, 106, 213, 48, 54, 165, 56, 191, 64, 163, 158, 129, 243, 215, 251, 124, 227,
                57, 130, 155, 47, 255, 135, 52, 142, 67, 68, 196, 222, 233, 203, 84, 123, 148, 50,
                166, 194, 35, 61, 238, 76, 149, 11, 66, 250, 195, 78, 8, 46, 161, 102, 40, 217, 36,
                178, 118, 91, 162, 73, 109, 139, 209, 37])
_AES_B = bytes([31, 221, 168, 51, 136, 7, 199, 49, 177, 18, 16, 89, 39, 128, 236, 95, 96, 81, 127,
                169, 25, 181, 74, 13, 45, 229, 122, 159, 147, 201, 156, 239, 160, 224, 59, 77, 174,
                42, 245, 176, 200, 235, 187, 60, 131, 83, 153, 97, 23, 43, 4, 126, 186, 119, 214,
                38, 225, 105, 20, 99, 85, 33, 12, 125])
_AES_PRIVATE_A = bytes([191, 192, 216, 250, 122, 246, 220, 97, 31, 254, 98, 27, 8, 72, 71, 176, 135,
                        99, 96, 18, 127, 101, 203, 104, 211, 102, 191, 125, 37, 72, 150, 156, 51,
                        229, 121, 35, 17, 153, 141, 177, 110, 131, 150, 128, 172, 255, 254, 6, 18,
                        140, 55, 62, 236, 249, 135, 64, 135, 12, 117, 4, 89, 149, 168, 209])
_AES_PRIVATE_B = bytes([246, 204, 26, 232, 232, 70, 129, 109, 223, 146, 169, 242, 23, 241, 105, 145,
                        50, 196, 165, 42, 254, 120, 3, 54, 244, 207, 209, 85, 53, 6, 138, 106, 175,
                        148, 31, 204, 186, 186, 165, 182, 87, 142, 49, 10, 39, 110, 26, 154, 86, 56,
                        173, 125, 18, 64, 198, 225, 99, 99, 83, 82, 191, 134, 76, 170])


def _salt(private: bool) -> bytes:
    a, b = (_AES_PRIVATE_A, _AES_PRIVATE_B) if private else (_AES_A, _AES_B)
    return bytes(x ^ y for x, y in zip(a, b))


def byte_crypto_decrypt(raw: bytes) -> bytes | None:
    """Undo TRAE's "byte crypto": header || 32-byte key || AES-128-CBC(sha512||plain)."""
    if len(raw) <= _HEADER + _KEYLEN:
        return None
    header = raw[:_HEADER]
    if header == _PREFIX_AES:
        private = False
    elif header == _PREFIX_AES_PRIVATE:
        private = True
    else:
        return None
    key_material = raw[_HEADER:_HEADER + _KEYLEN]
    ciphertext = raw[_HEADER + _KEYLEN:]
    if not ciphertext or len(ciphertext) % 16 != 0:
        return None
    merged = hashlib.sha512(hashlib.sha512(key_material).digest() + _salt(private)).digest()
    aes_key, iv = merged[:16], merged[16:32]
    from ..win.aescbc import decrypt as cbc_decrypt

    try:
        padded = cbc_decrypt(aes_key, iv, ciphertext)
    except Exception:                                           # noqa: BLE001
        return None
    if not padded:
        return None
    pad = padded[-1]
    if pad < 1 or pad > 16 or pad > len(padded):
        return None
    plain = padded[:-pad]
    if len(plain) < 64 or hashlib.sha512(plain[64:]).digest() != plain[:64]:
        return None
    return plain[64:]


# ------------------------------------------------------------ storage.json


def _decode_value(value) -> dict | None:
    """A storage value: a dict, a JSON string, or base64 of a byte-crypto blob."""
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except ValueError:
        pass
    try:
        raw = base64.b64decode(text)
    except (ValueError, Exception):                             # noqa: BLE001
        return None
    plain = byte_crypto_decrypt(raw)
    if plain is None:
        return None
    try:
        parsed = json.loads(plain.decode("utf-8", "replace"))
        return parsed if isinstance(parsed, dict) else None
    except ValueError:
        return None


def _user_auth_key(root: dict) -> str | None:
    if isinstance(root.get(AUTH_PREFIX + DEFAULT_PROVIDER), (str, dict)):
        return AUTH_PREFIX + DEFAULT_PROVIDER
    for key in root:
        if key.startswith(AUTH_PREFIX) and key != USERTAG_KEY and not key.startswith(DEVICE_PREFIX):
            return key
    return None


def _pick(node, *paths):
    for path in paths:
        cur = node
        for step in path:
            cur = cur.get(step) if isinstance(cur, dict) else None
            if cur is None:
                break
        if isinstance(cur, str) and cur:
            return cur
    return None


def read_storage_auth(storage_path: Path) -> tuple[str | None, str | None]:
    """(access token, email) from a storage.json, or (None, None)."""
    from ..win.shareio import read_text as _read_shared

    text = _read_shared(storage_path)
    if text is None:
        return None, None
    try:
        root = json.loads(text)
    except ValueError:
        return None, None
    if not isinstance(root, dict):
        return None, None
    auth_key = _user_auth_key(root)
    auth = _decode_value(root.get(auth_key)) if auth_key else None
    auth = auth or _decode_value(root.get(AUTH_PREFIX + DEFAULT_PROVIDER))
    if not isinstance(auth, dict):
        return None, None
    token = _pick(auth, ["accessToken"], ["access_token"], ["token"],
                  ["data", "accessToken"], ["data", "access_token"], ["auth", "accessToken"])
    if not token:
        return None, None
    email = _pick(auth, ["email"], ["account", "email"], ["data", "email"],
                  ["user", "email"], ["userInfo", "email"])
    return token, email


# ------------------------------------------------------------ discovery


def _storage_paths(settings: dict) -> list[tuple[Path, bool, str]]:
    """[(storage.json, is_cn, label)] for Trae CN and Trae, Windows and WSL."""
    out: list[tuple[Path, bool, str]] = []
    seen: set[str] = set()

    def add(base: Path, is_cn: bool, label: str) -> None:
        path = base / "User" / "globalStorage" / "storage.json"
        key = os.path.normcase(str(path))
        if key not in seen:
            seen.add(key)
            out.append((path, is_cn, label))

    manual = (settings.get("storage_path") or "").strip()
    if manual:
        out.append((Path(manual), bool(settings.get("cn", True)), "TRAE"))

    appdata = os.environ.get("APPDATA")
    for folder, is_cn in APPS:
        label = "TRAE CN" if is_cn else "TRAE"
        if appdata:
            add(Path(appdata) / folder, is_cn, label)
    if settings.get("scan_wsl", True):
        from .claude import _wsl_distros

        for distro in _wsl_distros():
            root = Path(f"\\\\wsl.localhost\\{distro}")
            homes = [root / "root"]
            try:
                if (root / "home").is_dir():
                    homes += list((root / "home").iterdir())[:10]
            except OSError:
                pass
            for home in homes:
                for folder, is_cn in APPS:
                    label = ("TRAE CN" if is_cn else "TRAE") + f" - WSL {distro}"
                    add(home / ".config" / folder, is_cn, label)
    return out


# ------------------------------------------------------------ usage parsing


def _num(node, *keys):
    if not isinstance(node, dict):
        return None
    for key in keys:
        v = node.get(key)
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, str):
            try:
                return float(v)
            except ValueError:
                continue
    return None


def _find_pack_list(obj):
    """The user_entitlement_pack_list anywhere in the response."""
    if isinstance(obj, dict):
        packs = obj.get("user_entitlement_pack_list")
        if isinstance(packs, list):
            return [p for p in packs if isinstance(p, dict)]
        for value in obj.values():
            found = _find_pack_list(value)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = _find_pack_list(value)
            if found is not None:
                return found
    return None


def _pack_product_type(pack: dict) -> int | None:
    v = _num(pack.get("entitlement_base_info"), "product_type") or _num(pack, "product_type")
    return int(v) if v is not None else None


def _pack_usage(pack: dict) -> dict:
    u = pack.get("usage")
    return u if isinstance(u, dict) else {}


def _pack_quota(pack: dict) -> dict:
    base = pack.get("entitlement_base_info") if isinstance(pack.get("entitlement_base_info"), dict) else {}
    for node in (base.get("quota"),
                 ((base.get("product_extra") or {}).get("subscription_extra") or {}).get("quota"),
                 ((base.get("product_extra") or {}).get("package_extra") or {}).get("quota")):
        if isinstance(node, dict):
            return node
    return {}


def _visible(pack: dict) -> bool:
    if pack.get("is_hide") is True:
        return False
    status = _num(pack, "status", "entitlement_status")
    return status is None or status == 1


def parse_usage(response: dict) -> tuple[list[QuotaWindow], list[InfoRow], str | None]:
    """(windows, rows, plan) from ide_user_ent_usage."""
    packs = [p for p in (_find_pack_list(response) or []) if _visible(p)]
    windows: list[QuotaWindow] = []
    rows: list[InfoRow] = []
    plan = None
    if not packs:
        return windows, rows, plan

    # Plan: the richest pack we recognise.
    for want in (6, 4, 1, 9, 8, 0):
        pack = next((p for p in packs if _pack_product_type(p) == want), None)
        if pack:
            plan = PRODUCT_TYPES.get(want)
            break

    fast_used = sum(_num(_pack_usage(p), "premium_model_fast_amount") or 0.0 for p in packs)
    fast_limits = [lim for p in packs if (lim := _num(_pack_quota(p), "premium_model_fast_request_limit")) is not None]
    if fast_limits:
        if any(lim == -1 for lim in fast_limits):
            windows.append(QuotaWindow("fast_request", "Fast requests", 0.0,
                                       detail="unlimited", order=10))
        else:
            limit = sum(fast_limits)
            pct = min(100.0, fast_used / limit * 100.0) if limit > 0 else None
            windows.append(QuotaWindow("fast_request", "Fast requests", pct,
                                       detail=f"{fast_used:,.0f} / {limit:,.0f}", order=10,
                                       exhausted=limit > 0 and fast_used >= limit))

    # Basic / bonus usage, when the plan measures those instead.
    main = next((p for p in packs if _pack_product_type(p) not in (3, None)), packs[0])
    basic_limit = _num(_pack_quota(main), "basic_usage_limit")
    basic_used = _num(_pack_usage(main), "basic_usage_amount")
    if basic_limit is not None and basic_limit >= 0 and basic_used is not None:
        pct = min(100.0, basic_used / basic_limit * 100.0) if basic_limit > 0 else None
        if not windows:
            windows.append(QuotaWindow("basic_usage", "Usage", pct,
                                       detail=f"{basic_used:,.0f} / {basic_limit:,.0f}", order=15,
                                       exhausted=basic_limit > 0 and basic_used >= basic_limit))
        else:
            rows.append(InfoRow("Basic usage", f"{basic_used:,.0f} / {basic_limit:,.0f}"))
    bonus_limit = _num(_pack_quota(main), "bonus_usage_limit")
    bonus_used = _num(_pack_usage(main), "bonus_usage_amount")
    if bonus_limit is not None and bonus_limit > 0 and bonus_used is not None:
        rows.append(InfoRow("Bonus usage", f"{bonus_used:,.0f} / {bonus_limit:,.0f}"))

    reset = parse_time(_num(main, "end_time") or _num(_pack_quota(main), "end_time"))
    reset = reset or parse_time(_num(main.get("entitlement_base_info"), "end_time"))
    if reset:
        rows.append(InfoRow("Renews", fmt_date(reset)))
    return windows, rows, plan


def parse_pay_status(response: dict) -> tuple[str | None, InfoRow | None]:
    """(plan, renew row) from ide_user_pay_status."""
    if not isinstance(response, dict):
        return None, None
    plan = _pick(response, ["user_pay_identity_str"])
    renew = parse_time(_num(response.get("detail"), "subscription_renew_time"))
    return plan, (InfoRow("Renews", fmt_date(renew)) if renew else None)


# ------------------------------------------------------------ provider


class TraeProvider(Provider):
    id = "trae"
    name = "TRAE"

    def detect(self) -> bool:
        for path, _cn, _label in _storage_paths(self.settings):
            try:
                if path.is_file():
                    return True
            except OSError:
                continue
        return False

    def _headers(self, token: str) -> dict:
        return {"Authorization": f"Cloud-IDE-JWT {token}", "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "Trae/1.0.0 (quota-tray)"}

    def _post(self, token: str, hosts, path: str, body: dict):
        problems = []
        for host in hosts:
            try:
                resp = session().post(host + path, headers=self._headers(token), json=body, timeout=20)
            except Exception as exc:                            # noqa: BLE001
                problems.append(f"{host}: {exc}")
                continue
            if resp.status_code >= 400:
                problems.append(f"{host}: HTTP {resp.status_code}")
                continue
            try:
                data = resp.json()
            except ValueError:
                problems.append(f"{host}: not JSON")
                continue
            if isinstance(data, dict) and data.get("code") not in (None, 0):
                problems.append(f"{host}: code {data.get('code')}")
                continue
            return data, None
        return None, "; ".join(problems)

    def _page(self, token: str, email, is_cn: bool, label: str, fetched_at) -> ProviderResult:
        page = ProviderResult(provider_id=self.id, name=self.name, fetched_at=fetched_at, label=label)
        tag = f"pay API ({label})"
        hosts = CN_HOSTS if is_cn else GLOBAL_HOSTS
        usage, usage_err = self._post(token, hosts, ENT_USAGE_PATH, {"require_usage": True})
        status, status_err = self._post(token, hosts, PAY_STATUS_PATH, {})
        if usage is None and status is None:
            detail = usage_err or status_err or "no response"
            if "HTTP 401" in detail or "HTTP 403" in detail:
                detail += " (open TRAE to refresh its login)"
            page.attempts.append(SourceAttempt(tag, False, detail))
            return page
        windows, rows, plan = parse_usage(usage or {})
        pay_plan, renew = parse_pay_status(status or {})
        page.windows = windows
        page.plan = pay_plan or plan
        page.info = rows + ([renew] if renew and not any(r.label == "Renews" for r in rows) else [])
        page.account = email
        page.ok = bool(windows or page.plan)
        page.source = (hosts[0].replace("https://", ""))
        page.status = "connected" if page.ok else "signed in, but no usage reported"
        page.data_time = now_utc()
        page.attempts.append(SourceAttempt(tag, page.ok, label))
        return page

    def collect(self, result: ProviderResult) -> None:
        pages: list[ProviderResult] = []
        found = 0
        for path, is_cn, label in _storage_paths(self.settings):
            try:
                if not path.is_file():
                    continue
            except OSError:
                continue
            token, email = read_storage_auth(path)
            if not token:
                result.attempts.append(SourceAttempt(f"login ({label})", False,
                                                     f"{path}: no access token (sign in to TRAE)"))
                continue
            found += 1
            page = self._page(token, email, is_cn, label, result.fetched_at)
            result.attempts.extend(page.attempts)
            if page.ok:
                pages.append(page)
        if pages:
            pages = group_pages(pages)
            result.adopt(pages[0])
            result.alternates = pages[1:]
            return
        if not result.installed:
            result.status = "TRAE not detected"
        elif found == 0:
            result.status = "no TRAE login found (sign in to TRAE)"
        else:
            failed = [a for a in result.attempts if not a.ok and a.name.startswith("pay API")]
            result.status = failed[-1].detail if failed else "could not read TRAE usage"
