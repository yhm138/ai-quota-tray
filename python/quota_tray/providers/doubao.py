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
  2. POST www.doubao.com/alice/profile/self -> optional account information
  3. POST www.doubao.com/alice/commerce/sale/subscription/overview -> plan and
     window-limit usage, independently of whether the profile call succeeded
"""
from __future__ import annotations

import logging
import sys

from ..model import InfoRow, ProviderResult, QuotaWindow, SourceAttempt, now_utc, parse_time
from .base import Provider, session

log = logging.getLogger(__name__)

PROFILE_URL = "https://www.doubao.com/alice/profile/self"
OVERVIEW_URL = "https://www.doubao.com/alice/commerce/sale/subscription/overview/"
# Stable query params from the web client. The overview was verified to work
# without msToken/a_bogus on 2026-10-01; the optional profile may still reject
# calls without the browser's per-request context.
OVERVIEW_PARAMS = {
    "version_code": "20800", "language": "zh", "device_platform": "web",
    "aid": "497858", "real_aid": "497858", "region": "CN", "sys_region": "CN",
    "samantha_web": "1", "web_platform": "browser", "use-olympus-account": "1",
}
APP_FOLDERS = ("Doubao", "doubao")
# Doubao answers a stale/invalid login with HTTP 200 and this code, not a 401.
LOGIN_INVALID_CODES = {710012001}


def _login_invalid(payload) -> bool:
    if not isinstance(payload, dict):
        return False
    code = payload.get("code")
    if code in (0, None):
        return False
    text = f"{payload.get('msg', '')} {payload.get('message', '')}".lower()
    return (isinstance(code, int) and code in LOGIN_INVALID_CODES) or "login invalid" in text or "\u767b\u5f55" in text
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


# Doubao labels windows by window_type, not by duration: 1 is the current
# rolling period (its length is not fixed, so it is not named in hours), 2 is
# the last-7-days window. Enums beyond these stay generic until confirmed.
_WINDOW_LABELS = {1: "Current period", 2: "Last 7 days"}


def _window_label(window_type) -> str:
    return _WINDOW_LABELS.get(window_type, "Usage window")


def _positive(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


def parse_overview(payload: dict) -> tuple[str | None, list[QuotaWindow], list[InfoRow]]:
    """(plan, windows, rows) from the subscription overview response."""
    data = payload.get("data") if isinstance(payload, dict) else None
    data = data if isinstance(data, dict) else {}
    plan = None
    rows: list[InfoRow] = []
    sub = data.get("current_subscription")
    sub = sub if isinstance(sub, dict) else {}
    member = data.get("member_info")
    active = member.get("hasActiveSubscription") if isinstance(member, dict) else None
    if isinstance(active, bool):
        status = "Active" if active else "Inactive"
        trial = sub.get("trial_info")
        if active and isinstance(trial, dict) and trial.get("is_trialing") is True:
            status += " (trial)"
        elif active and sub.get("is_gift") is True:
            status += " (gift)"
        rows.append(InfoRow("Plan status", status, "good" if active else "warn"))
    disp = sub.get("display") if isinstance(sub.get("display"), dict) else {}
    for key in ("short_name", "product_name"):
        v = disp.get(key)
        if isinstance(v, str) and v.strip():
            plan = v.strip()
            break
    # Subscription period and promotional entitlement have separate deadlines.
    # Neither implies auto-renewal, and one must not hide the other.
    campaign = data.get("campaign_benefit_info")
    benefit = campaign.get("benefit_end_time") if isinstance(campaign, dict) else None
    for label, timestamp in (("Plan until", sub.get("end_time")), ("Bonus until", benefit)):
        until = parse_time(timestamp) if _positive(timestamp) else None
        if until is not None:
            rows.append(InfoRow(label, until.astimezone().strftime("%Y-%m-%d %H:%M")))
    windows: list[QuotaWindow] = []
    named_groups: list[tuple[str, list[QuotaWindow]]] = []
    section = data.get("window_limit_section")
    order = 10
    if isinstance(section, dict):
        exhausted_all = bool(section.get("usage_exhausted"))
        for index, group in enumerate(section.get("window_limit_groups") or [], start=1):
            if not isinstance(group, dict):
                continue
            gname = str(group.get("feature_group") or "")
            display = group.get("feature_group_name")
            display = " ".join(display.split()) if isinstance(display, str) else ""
            display = display or ("General" if gname == "general" else f"Group {index}")
            group_windows: list[QuotaWindow] = []
            for wl in group.get("window_limits") or []:
                if not isinstance(wl, dict):
                    continue
                pct = wl.get("used_percent")
                if not isinstance(pct, (int, float)) or isinstance(pct, bool):
                    continue
                pct = max(0.0, min(100.0, float(pct)))
                wtype = wl.get("window_type")
                start, end = wl.get("start_time"), wl.get("end_time")
                # start_time=end_time=0 is the "not started" sentinel (the
                # period begins on first use), NOT an epoch of 1970.
                started = _positive(start) or _positive(end)
                if not started:
                    detail = "not started"
                elif wl.get("less_than_one_percent") and pct <= 0:
                    detail = "<1% used"           # a true <1%, not an exact zero
                else:
                    detail = None
                group_windows.append(QuotaWindow(
                    key=f"doubao-{gname}-{wtype}-{order}",
                    label=_window_label(wtype),
                    percent=pct, resets_at=parse_time(end) if started else None,
                    detail=detail, order=order, exhausted=exhausted_all and pct >= 100,
                ))
                order += 1
            if group_windows:
                named_groups.append((display, group_windows))
    names = [name for name, _ in named_groups]
    # Readable names can collide; distinguish the groups without exposing IDs.
    names = [f"{name} ({i})" if names.count(name) > 1 else name
             for i, name in enumerate(names, start=1)]
    for name, (_, group_windows) in zip(names, named_groups):
        for window in group_windows:
            if len(names) > 1:
                window.label = f"{name} \u00b7 {window.label}"
            windows.append(window)
    if names:
        rows.append(InfoRow("Quota group" if len(names) == 1 else "Quota groups", ", ".join(names)))
    return plan, windows, rows


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

    def _cookie_header(self) -> tuple[str | None, list[str]]:
        """The Cookie header to send. From config (session_id) if set -- a bare
        sessionid value or a whole "k=v; k=v" string -- else the full jar read
        from the app/browser cookie store. Preserve the complete jar when
        available; a lone sessionid also works for the subscription overview."""
        manual = (self.settings.get("session_id") or "").strip()
        if manual:
            header = manual if "=" in manual else f"sessionid={manual}"
            return header, ["using session_id from config.json"]
        if sys.platform != "win32":
            return None, ["reading the Doubao cookie is Windows-only"]
        from ..win.chromium_cookies import get_cookies

        extra = self._extra_roots()
        all_notes: list[str] = []
        for i, folder in enumerate(APP_FOLDERS):
            # The configured portable folder only needs searching once.
            jar, notes = get_cookies(folder, "%doubao.com", "sessionid", extra if i == 0 else None)
            all_notes += notes
            if jar.get("sessionid") or jar.get("session_id"):
                header = "; ".join(f"{k}={v}" for k, v in jar.items() if v)
                return header, all_notes
        return None, all_notes

    def collect(self, result: ProviderResult) -> None:
        cookie_header, notes = self._cookie_header()
        if not cookie_header:
            result.attempts.append(SourceAttempt("Doubao login", False,
                                                 "; ".join(notes[-3:]) or "no Doubao sessionid cookie found"))
            result.status = "Doubao not detected" if not result.installed else "sign in to Doubao Desktop first"
            return
        # The captured browser uses POST for profile (GET returns 404). Profile
        # can still reject this minimal request without its browser context;
        # that says nothing about whether the overview accepts the same cookie.
        payload, _ = self._request("profile", PROFILE_URL, {"avatar_format": "png"},
                                   cookie_header, result)
        account, plan, rows = parse_profile(payload) if payload is not None else (None, None, [])
        if payload is not None:
            result.attempts.append(SourceAttempt("Doubao profile", bool(account or plan),
                "account available" if account or plan else "no account in response"))
        overview, overview_failure = self._request("overview", OVERVIEW_URL,
            {"product_line": "membership"}, cookie_header, result)
        ov_plan, windows, ov_rows = parse_overview(overview) if overview is not None else (None, [], [])
        if overview is not None:
            result.attempts.append(SourceAttempt("Doubao overview", True,
                f"{len(windows)} window(s)" if windows else "no usage windows returned"))
        plan = ov_plan or plan
        rows = rows + ov_rows
        if not account and not plan and not windows and not ov_rows:
            result.status = overview_failure or "Doubao returned no account, plan or usage windows"
            return
        result.ok = True
        result.account = account
        result.plan = plan
        result.info = rows
        result.windows = windows
        result.headline = plan or "signed in"
        result.source = ("www.doubao.com/alice/commerce/sale/subscription/overview"
                         if ov_plan or windows or ov_rows else "www.doubao.com/alice/profile/self")
        if windows:
            result.status = "connected"
        elif overview_failure:
            result.status = "signed in; usage unavailable: " + overview_failure
        else:
            result.status = "connected (Doubao returned no usage windows)"
        result.data_time = now_utc()

    def _request(self, source: str, url: str, body: dict, cookie_header: str,
                 result: ProviderResult) -> tuple[dict | None, str | None]:
        """Independent request, with credential-free failure diagnostics.

        Response bodies, server messages and exception strings can echo secrets.
        Record only status codes and fixed descriptions, never their raw text.
        """
        headers = {
            "Cookie": cookie_header,
            "Accept": "application/json" if source == "profile" else "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "agw-js-conv": "str",
            "Referer": "https://www.doubao.com/chat/",
            "User-Agent": BROWSER_UA,
        }
        try:
            resp = session().post(url, params=OVERVIEW_PARAMS, headers=headers,
                                  json=body, timeout=20, allow_redirects=False)
        except Exception:                                      # noqa: BLE001
            failure = f"could not reach Doubao {source}"
            detail = "request failed (network error)"
        else:
            try:
                payload = resp.json()
            except ValueError:
                payload = None
            code = payload.get("code") if isinstance(payload, dict) else None
            detail = f"HTTP {resp.status_code}"
            if isinstance(code, int) and not isinstance(code, bool):
                detail += f", code {code}"
            if resp.status_code in (401, 403) or _login_invalid(payload):
                failure = "Doubao login expired; re-sign in, or paste a fresh session_id"
                detail += ": login rejected (expired or incomplete cookie)"
            elif resp.status_code >= 300:
                failure = f"Doubao {source} HTTP {resp.status_code}"
            elif not isinstance(payload, dict):
                failure = f"Doubao {source} returned non-JSON"
                detail += ": response was not JSON"
            elif payload.get("code") not in (0, None):
                failure = (f"Doubao {source} API code {code}"
                           if isinstance(code, int) and not isinstance(code, bool)
                           else f"Doubao {source} returned an invalid API code")
                detail += ": API error"
            else:
                return payload, None
        result.attempts.append(SourceAttempt(f"Doubao {source}", False, detail))
        return None, failure
