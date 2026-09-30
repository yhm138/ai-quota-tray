"""Formatting shared by the plan / subscription / credits / resets details."""
from __future__ import annotations

import re
from datetime import datetime

from ..model import InfoRow, ProviderResult, ResetGrant, humanize_delta, now_utc


def fmt_date(dt: datetime | None) -> str:
    """'Oct 22' this year, 'Oct 22, 2027' otherwise, in local time."""
    if dt is None:
        return "?"
    local = dt.astimezone()
    text = f"{local:%b} {local.day}"
    if local.year != now_utc().astimezone().year:
        text += f", {local.year}"
    return text


def fmt_expiry(dt: datetime | None) -> str:
    """'Oct 22 (in 3d 4h)', or 'no expiry'."""
    if dt is None:
        return "no expiry date given"
    left = humanize_delta(dt)
    return f"{fmt_date(dt)} (in {left})" if left and left != "resetting" else fmt_date(dt)


def soon(dt: datetime | None, days: float) -> bool:
    return dt is not None and (dt - now_utc()).total_seconds() < days * 86400


def pretty_plan(raw: str | None) -> str | None:
    """'default_claude_max_20x' -> 'Max 20x', 'prolite' -> 'Pro Lite'."""
    if not raw or not isinstance(raw, str):
        return None
    s = raw.strip().lower()
    for prefix in ("default_", "claude_", "chatgpt_"):
        if s.startswith(prefix):
            s = s[len(prefix):]
    s = s.replace("claude_", "")
    known = {"prolite": "Pro Lite", "plus": "Plus", "pro": "Pro", "team": "Team",
             "business": "Business", "enterprise": "Enterprise", "edu": "Edu", "free": "Free", "ai": "Free"}
    if s in known:
        return known[s]
    words = [w for w in re.split(r"[_\s-]+", s) if w]
    return " ".join(w if re.fullmatch(r"\d+x", w) else w.capitalize() for w in words) or None


def money(amount, currency: str | None = "USD") -> str:
    symbol = {"USD": "$", "EUR": "\u20ac", "GBP": "\u00a3", "CNY": "\u00a5", "RMB": "\u00a5"}.get(
        (currency or "USD").upper())
    return f"{symbol}{amount:,.2f}" if symbol else f"{amount:,.2f} {currency}"


def minor_amount(node) -> float | None:
    """{'amount_minor': 1359, 'exponent': 2} -> 13.59."""
    if not isinstance(node, dict):
        return None
    minor = node.get("amount_minor")
    if not isinstance(minor, (int, float)):
        return None
    exp = node.get("exponent")
    return float(minor) / (10 ** (exp if isinstance(exp, int) else 2))


def to_float(value) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def reset_rows(resets: list[ResetGrant], where: str) -> list[InfoRow]:
    """Panel lines for banked resets (shared with Codex)."""
    active = [g for g in resets if g.active()]
    if not active:
        return []
    total = sum(g.count for g in active)
    first = min((g.expires_at for g in active if g.expires_at), default=None)
    value = f"{total} unused" + (f" \u00b7 first expires {fmt_expiry(first)}" if first else "")
    rows = [InfoRow("Resets", value, "warn" if soon(first, 3) else "good")]
    notes = {g.note for g in active if g.note}
    if notes:
        rows.append(InfoRow("", "; ".join(sorted(notes)) + f" \u00b7 use in {where}"))
    return rows


def same_account(a: ProviderResult, b: ProviderResult) -> bool:
    """Two logins are one account when their emails match; without emails,
    identical usage numbers are the best evidence there is."""
    if a.account and b.account:
        return a.account.strip().lower() == b.account.strip().lower()
    return [(w.key, round(w.percent or 0.0, 1)) for w in a.windows] == \
        [(w.key, round(w.percent or 0.0, 1)) for w in b.windows]


def group_pages(pages: list[ProviderResult]) -> list[ProviderResult]:
    """One page per account; logins of the same account share a page."""
    out: list[ProviderResult] = []
    for page in pages:
        twin = next((p for p in out if same_account(p, page)), None)
        if twin is None:
            out.append(page)
        elif page.label and page.label not in (twin.label or ""):
            twin.label = f"{twin.label} + {page.label}" if twin.label else page.label
    return out
