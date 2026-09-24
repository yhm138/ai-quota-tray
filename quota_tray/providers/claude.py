"""Claude quota collection.

Fallback chain (reorder or trim it via providers.claude.order in config.json):
  1. oauth           -- the OAuth credentials Claude Code writes locally
                        -> api.anthropic.com/api/oauth/usage
                        Credentials are looked up in: env vars, CLAUDE_CONFIG_DIR,
                        ~/.claude, %APPDATA%\\Claude, and every WSL distro's ~/.claude
  2. desktop_oauth   -- the OAuth token Claude Desktop keeps for itself in
                        config.json (oauth:tokenCacheV2, encrypted with the
                        app's OSCrypt key) -> the same api.anthropic.com
                        endpoint. No cookie file, no claude.ai, no Cloudflare
  3. desktop_cookie  -- decrypt the sessionKey cookie out of Claude Desktop's
                        Electron cookie store -> claude.ai usage endpoints
  4. manual_cookie   -- a session_key the user pasted into config.json
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
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
from ..config import app_dir
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
        label = f"Claude Code OAuth ({Path(origin).name if os.sep in origin else origin})"
        return self._usage_with_token(result, token, "OAuth credentials", label, origin, plan)

    def _try_desktop_oauth(self, result: ProviderResult) -> bool:
        tag = "Claude Desktop login"
        tokens, notes = desktop_oauth_tokens()
        if not tokens:
            result.attempts.append(
                SourceAttempt(tag, False, "; ".join(notes[-3:]) or "no Claude Desktop login found")
            )
            return False
        for token, where in tokens[:2]:
            if self._usage_with_token(result, token, tag, "Claude Desktop login", where, None):
                return True
        return False

    def _usage_with_token(
        self, result: ProviderResult, token: str, tag: str, label: str, origin: str, plan
    ) -> bool:
        """Call the OAuth usage endpoint with a bearer token and fill result."""
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
            result.attempts.append(SourceAttempt(tag, False, f"request failed: {exc}"))
            return False

        if resp.status_code in (401, 403):
            hint = ("open Claude Desktop so it renews its login" if "Desktop" in tag
                    else "sign in to Claude Code again")
            result.attempts.append(
                SourceAttempt(tag, False, f"token rejected (HTTP {resp.status_code}), {hint}")
            )
            return False
        if resp.status_code == 429:
            result.attempts.append(
                SourceAttempt(tag, False, "rate limited (429), will retry on next refresh")
            )
            return False
        if resp.status_code >= 400:
            result.attempts.append(
                SourceAttempt(tag, False, f"HTTP {resp.status_code}: {resp.text[:160]}")
            )
            return False
        try:
            payload = resp.json()
        except ValueError:
            result.attempts.append(SourceAttempt(tag, False, "response was not JSON"))
            return False

        windows = _build_windows(payload, self.settings.get("percent_scale", "auto"))
        if not windows:
            result.attempts.append(
                SourceAttempt(tag, False, f"no quota fields in response: {str(payload)[:160]}")
            )
            return False

        result.windows = windows
        result.ok = True
        result.source = label
        result.plan = plan or _guess_plan(payload)
        result.account = _guess_account(payload)
        result.status = "connected"
        result.attempts.append(SourceAttempt(tag, True, origin))
        return True

    def _try_cookie(self, result: ProviderResult, session_key: str | None, tag: str) -> bool:
        notes: list[str] = []
        jar: dict[str, str] = {}
        from_desktop = not session_key
        cached = False
        if from_desktop:
            if sys.platform != "win32":
                result.attempts.append(
                    SourceAttempt(tag, False, "reading desktop cookies is Windows-only")
                )
                return False
            from ..win.chromium_cookies import get_cookies

            jar, notes = get_cookies("Claude", "%claude.ai", "sessionKey")
            if jar.get("sessionKey"):
                save_cached_session(jar)
            else:
                # Claude Desktop can hold its cookie file locked while it runs;
                # fall back to the session read the last time it was readable.
                jar = load_cached_session()
                cached = bool(jar.get("sessionKey"))
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
            "Accept": "application/json",
            "Referer": "https://claude.ai/",
            "anthropic-client-platform": "web_claude_ai",
        }
        org_id, account, plan = None, None, None
        org_error = ""
        try:
            orgs_resp = _web_get("https://claude.ai/api/organizations", headers)
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
                resp = _web_get(url, headers)
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
                result.source = f"Claude Desktop session ({url.rsplit('/', 1)[-1]})" + (
                    ", saved copy" if cached else ""
                )
                result.account = account
                result.plan = plan or _guess_plan(payload)
                result.status = "connected"
                result.attempts.append(SourceAttempt(tag, True, url))
                return True
            problems.append(f"{name}: no quota fields")
        if from_desktop and any("rejected" in p for p in [org_error, *problems]):
            forget_cached_session()
        detail = "; ".join(problems[:2]) or "no quota fields in the claude.ai endpoints"
        if cached:
            detail = f"(using the saved session, Claude Desktop's cookie file was unreadable) {detail}"
        if org_error:
            detail = f"{org_error}; {detail}"
        result.attempts.append(SourceAttempt(tag, False, detail))
        return False

    def collect(self, result: ProviderResult) -> None:
        order = list(self.settings.get("order") or DEFAULT_ORDER)
        # config.json stores the whole list, so installs from before
        # desktop_oauth existed would never try it. A list that still has
        # desktop_cookie is such a saved default: add it right before that.
        if "desktop_oauth" not in order and "desktop_cookie" in order:
            order.insert(order.index("desktop_cookie"), "desktop_oauth")
        for step in order:
            if step == "oauth" and self._try_oauth(result):
                return
            if step == "desktop_oauth" and self._try_desktop_oauth(result):
                return
            if step == "desktop_cookie" and self._try_cookie(result, None, "Claude Desktop cookie"):
                return
            if step == "manual_cookie":
                manual = (self.settings.get("session_key") or "").strip()
                if manual and self._try_cookie(result, manual, "manual sessionKey"):
                    return
        if not result.installed:
            result.status = "Claude not detected"
            return
        # Name the real problem in the panel, preferring the desktop session.
        failed = [a for a in result.attempts if not a.ok]
        best = next((a for a in failed if a.name == "Claude Desktop login"),
                    next((a for a in failed if "Desktop" in a.name),
                         failed[-1] if failed else None))
        result.status = f"{best.name}: {best.detail}"[:200] if best else "no usable quota source"


# ------------------------------------------------------------ Claude Desktop login

DEFAULT_ORDER = ["oauth", "desktop_oauth", "desktop_cookie", "manual_cookie"]
# Newest layout first: current builds keep the live grant in V2 and leave a
# stale grant under the old key.
DESKTOP_TOKEN_CACHE_KEYS = ("oauth:tokenCacheV2", "oauth:tokenCache")


def parse_desktop_token_cache(plaintext: str, now_ms: float) -> list[tuple[tuple, str]]:
    """[(rank, access token)] from a decrypted Claude Desktop token cache.

    The cache maps "<install>:<user>:<base url>:<scopes>" to
    {"token", "expiresAt", "refreshToken"}; usage needs user:inference and
    user:profile. Expired entries are dropped.
    """
    try:
        entries = json.loads(plaintext)
    except ValueError:
        return []
    if not isinstance(entries, dict):
        return []
    out = []
    for key, entry in entries.items():
        if not isinstance(entry, dict):
            continue
        token = entry.get("token") or entry.get("accessToken")
        if not isinstance(token, str) or not token.strip():
            continue
        expires = entry.get("expiresAt")
        if isinstance(expires, (int, float)) and expires < now_ms:
            continue
        scopes = set(re.findall(r"user:[a-z_]+", str(key)))
        rank = ("user:inference" in scopes and "user:profile" in scopes,
                "user:inference" in scopes,
                float(expires) if isinstance(expires, (int, float)) else 0.0)
        out.append((rank, token.strip()))
    return out


def desktop_oauth_tokens(roots: list[Path] | None = None) -> tuple[list[tuple[str, str]], list[str]]:
    """Every usable Claude Desktop OAuth token, best first: [(token, where)]."""
    from ..win import chromium_cookies as cc

    notes: list[str] = []
    if roots is None:
        roots = cc.app_roots("Claude")
    found: list[tuple[tuple, str, str]] = []
    now_ms = now_utc().timestamp() * 1000
    for root in roots:
        cfg = root / "config.json"
        try:
            data = json.loads(cfg.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            notes.append(f"{root.name}: no readable config.json")
            continue
        caches = [(k, data.get(k)) for k in DESKTOP_TOKEN_CACHE_KEYS
                  if isinstance(data, dict) and isinstance(data.get(k), str) and data.get(k)]
        if not caches:
            notes.append(f"{root.name}: config.json has no saved login (sign in to Claude Desktop)")
            continue
        local_state = cc.find_local_state(root)
        if local_state is None:
            notes.append(f"{root.name}: no Local State next to config.json")
            continue
        try:
            key = cc.master_key(local_state)
        except Exception as exc:                                # noqa: BLE001
            notes.append(f"{root.name}: could not unwrap the app key ({exc})")
            continue
        before = len(found)
        for name, value in caches:
            try:
                blob = base64.b64decode(value)
                if not blob.startswith(b"v10"):
                    raise ValueError("not a v10 value")
                plain = cc._aesgcm_decrypt(key, blob).decode("utf-8", "replace")
            except Exception as exc:                            # noqa: BLE001
                notes.append(f"{root.name}: could not decrypt {name} ({exc.__class__.__name__})")
                continue
            for rank, token in parse_desktop_token_cache(plain, now_ms):
                # V2 outranks the legacy key at equal quality.
                found.append(((*rank[:2], name.endswith("V2"), rank[2]), token, f"{cfg} [{name}]"))
        if len(found) == before:
            notes.append(f"{root.name}: the saved login has expired (open Claude Desktop to renew it)")
    found.sort(key=lambda item: item[0], reverse=True)
    seen: set[str] = set()
    tokens = []
    for _rank, token, where in found:
        if token not in seen:
            seen.add(token)
            tokens.append((token, where))
    return tokens, notes


# ------------------------------------------------------------ claude.ai transport


def web_session():
    """claude.ai sits behind Cloudflare, which challenges clients whose TLS
    handshake is not a browser's. curl_cffi reproduces Chrome's; plain
    requests is only the fallback when it is missing."""
    try:
        from curl_cffi import requests as curl_requests
    except Exception:                                           # noqa: BLE001
        return None
    global _WEB
    if _WEB is None:
        _WEB = curl_requests.Session(impersonate="chrome")
    return _WEB


_WEB = None


def _web_get(url: str, headers: dict):
    web = web_session()
    if web is not None:
        # The impersonated browser brings its own matching User-Agent.
        return web.get(url, headers=headers, timeout=20)
    return session().get(url, headers={**headers, "User-Agent": BROWSER_UA}, timeout=20)


# ------------------------------------------------------------ saved session


def _session_cache_path() -> Path:
    return app_dir() / "claude-session.bin"


def save_cached_session(jar: dict) -> None:
    if sys.platform != "win32":
        return
    keep = {k: jar[k] for k in ("sessionKey", *_FORWARD_COOKIES) if jar.get(k)}
    try:
        from ..win.dpapi import protect

        _session_cache_path().write_bytes(protect(json.dumps(keep).encode("utf-8")))
    except Exception:                                           # noqa: BLE001
        log.debug("could not save the Claude session", exc_info=True)


def load_cached_session() -> dict:
    if sys.platform != "win32":
        return {}
    try:
        from ..win.dpapi import unprotect

        data = json.loads(unprotect(_session_cache_path().read_bytes()).decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:                                           # noqa: BLE001
        return {}


def forget_cached_session() -> None:
    try:
        _session_cache_path().unlink()
    except OSError:
        pass


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
