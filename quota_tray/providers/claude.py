"""Claude quota collection.

Fallback chain (reorder or trim it via providers.claude.order in config.json):
  1. oauth           -- the OAuth credentials Claude Code writes locally
                        -> api.anthropic.com/api/oauth/usage
                        Credentials are looked up in: env vars, CLAUDE_CONFIG_DIR,
                        ~/.claude, %APPDATA%\\Claude, and every WSL distro's ~/.claude
  2. desktop_cookie  -- decrypt the sessionKey cookie out of Claude Desktop's
                        Electron cookie store -> claude.ai usage endpoints
  3. manual_cookie   -- a session_key the user pasted into config.json
"""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

from ..model import (
    ProviderResult,
    QuotaWindow,
    SourceAttempt,
    apply_scale,
    decide_percent_scale,
    iter_quota_nodes,
    node_reset,
    node_used,
    now_utc,
)
from ..win.proc import run
from .base import Provider, session

log = logging.getLogger(__name__)

OAUTH_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
OAUTH_BETA = "oauth-2025-04-20"
CLAUDE_CODE_UA = "claude-code/2.0.0 (external, cli)"
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

WINDOW_LABELS = {
    "five_hour": ("5-hour window", 10),
    "fivehour": ("5-hour window", 10),
    "session": ("Session", 10),
    "seven_day": ("7-day window", 20),
    "sevenday": ("7-day window", 20),
    "weekly": ("7-day window", 20),
    "seven_day_opus": ("7-day Opus", 30),
    "seven_day_sonnet": ("7-day Sonnet", 31),
    "seven_day_haiku": ("7-day Haiku", 32),
    "seven_day_fable": ("7-day Fable", 33),
    "seven_day_cowork": ("7-day Cowork", 34),
    "cowork": ("Cowork", 34),
    "extra_usage": ("Extra usage", 40),
    "monthly": ("Monthly", 45),
}


def _label_for(key: str) -> tuple[str, int]:
    k = key.lower()
    if k in WINDOW_LABELS:
        return WINDOW_LABELS[k]
    pretty = key.replace("_", " ").strip().title()
    return pretty, 50


# ------------------------------------------------------------ credentials


def _wsl_distros() -> list[str]:
    if sys.platform != "win32":
        return []
    code, out, _ = run(["wsl.exe", "-l", "-q"], timeout=8, encoding="utf-16-le")
    if code != 0:
        return []
    names = []
    for line in out.replace("\x00", "").splitlines():
        name = line.strip().strip("\ufeff")
        if name and "\r" not in name:
            names.append(name)
    return names[:6]


def credential_candidates(settings: dict) -> list[Path]:
    """Every place a .credentials.json might live, most reliable first."""
    seen: list[Path] = []

    def add(p: Path | str | None) -> None:
        if not p:
            return
        path = Path(p)
        if path not in seen:
            seen.append(path)

    manual = (settings.get("credentials_path") or "").strip()
    if manual:
        add(manual)

    bases: list[Path] = []
    cfg_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if cfg_dir:
        bases.extend(Path(part) for part in cfg_dir.split(os.pathsep) if part.strip())
    bases.append(Path.home() / ".claude")
    for env in ("APPDATA", "LOCALAPPDATA", "USERPROFILE"):
        base = os.environ.get(env)
        if base:
            bases.append(Path(base) / "Claude")
            bases.append(Path(base) / ".claude")
    bases.append(Path.home() / ".config" / "claude")

    for base in bases:
        add(base / ".credentials.json")
        add(base / "credentials.json")

    if settings.get("scan_wsl", True):
        for distro in _wsl_distros():
            for prefix in (r"\\wsl.localhost", r"\\wsl$"):
                root = Path(f"{prefix}\\{distro}")
                add(root / "root" / ".claude" / ".credentials.json")
                try:
                    home = root / "home"
                    if home.is_dir():
                        for user in list(home.iterdir())[:10]:
                            add(user / ".claude" / ".credentials.json")
                except OSError:
                    continue
    return seen


def _token_from_file(path: Path) -> tuple[str | None, float | None, str | None]:
    """Return (access_token, expiresAt in ms, subscriptionType)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return None, None, None
    node = data.get("claudeAiOauth") or data.get("oauth") or data
    if not isinstance(node, dict):
        return None, None, None
    token = node.get("accessToken") or node.get("access_token")
    if not isinstance(token, str) or len(token) < 20:
        return None, None, None
    expires = node.get("expiresAt") or node.get("expires_at")
    plan = node.get("subscriptionType") or node.get("subscription_type")
    return token, (float(expires) if isinstance(expires, (int, float)) else None), plan


def discover_oauth_token(settings: dict) -> tuple[str | None, str, str | None, list[str]]:
    """Return (token, where it came from, plan, diagnostics)."""
    notes: list[str] = []

    manual = (settings.get("oauth_token") or "").strip()
    if manual:
        return manual, "token from config.json", None, notes

    env_token = (os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or "").strip()
    if env_token:
        return env_token, "env CLAUDE_CODE_OAUTH_TOKEN", None, notes

    checked = 0
    for path in credential_candidates(settings):
        checked += 1
        try:
            exists = path.is_file()
        except OSError:
            continue
        if not exists:
            continue
        token, expires, plan = _token_from_file(path)
        if not token:
            notes.append(f"{path}: file exists but no accessToken in it")
            continue
        if expires and expires / 1000.0 < now_utc().timestamp() - 60:
            notes.append(f"{path}: token expired (run Claude Code once to refresh it)")
            continue
        return token, str(path), plan, notes
    notes.append(f"checked {checked} candidate paths, no usable credentials")
    return None, "", None, notes


# ------------------------------------------------------------ parsing


def _build_windows(payload: dict, scale_mode: str, *, base_time=None) -> list[QuotaWindow]:
    nodes = list(iter_quota_nodes(payload))
    if not nodes:
        return []
    raw_values = [v for _, node in nodes if (v := node_used(node)) is not None]
    if scale_mode == "percent":
        scale = 1.0
    elif scale_mode == "fraction":
        scale = 100.0
    elif all(isinstance(node.get("utilization"), (int, float)) for _, node in nodes):
        # Both the OAuth and claude.ai endpoints report `utilization` as 0-100.
        # Guessing from magnitude would turn 1% (early in a window) into 100%.
        scale = 1.0
    else:
        scale = decide_percent_scale(raw_values)

    windows: list[QuotaWindow] = []
    seen: set[str] = set()
    for path, node in nodes:
        key = path[-1] if path else "usage"
        if key.isdigit() and len(path) >= 2:
            key = path[-2]
        if key in seen:
            key = "/".join(path[-2:]) if len(path) >= 2 else key
        seen.add(key)
        used = node_used(node)
        if used is None:
            continue
        label, order = _label_for(key)
        detail = None
        limit = node.get("monthly_limit") or node.get("limit")
        used_credits = node.get("used_credits") or node.get("used")
        if isinstance(limit, (int, float)) and isinstance(used_credits, (int, float)):
            detail = f"{used_credits:,.0f} / {limit:,.0f}"
        if node.get("is_enabled") is False:
            detail = (detail + " - off") if detail else "off"
        pct = apply_scale(used, scale)
        windows.append(
            QuotaWindow(
                key=key,
                label=label,
                percent=pct,
                resets_at=node_reset(node, base=base_time),
                detail=detail,
                order=order,
                exhausted=bool(pct is not None and pct >= 99.9),
            )
        )
    return windows


# ------------------------------------------------------------ provider


class ClaudeProvider(Provider):
    id = "claude"
    name = "Claude"

    def detect(self) -> bool:
        from ..win.chromium_cookies import app_roots

        if app_roots("Claude"):
            return True
        if (Path.home() / ".claude").is_dir():
            return True
        return bool(discover_oauth_token(self.settings)[0])

    # ---------------- individual sources

    def _try_oauth(self, result: ProviderResult) -> bool:
        token, origin, plan, notes = discover_oauth_token(self.settings)
        if not token:
            result.attempts.append(
                SourceAttempt(
                    "OAuth credentials",
                    False,
                    "; ".join(notes) or "no Claude Code credentials on this machine",
                )
            )
            return False
        try:
            resp = session().get(
                OAUTH_USAGE_URL,
                headers={
                    "Authorization": f"Bearer {token}",
                    "anthropic-beta": OAUTH_BETA,
                    "User-Agent": CLAUDE_CODE_UA,
                    "Accept": "application/json",
                },
                timeout=20,
            )
        except Exception as exc:                                # noqa: BLE001
            result.attempts.append(SourceAttempt("OAuth credentials", False, f"request failed: {exc}"))
            return False

        if resp.status_code == 401:
            result.attempts.append(
                SourceAttempt("OAuth credentials", False, "token rejected, sign in to Claude Code again")
            )
            return False
        if resp.status_code == 429:
            result.attempts.append(
                SourceAttempt("OAuth credentials", False, "rate limited (429), will retry on next refresh")
            )
            return False
        if resp.status_code >= 400:
            result.attempts.append(
                SourceAttempt("OAuth credentials", False, f"HTTP {resp.status_code}: {resp.text[:160]}")
            )
            return False
        try:
            payload = resp.json()
        except ValueError:
            result.attempts.append(SourceAttempt("OAuth credentials", False, "response was not JSON"))
            return False

        windows = _build_windows(payload, self.settings.get("percent_scale", "auto"))
        if not windows:
            result.attempts.append(
                SourceAttempt("OAuth credentials", False, f"no quota fields in response: {str(payload)[:160]}")
            )
            return False

        result.windows = windows
        result.ok = True
        result.source = f"Claude Code OAuth ({Path(origin).name if os.sep in origin else origin})"
        result.plan = plan or _guess_plan(payload)
        result.account = _guess_account(payload)
        result.status = "connected"
        result.attempts.append(SourceAttempt("OAuth credentials", True, origin))
        return True

    def _try_cookie(self, result: ProviderResult, session_key: str | None, tag: str) -> bool:
        notes: list[str] = []
        jar: dict[str, str] = {}
        if not session_key:
            if sys.platform != "win32":
                result.attempts.append(
                    SourceAttempt(tag, False, "reading desktop cookies is Windows-only")
                )
                return False
            from ..win.chromium_cookies import get_cookies

            jar, notes = get_cookies("Claude", "%claude.ai", "sessionKey")
            session_key = jar.get("sessionKey")
        if not session_key:
            result.attempts.append(
                SourceAttempt(tag, False, "; ".join(notes[-3:]) or "no sessionKey found")
            )
            return False

        # Forward the identity cookies the web client sends alongside the
        # session. Cloudflare's cf_* cookies are bound to Claude Desktop's own
        # user agent, so replaying them with ours would do more harm than good.
        cookie = {"sessionKey": session_key}
        for name in _FORWARD_COOKIES:
            if jar.get(name) and _cookie_safe(jar[name]):
                cookie[name] = jar[name]
        headers = {
            "Cookie": "; ".join(f"{k}={v}" for k, v in cookie.items()),
            "User-Agent": BROWSER_UA,
            "Accept": "application/json",
            "Referer": "https://claude.ai/",
            "anthropic-client-platform": "web_claude_ai",
        }
        org_id, account, plan = None, None, None
        org_error = ""
        try:
            orgs_resp = session().get(
                "https://claude.ai/api/organizations", headers=headers, timeout=20
            )
        except Exception as exc:                                # noqa: BLE001
            orgs_resp, org_error = None, f"claude.ai request failed: {exc}"
        if orgs_resp is not None:
            if orgs_resp.status_code >= 400:
                org_error = _http_problem(orgs_resp)
            else:
                try:
                    org_id, account, plan = _pick_org(
                        orgs_resp.json(), prefer=cookie.get("lastActiveOrg")
                    )
                except ValueError:
                    org_error = "org list was not JSON (likely a Cloudflare challenge)"
        # The org list can be blocked while the usage endpoint is not; the
        # desktop app remembers the org it last used in a cookie.
        org_id = org_id or cookie.get("lastActiveOrg")
        if not org_id:
            result.attempts.append(
                SourceAttempt(tag, False, org_error or "could not determine the organization id")
            )
            return False

        problems: list[str] = []
        for url in (
            f"https://claude.ai/api/organizations/{org_id}/usage",
            f"https://claude.ai/api/organizations/{org_id}/rate_limits",
            "https://claude.ai/api/bootstrap",
        ):
            name = url.rsplit("/", 1)[-1]
            try:
                resp = session().get(url, headers=headers, timeout=20)
            except Exception as exc:                            # noqa: BLE001
                problems.append(f"{name}: {exc}")
                continue
            if resp.status_code >= 400:
                problems.append(f"{name}: {_http_problem(resp)}")
                continue
            try:
                payload = resp.json()
            except ValueError:
                problems.append(f"{name}: not JSON")
                continue
            windows = _build_windows(payload, self.settings.get("percent_scale", "auto"))
            if windows:
                result.windows = windows
                result.ok = True
                result.source = f"Claude Desktop cookie ({url.rsplit('/', 1)[-1]})"
                result.account = account
                result.plan = plan or _guess_plan(payload)
                result.status = "connected"
                result.attempts.append(SourceAttempt(tag, True, url))
                return True
            problems.append(f"{name}: no quota fields")
        detail = "; ".join(problems[:2]) or "no quota fields in the claude.ai endpoints"
        if org_error:
            detail = f"{org_error}; {detail}"
        result.attempts.append(SourceAttempt(tag, False, detail))
        return False

    def collect(self, result: ProviderResult) -> None:
        order = self.settings.get("order") or ["oauth", "desktop_cookie", "manual_cookie"]
        for step in order:
            if step == "oauth" and self._try_oauth(result):
                return
            if step == "desktop_cookie" and self._try_cookie(result, None, "Claude Desktop cookie"):
                return
            if step == "manual_cookie":
                manual = (self.settings.get("session_key") or "").strip()
                if manual and self._try_cookie(result, manual, "manual sessionKey"):
                    return
        if not result.installed:
            result.status = "Claude not detected"
        else:
            result.status = "no usable quota source - see Diagnostics"


_FORWARD_COOKIES = ("lastActiveOrg", "anthropic-device-id", "activitySessionId", "routingHint")


def _cookie_safe(value: str) -> bool:
    return all(0x21 <= ord(c) < 0x7F and c not in ';,"\\' for c in value)


def _http_problem(resp) -> str:
    body = (getattr(resp, "text", "") or "")[:2000].lower()
    if resp.status_code in (403, 503) and ("cloudflare" in body or "just a moment" in body
                                           or "cf-chl" in body):
        return f"HTTP {resp.status_code}: blocked by Cloudflare (open Claude Desktop, then retry)"
    if resp.status_code in (401, 403):
        return f"HTTP {resp.status_code}: session cookie rejected (sign in to Claude Desktop again)"
    return f"HTTP {resp.status_code}"


def _pick_org(orgs, prefer: str | None = None) -> tuple[str | None, str | None, str | None]:
    if isinstance(orgs, dict):
        orgs = orgs.get("organizations") or [orgs]
    if not isinstance(orgs, list):
        return None, None, None
    preferred = chat = first = None
    for org in orgs:
        if not isinstance(org, dict):
            continue
        if prefer and prefer in (org.get("uuid"), org.get("id")):
            preferred = org
            break
        caps = org.get("capabilities") or []
        if chat is None and any("chat" in str(c) for c in caps):
            chat = org
        first = first or org
    best = preferred or chat or first
    if not best:
        return None, None, None
    plan = None
    for c in best.get("capabilities") or []:
        s = str(c)
        if s.startswith("claude_"):
            plan = s.replace("claude_", "")
    return best.get("uuid") or best.get("id"), best.get("name"), plan


def _guess_account(payload) -> str | None:
    if not isinstance(payload, dict):
        return None
    for key in ("account", "user", "profile"):
        node = payload.get(key)
        if isinstance(node, dict):
            for f in ("email_address", "email", "display_name", "full_name"):
                if isinstance(node.get(f), str):
                    return node[f]
    for f in ("email_address", "email"):
        if isinstance(payload.get(f), str):
            return payload[f]
    return None


def _guess_plan(payload) -> str | None:
    if not isinstance(payload, dict):
        return None
    for f in ("subscription_type", "subscriptionType", "plan", "tier", "rate_limit_tier"):
        v = payload.get(f)
        if isinstance(v, str) and v:
            return v
    return None
