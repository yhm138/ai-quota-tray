"""Gemini CLI quota collection.

Gemini CLI (google-gemini/gemini-cli) signs in with Google OAuth and keeps the
tokens in ~/.gemini/oauth_creds.json (plain JSON, unless encrypted storage is
forced). Its per-model quota comes from Google's Code Assist API, the same one
the CLI calls:

  1. read oauth_creds.json (GEMINI_CLI_HOME, ~/.gemini, each WSL distro)
  2. POST v1internal:loadCodeAssist  -> the tier (Free / Standard / ...)
     POST v1internal:retrieveUserQuota -> a bucket per model with the fraction left

Every Google account signed in gets its own page. No sign-in prompt: the token
already on disk is only ever sent to Google.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import time
from pathlib import Path

from ..model import InfoRow, ProviderResult, QuotaWindow, SourceAttempt, now_utc, parse_time
from .account import group_pages, pretty_plan
from .base import Provider, session

log = logging.getLogger(__name__)

ENDPOINT = "https://cloudcode-pa.googleapis.com/v1internal"
LOAD_META = {"ideType": "IDE_UNSPECIFIED", "platform": "PLATFORM_UNSPECIFIED", "pluginType": "GEMINI"}

TIER_NAMES = {"free-tier": "Free", "legacy-tier": "Legacy", "standard-tier": "Standard",
              "enterprise-tier": "Enterprise"}


# ------------------------------------------------------------ credentials


def _gemini_dirs(settings: dict) -> list[tuple[Path, str]]:
    """[(~/.gemini dir, label suffix)]: env override, Windows home, each WSL user."""
    out: list[tuple[Path, str]] = []
    seen: set[str] = set()

    def add(path: Path, suffix: str) -> None:
        key = os.path.normcase(str(path))
        if key not in seen:
            seen.add(key)
            out.append((path, suffix))

    env_home = (settings.get("gemini_home") or os.environ.get("GEMINI_CLI_HOME") or "").strip()
    if env_home:
        add(Path(env_home) / ".gemini", "")
    add(Path.home() / ".gemini", "")
    if settings.get("scan_wsl", True):
        from .claude import _wsl_distros

        for distro in _wsl_distros():
            root = Path(f"\\\\wsl.localhost\\{distro}")
            add(root / "root" / ".gemini", f" - WSL {distro}")
            try:
                users = list((root / "home").iterdir())[:10] if (root / "home").is_dir() else []
            except OSError:
                users = []
            for user in users:
                add(user / ".gemini", f" - WSL {distro}")
    return out


def _read_creds(path: Path) -> dict | None:
    from ..win.shareio import read_text as _read_shared

    text = _read_shared(path)
    if text is None:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) and (data.get("access_token") or data.get("refresh_token")) else None


def discover_credentials(settings: dict) -> tuple[list[tuple[dict, str]], list[str]]:
    """([(creds, where)], notes). Windows and each WSL distro can be different accounts."""
    creds: list[tuple[dict, str]] = []
    notes: list[str] = []
    checked = 0
    for base, suffix in _gemini_dirs(settings):
        path = base / "oauth_creds.json"
        try:
            exists = path.is_file()
        except OSError:
            continue
        if not exists:
            continue
        checked += 1
        data = _read_creds(path)
        if data is None:
            notes.append(f"{path}: no OAuth tokens in it (run gemini once and sign in)")
            continue
        creds.append((data, f"Gemini CLI{suffix}"))
    if not creds and checked == 0:
        notes.append("no ~/.gemini/oauth_creds.json (sign in with `gemini`)")
    return creds, notes


def _jwt_email(token) -> str | None:
    if not isinstance(token, str) or token.count(".") < 2:
        return None
    part = token.split(".")[1]
    try:
        claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except (ValueError, TypeError):
        return None
    email = claims.get("email") if isinstance(claims, dict) else None
    return email if isinstance(email, str) else None


# ------------------------------------------------------------ tokens


def access_token(creds: dict) -> tuple[str | None, str | None]:
    """The access token on disk. (token, error).

    QuotaTray does not refresh the token itself (that would need Gemini CLI's
    OAuth client secret, which is not ours to embed): the CLI refreshes it
    whenever it runs, so the file is usually current. A clearly expired token
    is reported so the panel can say to run `gemini` once.
    """
    token = creds.get("access_token")
    expiry = creds.get("expiry_date")            # ms epoch
    if isinstance(token, str) and token:
        expired = isinstance(expiry, (int, float)) and expiry / 1000.0 < time.time() - 60
        if expired:
            return None, "the saved token has expired; run `gemini` once to refresh it"
        return token, None
    return None, "no access token in oauth_creds.json (sign in with `gemini`)"


# ------------------------------------------------------------ parsing


def quota_windows(payload: dict) -> list[QuotaWindow]:
    """A bucket per model -> a usage window (percent used = 100 - fraction left)."""
    out: list[QuotaWindow] = []
    if not isinstance(payload, dict):
        return out
    order = 10
    for bucket in payload.get("buckets") or []:
        if not isinstance(bucket, dict):
            continue
        model = bucket.get("modelId")
        frac = bucket.get("remainingFraction")
        if not isinstance(model, str) or not isinstance(frac, (int, float)):
            continue
        pct = max(0.0, min(100.0, (1.0 - float(frac)) * 100.0))
        detail = None
        amount = bucket.get("remainingAmount")
        if isinstance(amount, str) and amount:
            unit = bucket.get("tokenType")
            detail = f"{amount} left" + (f" ({unit.lower()})" if isinstance(unit, str) and unit else "")
        out.append(QuotaWindow(
            key=model, label=_model_label(model), percent=pct,
            resets_at=parse_time(bucket.get("resetTime")), detail=detail, order=order,
            exhausted=frac <= 0,
        ))
        order += 1
    return out


def _model_label(model: str) -> str:
    """'gemini-2.5-pro' -> 'Gemini 2.5 Pro'."""
    name = model.replace("-", " ").replace("_", " ").strip()
    words = [w.capitalize() if not any(c.isdigit() for c in w) else w for w in name.split()]
    return " ".join(words) or model


def tier_info(load: dict) -> tuple[str | None, list[InfoRow]]:
    """(plan, rows) from loadCodeAssist."""
    rows: list[InfoRow] = []
    plan = None
    tier = None
    if isinstance(load, dict):
        for key in ("currentTier", "paidTier"):
            node = load.get(key)
            if isinstance(node, dict):
                tier = node
                break
    if isinstance(tier, dict):
        tid = tier.get("id")
        plan = TIER_NAMES.get(tid) or pretty_plan(tier.get("name")) or (
            pretty_plan(tid) if isinstance(tid, str) else None)
        for credit in tier.get("availableCredits") or []:
            if not isinstance(credit, dict):
                continue
            amount = credit.get("amount") or credit.get("balance")
            name = credit.get("name") or "Credits"
            if amount is not None:
                rows.append(InfoRow(str(name), str(amount)))
    return plan, rows


# ------------------------------------------------------------ provider


class GeminiProvider(Provider):
    id = "gemini"
    name = "Gemini CLI"

    def detect(self) -> bool:
        return bool(discover_credentials(self.settings)[0])

    def _headers(self, token: str) -> dict:
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                "Accept": "application/json"}

    def _post(self, method: str, token: str, body: dict):
        return session().post(f"{ENDPOINT}:{method}", headers=self._headers(token),
                              json=body, timeout=25)

    def _page(self, creds: dict, where: str, fetched_at) -> ProviderResult:
        page = ProviderResult(provider_id=self.id, name=self.name, fetched_at=fetched_at, label=where)
        tag = f"Code Assist API ({where})"
        token, err = access_token(creds)
        if not token:
            page.attempts.append(SourceAttempt(tag, False, err or "no access token"))
            return page
        try:
            load_resp = self._post("loadCodeAssist", token, {"metadata": LOAD_META})
        except Exception as exc:                                # noqa: BLE001
            page.attempts.append(SourceAttempt(tag, False, f"loadCodeAssist failed: {exc}"))
            return page
        if load_resp.status_code in (401, 403):
            page.attempts.append(SourceAttempt(tag, False,
                f"token rejected (HTTP {load_resp.status_code}); sign in to Gemini again"))
            return page
        load = load_resp.json() if load_resp.status_code < 400 else {}
        load = load if isinstance(load, dict) else {}
        project = load.get("cloudaicompanionProject")
        plan, rows = tier_info(load)

        body = {"metadata": LOAD_META}
        if isinstance(project, str) and project:
            body = {"project": project}
        try:
            quota_resp = self._post("retrieveUserQuota", token, body)
        except Exception as exc:                                # noqa: BLE001
            page.attempts.append(SourceAttempt(tag, False, f"retrieveUserQuota failed: {exc}"))
            return page
        if quota_resp.status_code >= 400:
            page.attempts.append(SourceAttempt(tag, False,
                f"retrieveUserQuota HTTP {quota_resp.status_code}: {quota_resp.text[:160]}"))
            return page
        try:
            quota = quota_resp.json()
        except ValueError:
            page.attempts.append(SourceAttempt(tag, False, "quota response was not JSON"))
            return page
        windows = quota_windows(quota if isinstance(quota, dict) else {})
        if not windows and not plan:
            page.attempts.append(SourceAttempt(tag, False, f"no quota in response: {str(quota)[:160]}"))
            return page
        page.windows = windows
        page.ok = True
        page.plan = plan
        page.info = rows
        page.account = _jwt_email(creds.get("id_token"))
        page.source = "cloudcode-pa.googleapis.com"
        page.status = "connected"
        page.data_time = now_utc()
        page.attempts.append(SourceAttempt(tag, True, where))
        return page

    def collect(self, result: ProviderResult) -> None:
        found, notes = discover_credentials(self.settings)
        if not found:
            result.attempts.append(SourceAttempt("credentials", False, "; ".join(notes) or "no Gemini CLI login"))
            result.status = "Gemini CLI not detected" if not result.installed else "no Gemini CLI login found"
            return
        for note in notes:
            result.attempts.append(SourceAttempt("credentials", False, note))
        pages = []
        for creds, where in found:
            page = self._page(creds, where, result.fetched_at)
            result.attempts.extend(page.attempts)
            if page.ok:
                pages.append(page)
        if pages:
            pages = group_pages(pages)
            result.adopt(pages[0])
            result.alternates = pages[1:]
            return
        failed = [a for a in result.attempts if not a.ok and a.name.startswith("Code Assist")]
        result.status = failed[-1].detail if failed else "could not read Gemini quota"
