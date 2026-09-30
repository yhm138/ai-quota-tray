"""Doubao (\u8c46\u5305) desktop client.

Doubao's desktop app is an Electron client for www.doubao.com; it keeps a
`sessionid` cookie in its Electron cookie store, encrypted the same way as
Claude Desktop's (OSCrypt key wrapped with DPAPI). With that cookie the
account profile endpoint reports who is signed in and, when the account
exposes it, the membership.

Doubao does not publish a "remaining quota" API the way Codex or DeepSeek do
(the per-day points show only inside the app's own \u914d\u989d\u4e2d\u5fc3 page), so this card
shows the signed-in account and plan; any usage-looking field the profile
returns is shown too, but there is usually none.

  1. read the sessionid cookie from the Doubao Desktop cookie store
     (or a session_id pasted into config.json)
  2. GET www.doubao.com/alice/profile/self -> the account (and plan, if any)
"""
from __future__ import annotations

import logging
import sys

from ..model import InfoRow, ProviderResult, SourceAttempt, now_utc
from .base import Provider, session

log = logging.getLogger(__name__)

PROFILE_URL = "https://www.doubao.com/alice/profile/self"
APP_FOLDERS = ("Doubao", "doubao")
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Fields that name a membership / plan, and ones that look like a remaining count.
_PLAN_KEYS = ("vip_type", "member_type", "membership", "plan", "subscription", "user_level",
              "vip_level", "member_level")
_PLAN_LABELS = {"0": None, "1": "Member", "2": "Pro", "3": "Premium"}
_REMAIN_KEYS = ("remaining", "remain_count", "left_count", "available", "quota_remaining")


def _find(node, keys, depth=0):
    """First value whose key matches one of `keys`, searched breadth-ish."""
    if depth > 6 or not isinstance(node, dict):
        return None
    for key, value in node.items():
        if any(k == str(key).lower() for k in keys) and isinstance(value, (str, int, float, bool)):
            return value
    for value in node.values():
        if isinstance(value, dict):
            found = _find(value, keys, depth + 1)
            if found is not None:
                return found
        elif isinstance(value, list):
            for item in value:
                found = _find(item, keys, depth + 1)
                if found is not None:
                    return found
    return None


def parse_profile(payload: dict) -> tuple[str | None, str | None, list[InfoRow]]:
    """(account, plan, rows) from /alice/profile/self."""
    data = payload.get("data") if isinstance(payload, dict) else None
    profile = (data or {}).get("profile_brief") if isinstance(data, dict) else None
    profile = profile if isinstance(profile, dict) else (data if isinstance(data, dict) else payload)
    account = None
    for key in ("nickname", "nick_name", "user_name", "name"):
        v = profile.get(key) if isinstance(profile, dict) else None
        if isinstance(v, str) and v.strip():
            account = v.strip()
            break
    raw_plan = _find(payload, _PLAN_KEYS)
    plan = None
    if isinstance(raw_plan, bool):
        plan = "Member" if raw_plan else None
    elif isinstance(raw_plan, (int, float)):
        plan = _PLAN_LABELS.get(str(int(raw_plan)), f"Level {int(raw_plan)}" if raw_plan else None)
    elif isinstance(raw_plan, str) and raw_plan.strip():
        plan = raw_plan.strip().title()
    rows: list[InfoRow] = []
    remain = _find(payload, _REMAIN_KEYS)
    if isinstance(remain, (int, float)) and not isinstance(remain, bool):
        rows.append(InfoRow("Remaining", f"{remain:,.0f}"))
    return account, plan, rows


class DoubaoProvider(Provider):
    id = "doubao"
    name = "Doubao"

    def detect(self) -> bool:
        if (self.settings.get("session_id") or "").strip():
            return True
        from ..win.chromium_cookies import app_roots

        return any(app_roots(folder) for folder in APP_FOLDERS)

    def _session_key(self) -> tuple[str | None, list[str]]:
        manual = (self.settings.get("session_id") or "").strip()
        if manual:
            return manual, ["using session_id from config.json"]
        if sys.platform != "win32":
            return None, ["reading the Doubao cookie is Windows-only"]
        from ..win.chromium_cookies import get_cookies

        all_notes: list[str] = []
        for folder in APP_FOLDERS:
            jar, notes = get_cookies(folder, "%doubao.com", "sessionid")
            all_notes += notes
            key = jar.get("sessionid") or jar.get("session_id")
            if key:
                return key, all_notes
        return None, all_notes

    def collect(self, result: ProviderResult) -> None:
        session_key, notes = self._session_key()
        if not session_key:
            result.attempts.append(SourceAttempt("Doubao login", False,
                                                 "; ".join(notes[-3:]) or "no Doubao sessionid cookie found"))
            result.status = "Doubao not detected" if not result.installed else "sign in to Doubao Desktop first"
            return
        headers = {
            "Cookie": f"sessionid={session_key}",
            "Accept": "application/json",
            "Referer": "https://www.doubao.com/chat/",
            "User-Agent": BROWSER_UA,
        }
        try:
            resp = session().get(PROFILE_URL, headers=headers, timeout=20)
        except Exception as exc:                                # noqa: BLE001
            result.attempts.append(SourceAttempt("Doubao profile", False, f"request failed: {exc}"))
            result.status = "could not reach www.doubao.com"
            return
        if resp.status_code in (401, 403):
            result.attempts.append(SourceAttempt("Doubao profile", False,
                f"session cookie rejected (HTTP {resp.status_code}); open Doubao Desktop to refresh it"))
            result.status = "Doubao login expired (open the app)"
            return
        if resp.status_code >= 400:
            result.attempts.append(SourceAttempt("Doubao profile", False, f"HTTP {resp.status_code}: {resp.text[:160]}"))
            result.status = f"Doubao profile HTTP {resp.status_code}"
            return
        try:
            payload = resp.json()
        except ValueError:
            result.attempts.append(SourceAttempt("Doubao profile", False, "response was not JSON (a login page?)"))
            result.status = "Doubao returned no profile (open the app and sign in)"
            return
        account, plan, rows = parse_profile(payload if isinstance(payload, dict) else {})
        if not account and not plan:
            result.attempts.append(SourceAttempt("Doubao profile", False, f"no account in response: {str(payload)[:160]}"))
            result.status = "signed in, but Doubao returned no account"
            return
        result.ok = True
        result.account = account
        result.plan = plan
        result.info = rows
        # Doubao exposes no usage number through the API; say so plainly.
        result.headline = plan or "signed in"
        result.source = "www.doubao.com/alice/profile/self"
        result.status = ("connected (Doubao shows usage only in its own \u914d\u989d\u4e2d\u5fc3)"
                         if not rows else "connected")
        result.data_time = now_utc()
        result.attempts.append(SourceAttempt("Doubao profile", True, account or plan or "signed in"))
