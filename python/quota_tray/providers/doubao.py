"""Doubao (\u8c46\u5305) desktop client.

Doubao's desktop app is an Electron client for www.doubao.com; it keeps a
`sessionid` cookie in its Electron cookie store, encrypted the same way as
Claude Desktop's (OSCrypt key wrapped with DPAPI). With that cookie the
account profile endpoint reports who is signed in and, when the account
exposes it, the membership.

Doubao's subscription "overview" endpoint reports the plan and the
"window limit" usage the \u914d\u989d\u4e2d\u5fc3 (quota centre) page shows: a used-percent for
a short rolling window and a weekly one, the same shape as Claude's windows.

  1. read the sessionid cookie from the Doubao Desktop cookie store
     (or a session_id pasted into config.json)
  2. GET  www.doubao.com/alice/profile/self                       -> the account
  3. POST www.doubao.com/alice/commerce/sale/subscription/overview -> the plan and
     window-limit usage (best effort: if Doubao's web signing rejects the
     call, the card still shows the account and plan from step 2)
"""
from __future__ import annotations

import logging
import sys

from ..model import InfoRow, ProviderResult, QuotaWindow, SourceAttempt, now_utc, parse_time
from .base import Provider, session

log = logging.getLogger(__name__)

PROFILE_URL = "https://www.doubao.com/alice/profile/self"
OVERVIEW_URL = "https://www.doubao.com/alice/commerce/sale/subscription/overview/"
# The stable query params Doubao's web client sends; the per-request signing
# params (msToken, a_bogus, device_id) are left off and usually not required.
OVERVIEW_PARAMS = {
    "version_code": "20800", "language": "zh", "device_platform": "web",
    "aid": "497858", "real_aid": "497858", "region": "CN", "sys_region": "CN",
    "samantha_web": "1", "web_platform": "browser", "use-olympus-account": "1",
}
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


def _window_label(start_ms, end_ms, window_type) -> str:
    """Name a window by its length: 5-hour, Daily, Weekly."""
    try:
        hours = (float(end_ms) - float(start_ms)) / 3_600_000.0
    except (TypeError, ValueError):
        hours = 0.0
    if hours >= 100:
        return "Weekly"
    if hours >= 20:
        return "Daily"
    if hours >= 1:
        return f"{int(round(hours))}-hour"
    return {1: "5-hour", 2: "Weekly"}.get(window_type, "Window")


def parse_overview(payload: dict) -> tuple[str | None, list[QuotaWindow], list[InfoRow]]:
    """(plan, windows, rows) from the subscription overview response."""
    data = payload.get("data") if isinstance(payload, dict) else None
    data = data if isinstance(data, dict) else {}
    plan = None
    sub = data.get("current_subscription")
    if isinstance(sub, dict):
        disp = sub.get("display") if isinstance(sub.get("display"), dict) else {}
        for key in ("short_name", "product_name"):
            v = disp.get(key)
            if isinstance(v, str) and v.strip():
                plan = v.strip()
                break
    windows: list[QuotaWindow] = []
    section = data.get("window_limit_section")
    order = 10
    if isinstance(section, dict):
        exhausted_all = bool(section.get("usage_exhausted"))
        for group in section.get("window_limit_groups") or []:
            if not isinstance(group, dict):
                continue
            gname = str(group.get("feature_group") or "")
            for wl in group.get("window_limits") or []:
                if not isinstance(wl, dict):
                    continue
                pct = wl.get("used_percent")
                if not isinstance(pct, (int, float)) or isinstance(pct, bool):
                    continue
                pct = max(0.0, min(100.0, float(pct)))
                wtype = wl.get("window_type")
                detail = "<1% used" if wl.get("less_than_one_percent") and pct <= 0 else None
                windows.append(QuotaWindow(
                    key=f"doubao-{gname}-{wtype}-{order}",
                    label=_window_label(wl.get("start_time"), wl.get("end_time"), wtype),
                    percent=pct, resets_at=parse_time(wl.get("end_time")),
                    detail=detail, order=order, exhausted=exhausted_all and pct >= 100,
                ))
                order += 1
    return plan, windows, []


class DoubaoProvider(Provider):
    id = "doubao"
    name = "Doubao"

    def _extra_roots(self) -> list[str]:
        """Extra cookie-store roots: a portable install (data_dir), and the
        Chromium browsers' user-data dirs when scan_browsers is on."""
        roots: list[str] = []
        text = (self.settings.get("data_dir") or "").strip()
        if text:
            roots.append(text)
        if self.settings.get("scan_browsers"):
            from ..win.chromium_cookies import browser_roots

            roots += [str(p) for p in browser_roots()]
        return roots

    def detect(self) -> bool:
        if (self.settings.get("session_id") or "").strip():
            return True
        from pathlib import Path

        from ..win.chromium_cookies import app_roots

        for raw in self._extra_roots():
            try:
                if Path(raw).is_dir():
                    return True
            except OSError:
                continue
        return any(app_roots(folder) for folder in APP_FOLDERS)

    def _session_key(self) -> tuple[str | None, list[str]]:
        manual = (self.settings.get("session_id") or "").strip()
        if manual:
            return manual, ["using session_id from config.json"]
        if sys.platform != "win32":
            return None, ["reading the Doubao cookie is Windows-only"]
        from ..win.chromium_cookies import get_cookies

        extra = self._extra_roots()
        all_notes: list[str] = []
        for i, folder in enumerate(APP_FOLDERS):
            # The configured portable folder only needs searching once.
            jar, notes = get_cookies(folder, "%doubao.com", "sessionid", extra if i == 0 else None)
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
        # The subscription overview adds the plan name and the window-limit
        # usage; it is best effort (Doubao's web signing may reject our call).
        ov_plan, windows = self._overview(session_key, result)
        plan = ov_plan or plan
        if not account and not plan and not windows:
            result.attempts.append(SourceAttempt("Doubao profile", False, f"no account in response: {str(payload)[:160]}"))
            result.status = "signed in, but Doubao returned no account"
            return
        result.ok = True
        result.account = account
        result.plan = plan
        result.info = rows
        result.windows = windows
        result.headline = plan or "signed in"
        result.source = "www.doubao.com/alice/commerce/sale/subscription/overview"
        if windows:
            result.status = "connected"
        else:
            result.status = ("connected (open Doubao's \u914d\u989d\u4e2d\u5fc3 for usage)")
        result.data_time = now_utc()
        result.attempts.append(SourceAttempt("Doubao profile", True, account or plan or "signed in"))

    def _overview(self, session_key: str, result: ProviderResult) -> tuple[str | None, list[QuotaWindow]]:
        """Plan and window-limit usage, best effort. Never raises."""
        headers = {
            "Cookie": f"sessionid={session_key}",
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "agw-js-conv": "str",
            "Referer": "https://www.doubao.com/chat/",
            "User-Agent": BROWSER_UA,
        }
        try:
            resp = session().post(OVERVIEW_URL, params=OVERVIEW_PARAMS, headers=headers,
                                  json={"product_line": "membership"}, timeout=20)
        except Exception as exc:                                # noqa: BLE001
            result.attempts.append(SourceAttempt("Doubao overview", False, f"request failed: {exc}"))
            return None, []
        if resp.status_code >= 400:
            result.attempts.append(SourceAttempt("Doubao overview", False, f"HTTP {resp.status_code}"))
            return None, []
        try:
            payload = resp.json()
        except ValueError:
            result.attempts.append(SourceAttempt("Doubao overview", False, "response was not JSON"))
            return None, []
        if not isinstance(payload, dict) or payload.get("code") not in (0, None):
            result.attempts.append(SourceAttempt("Doubao overview", False,
                f"code {payload.get('code')}: {str(payload.get('msg') or payload.get('message'))[:120]}"
                if isinstance(payload, dict) else "unexpected response"))
            return None, []
        plan, windows, _ = parse_overview(payload)
        result.attempts.append(SourceAttempt("Doubao overview", True,
            f"{len(windows)} window(s)" + (f", {plan}" if plan else "")))
        return plan, windows
