"""DeepSeek Harness account wallets, separate from user-supplied API keys.

Read the official account grant without changing Harness's credential store.
Only the official Platform origin receives it; redirects are never followed.
The token stays out of ProviderResult (including its API-key Copy field).
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from decimal import Decimal, DecimalException, InvalidOperation, ROUND_DOWN, ROUND_HALF_UP, localcontext
from pathlib import Path

from .. import __version__
from ..model import InfoRow, ProviderResult, SourceAttempt, now_utc
from .base import Provider
from .deepseek import _homes, _read

ORIGIN = "https://platform.deepseek.com"
BALANCE_URL = ORIGIN + "/api/v0/users/get_user_summary"
RECORD = "deepseek-account-platform/default"
SIGN_IN = "sign in again in DeepSeek Harness"
_DECIMAL = re.compile(r"-?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", re.ASCII)
_MAX_DECIMAL = Decimal("79228162514264337593543950335")


def session():
    # A fresh jar for each query: account grants do not need browser cookies.
    import requests
    from ..tls import ca_bundle

    result = requests.Session()
    result.verify = ca_bundle() or True
    return result


def _scalar(value: str):
    value = value.strip()
    if value.startswith('"'):
        parsed, end = json.JSONDecoder().raw_decode(value)
        if value[end:].strip() and not value[end:].lstrip().startswith("#"):
            raise ValueError("unsupported scalar")
        return parsed
    if value.startswith("'"):
        match = re.fullmatch(r"'((?:[^']|'')*)'\s*(?:#.*)?", value)
        if not match:
            raise ValueError("unsupported scalar")
        return match[1].replace("''", "'")
    value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
    if re.fullmatch(r"[0-9]+", value):
        return int(value)
    return value


def credential_token(text: str) -> str | None:
    """Project only the versioned grant from DSH's emitted YAML or JSON.

    This intentionally supports the scalar/block mappings written by DSH,
    not arbitrary YAML tags, aliases or executable constructors.
    """
    try:
        text = text.lstrip("\ufeff")
        if text.lstrip().startswith("{"):
            def unique(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError("duplicate mapping key")
                    result[key] = value
                return result
            root = json.loads(text, object_pairs_hook=unique)
            record = root.get("records", {}).get(RECORD, {})
            payload = record.get("payload", {})
            fields = (root.get("version"), record.get("kind"), payload.get("version"),
                      payload.get("token"), payload.get("issuer"))
        else:
            wanted = {("version",), ("records", RECORD, "kind"),
                      *(('records', RECORD, 'payload', k) for k in ('version', 'token', 'issuer'))}
            ancestors = {p[:n] for p in wanted for n in range(1, len(p))}
            stack: list[tuple[int, str]] = []
            values, seen = {}, set()
            for raw in text.splitlines():
                if not raw.strip() or raw.lstrip().startswith("#") or raw.strip() == "---":
                    continue
                if "\t" in raw[:len(raw) - len(raw.lstrip())]:
                    return None
                indent = len(raw) - len(raw.lstrip())
                while stack and stack[-1][0] >= indent:
                    stack.pop()
                match = re.match(r"\s*([^:]+):(?:\s+(.*)|\s*)$", raw)
                if not match:
                    # Other credential kinds can contain lists and opaque data.
                    if tuple(k for _, k in stack) in ancestors:
                        return None
                    continue
                key = _scalar(match[1])
                path = tuple(k for _, k in stack) + (key,)
                value = (match[2] or "").strip()
                if path in wanted or path in ancestors:
                    if path in seen:
                        return None
                    seen.add(path)
                    if path in ancestors and value and not value.startswith("#"):
                        return None
                    if path in wanted:
                        values[path] = _scalar(value)
                if not value or value.startswith("#"):
                    stack.append((indent, key))
            fields = (values.get(("version",)), values.get(("records", RECORD, "kind")),
                      *(values.get(("records", RECORD, "payload", k)) for k in ('version', 'token', 'issuer')))
        version, kind, grant_version, token, issuer = fields
        if type(version) is not int or version != 1 or kind != "grant":
            return None
        if type(grant_version) is not int or grant_version != 1 or issuer != ORIGIN:
            return None
        return token if isinstance(token, str) and re.fullmatch(r"[\x21-\x7e]+", token) else None
    except (ValueError, TypeError, AttributeError, OverflowError):
        return None


def credential_files(settings: dict, env=None) -> list[tuple[Path, str]]:
    env = os.environ if env is None else env
    found = []
    for home, distro in _homes(settings):
        if distro and not settings.get("scan_wsl", True):
            continue
        override = (settings.get("home") or env.get("DSH_HOME")) if not distro else None
        folder = Path(override).expanduser() if isinstance(override, str) and override.strip() else home / ".dsh"
        path = folder / ".credentials.yaml"
        try:
            if path.is_file():
                found.append((path, "DeepSeek Harness" + (f" - WSL {distro}" if distro else "")))
        except OSError:
            continue
    return found


def wallet_rows(payload: dict, low: Decimal) -> tuple[list[InfoRow], str | None]:
    # Keep the full C# decimal range during addition, including its 29-digit max.
    with localcontext() as context:
        context.prec = 60
        return _wallet_rows(payload, low)


def _wallet_rows(payload: dict, low: Decimal) -> tuple[list[InfoRow], str | None]:
    totals: dict[str, list[Decimal]] = {}
    try:
        for index, key in enumerate(("normal_wallets", "bonus_wallets")):
            wallets = payload.get(key)
            if not isinstance(wallets, list):
                return [], None
            for wallet in wallets:
                if not isinstance(wallet, dict):
                    return [], None
                currency, amount = wallet.get("currency"), wallet.get("balance")
                if currency not in ("CNY", "USD") or not isinstance(amount, str) or not _DECIMAL.fullmatch(amount):
                    return [], None
                number = Decimal(amount)
                # Match the bounded decimal range used by the C# implementation.
                if not number.is_finite() or number.copy_abs() > _MAX_DECIMAL or (number and number.copy_abs() < Decimal("1e-28")):
                    return [], None
                totals.setdefault(currency, [Decimal(0), Decimal(0)])[index] += number
                if abs(totals[currency][index]) > _MAX_DECIMAL:
                    return [], None
        rows, headlines = [], []
        for currency in (c for c in ("CNY", "USD") if c in totals):
            normal, bonus = totals[currency]
            symbol = "\u00a5" if currency == "CNY" else "$"
            def money(value):
                # Match Harness's displayed wallets, including nonzero sub-cent values.
                if 0 < value < Decimal(".01"):
                    return "<" + symbol + "0.01"
                if Decimal("-.01") < value < 0:
                    return "-" + symbol + "0.01"
                rounded = value.copy_abs().quantize(Decimal(".01"), rounding=ROUND_HALF_UP if value < 0 else ROUND_DOWN)
                return ("-" if value < 0 else "") + symbol + format(rounded, ",.2f")
            total = normal + bonus
            if abs(total) > _MAX_DECIMAL:
                return [], None
            rows.extend((InfoRow("Balance", money(total), "warn" if total < low else "good"),
                         InfoRow("Topped up", money(normal)),
                         InfoRow("Granted", money(bonus) + " (can expire)")))
            headlines.append(money(total))
        return rows, " / ".join(headlines) or None
    except (DecimalException, ValueError, OverflowError):
        return [], None


def request_headers(token: str) -> dict[str, str]:
    offset = datetime.now().astimezone().utcoffset()
    return {
        "x-dsh-auth-token": token, "Accept": "*/*", "User-Agent": f"QuotaTray/{__version__}",
        "x-client-bundle-id": "", "x-client-platform": "desktop-win",
        "x-client-version": __version__, "x-client-locale": "zh_CN",
        "x-client-timezone-offset": str(int(offset.total_seconds()) if offset else 0),
    }


class DshManagedProvider(Provider):
    id = "dsh_managed"
    name = "\u7531 Harness \u7ba1\u7406"
    billing = "payg"

    def detect(self) -> bool:
        return bool(credential_files(self.settings))

    def _page(self, token: str, label: str, result: ProviderResult) -> ProviderResult:
        page = ProviderResult(self.id, self.name, billing=self.billing, label=label,
                              fetched_at=result.fetched_at)
        def fail(detail):
            page.status = detail
            page.attempts.append(SourceAttempt("Harness balance", False, detail))
            return page
        try:
            with session() as client:
                response = client.get(BALANCE_URL, headers=request_headers(token), timeout=30,
                                      allow_redirects=False)
                if response.status_code == 200:
                    payload = response.json()
            if response.status_code == 401:
                return fail(SIGN_IN)
            if response.status_code != 200:
                return fail(f"HTTP {response.status_code}")
        except Exception:  # A transport/body error may contain credentials. Never log its text.
            return fail("could not read the Harness balance response")
        if not isinstance(payload, dict):
            return fail("invalid Harness balance response")
        code = payload.get("code")
        if type(code) is not int:
            return fail("invalid Harness response code")
        if code == 40003:
            return fail(SIGN_IN)
        if code != 0:
            return fail(f"Harness response code {code}")
        data = payload.get("data")
        if not isinstance(data, dict) or type(data.get("biz_code")) is not int:
            return fail("invalid Harness business response")
        if data["biz_code"] != 0:
            return fail(f"Harness business code {data['biz_code']}")
        value = data.get("biz_data")
        try:
            low = Decimal(str(self.settings.get("low_balance", 5)))
            if not low.is_finite() or low < 0 or low > _MAX_DECIMAL:
                low = Decimal(5)
        except InvalidOperation:
            low = Decimal(5)
        rows, headline = wallet_rows(value, low) if isinstance(value, dict) else ([], None)
        if not rows:
            return fail("no valid wallets in Harness response")
        page.ok, page.info, page.headline = True, rows, headline
        page.plan, page.status = "Harness managed", "connected"
        page.source, page.data_time = "platform.deepseek.com/api/v0/users/get_user_summary", now_utc()
        page.attempts.append(SourceAttempt("Harness balance", True, "account wallets received"))
        return page

    def collect(self, result: ProviderResult) -> None:
        pages, credentials = [], {}
        for path, label in credential_files(self.settings):
            try:
                text = _read(path)
            except (OSError, UnicodeError, ValueError):
                text = None
            token = credential_token(text) if text else None
            if not token:
                result.attempts.append(SourceAttempt("Harness login", False, "no supported official account grant; " + SIGN_IN))
                continue
            labels = credentials.setdefault(token, [])
            if label not in labels:
                labels.append(label)
        for token, labels in credentials.items():
            page = self._page(token, " + ".join(labels), result)
            result.attempts.extend(page.attempts)
            pages.append(page)
        if pages:
            pages.sort(key=lambda page: not page.ok)
            result.adopt(pages[0])
            result.alternates = pages[1:]
        elif result.attempts:
            result.status = result.attempts[-1].detail
        else:
            result.status = "no Harness account found; sign in in DeepSeek Harness"
