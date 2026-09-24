"""Data model plus the generic, schema-tolerant response parsing helpers."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Iterator

# ---------------------------------------------------------------- structures


@dataclass
class QuotaWindow:
    """One quota window, e.g. Claude's 5-hour window or Codex's weekly window."""

    key: str
    label: str
    percent: float | None = None          # percent USED, 0-100
    resets_at: datetime | None = None     # reset moment, timezone-aware UTC
    detail: str | None = None             # extra note, e.g. "812 / 1,000 left"
    order: int = 100
    exhausted: bool = False

    @property
    def percent_text(self) -> str:
        if self.percent is None:
            return "--"
        if self.percent >= 99.95:
            return "100%"
        if self.percent < 10 and abs(self.percent - round(self.percent)) > 0.05:
            return f"{self.percent:.1f}%"
        return f"{self.percent:.0f}%"


@dataclass
class InfoRow:
    """One line of account detail under the usage bars, e.g. Plan: Max 20x."""

    label: str
    value: str
    tone: str = ""                         # "" | "good" | "warn"


@dataclass
class ResetGrant:
    """A banked limit reset: saved on the account until used or expired."""

    count: int
    expires_at: datetime | None = None
    note: str = ""                         # what it clears, when it can be used

    def active(self, now: datetime | None = None) -> bool:
        now = now or now_utc()
        return self.count > 0 and (self.expires_at is None or self.expires_at > now)


@dataclass
class SourceAttempt:
    """Diagnostic record for one collection attempt."""

    name: str
    ok: bool
    detail: str = ""


@dataclass
class ProviderResult:
    provider_id: str
    name: str
    ok: bool = False
    status: str = ""                       # one-line status shown to the user
    source: str | None = None              # which source actually worked
    account: str | None = None
    plan: str | None = None
    windows: list[QuotaWindow] = field(default_factory=list)
    fetched_at: datetime | None = None
    data_time: datetime | None = None      # when the data itself was produced
    attempts: list[SourceAttempt] = field(default_factory=list)
    installed: bool = True                 # is the product present on this machine
    info: list[InfoRow] = field(default_factory=list)       # plan, renewal, credits
    resets: list[ResetGrant] = field(default_factory=list)  # banked limit resets

    def active_resets(self, now: datetime | None = None) -> list[ResetGrant]:
        return sorted(
            (g for g in self.resets if g.active(now)),
            key=lambda g: g.expires_at or datetime.max.replace(tzinfo=timezone.utc),
        )

    @property
    def resets_available(self) -> int:
        return sum(g.count for g in self.active_resets())

    @property
    def worst_percent(self) -> float | None:
        vals = [w.percent for w in self.windows if w.percent is not None]
        return max(vals) if vals else None

    def sorted_windows(self) -> list[QuotaWindow]:
        return sorted(self.windows, key=lambda w: (w.order, w.label))

    def to_cache(self) -> dict:
        return {
            "provider_id": self.provider_id,
            "name": self.name,
            "ok": self.ok,
            "status": self.status,
            "source": self.source,
            "account": self.account,
            "plan": self.plan,
            "installed": self.installed,
            "fetched_at": _iso(self.fetched_at),
            "data_time": _iso(self.data_time),
            "windows": [
                {
                    "key": w.key,
                    "label": w.label,
                    "percent": w.percent,
                    "resets_at": _iso(w.resets_at),
                    "detail": w.detail,
                    "order": w.order,
                    "exhausted": w.exhausted,
                }
                for w in self.windows
            ],
            "info": [{"label": i.label, "value": i.value, "tone": i.tone} for i in self.info],
            "resets": [
                {"count": g.count, "expires_at": _iso(g.expires_at), "note": g.note}
                for g in self.resets
            ],
        }

    @classmethod
    def from_cache(cls, d: dict) -> "ProviderResult":
        r = cls(provider_id=d.get("provider_id", "?"), name=d.get("name", "?"))
        r.ok = bool(d.get("ok"))
        r.status = d.get("status", "")
        r.source = d.get("source")
        r.account = d.get("account")
        r.plan = d.get("plan")
        r.installed = bool(d.get("installed", True))
        r.fetched_at = parse_time(d.get("fetched_at"))
        r.data_time = parse_time(d.get("data_time"))
        r.windows = [
            QuotaWindow(
                key=w.get("key", "?"),
                label=w.get("label", "?"),
                percent=w.get("percent"),
                resets_at=parse_time(w.get("resets_at")),
                detail=w.get("detail"),
                order=w.get("order", 100),
                exhausted=bool(w.get("exhausted")),
            )
            for w in d.get("windows", [])
        ]
        r.info = [
            InfoRow(i.get("label", ""), i.get("value", ""), i.get("tone", ""))
            for i in d.get("info", []) if isinstance(i, dict)
        ]
        r.resets = [
            ResetGrant(int(g.get("count") or 0), parse_time(g.get("expires_at")), g.get("note", ""))
            for g in d.get("resets", []) if isinstance(g, dict)
        ]
        return r


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


# ---------------------------------------------------------------- time parsing


def parse_time(value: Any) -> datetime | None:
    """Parse the many timestamp shapes these APIs use into an aware datetime."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        # Both second and millisecond epochs show up in the wild.
        ts = float(value)
        if ts > 1e12:
            ts /= 1000.0
        if ts < 1e8:          # clearly not an epoch timestamp
            return None
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        if s.isdigit():
            return parse_time(int(s))
        s = s.replace("Z", "+00:00")
        # fromisoformat rejects sub-microsecond precision, so trim it.
        s = re.sub(r"(\.\d{6})\d+", r"\1", s)
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return None


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def humanize_delta(target: datetime | None, *, now: datetime | None = None) -> str:
    """Render time-until-reset as 3d 2h / 4h 12m / 7m."""
    if target is None:
        return ""
    now = now or now_utc()
    secs = int((target - now).total_seconds())
    if secs <= 0:
        return "resetting"
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h" if hours else f"{days}d"
    if hours:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    if minutes:
        return f"{minutes}m"
    return "<1m"


def humanize_age(t: datetime | None, *, now: datetime | None = None) -> str:
    if t is None:
        return ""
    now = now or now_utc()
    secs = max(0, int((now - t).total_seconds()))
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


# ------------------------------------------- generic, change-tolerant extraction

_USED_KEYS = (
    "utilization", "used_percent", "usedPercent", "percentUsed", "percent_used",
    "usedPercentage", "used_percentage",
)
_RESET_AT_KEYS = (
    "resets_at", "resetsAt", "reset_at", "resetAt", "reset_time", "resetTime",
    "resetsAtUtc", "resets_at_utc",
)
_RESET_IN_KEYS = (
    "resets_in_seconds", "resetsInSeconds", "reset_in_seconds", "secondsUntilReset",
    "seconds_until_reset", "resets_in_s",
)


def _num(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def iter_quota_nodes(obj: Any, path: tuple[str, ...] = ()) -> Iterator[tuple[tuple[str, ...], dict]]:
    """Recursively yield every dict node that looks like a quota window.

    A dict qualifies as soon as it carries a usage-percentage field. That way,
    if a vendor reshuffles the envelope around these objects, we still find them
    as long as the field names hold.
    """
    if isinstance(obj, dict):
        if any(_num(obj.get(k)) is not None for k in _USED_KEYS):
            yield path, obj
        for key, value in obj.items():
            yield from iter_quota_nodes(value, path + (str(key),))
    elif isinstance(obj, list):
        for idx, value in enumerate(obj):
            yield from iter_quota_nodes(value, path + (str(idx),))


def node_used(node: dict) -> float | None:
    for k in _USED_KEYS:
        v = _num(node.get(k))
        if v is not None:
            return v
    return None


def node_reset(node: dict, *, base: datetime | None = None) -> datetime | None:
    for k in _RESET_AT_KEYS:
        dt = parse_time(node.get(k))
        if dt:
            return dt
    for k in _RESET_IN_KEYS:
        secs = _num(node.get(k))
        if secs is not None and secs > 0:
            from datetime import timedelta
            return (base or now_utc()) + timedelta(seconds=secs)
    return None


def decide_percent_scale(values: Iterable[float]) -> float:
    """Decide whether a batch of usage values is 0-1 or 0-100; return the multiplier.

    Rule: if any value exceeds 1, the batch is already a percentage (x1);
    otherwise treat it as a fraction (x100). The pathological case (a true
    percent API reporting under 1% usage) can be pinned via the provider's
    percent_scale setting: "percent" or "fraction".
    """
    vals = [float(v) for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]
    if not vals:
        return 1.0
    return 1.0 if max(vals) > 1.0 else 100.0


def apply_scale(value: float | None, scale: float) -> float | None:
    if value is None:
        return None
    return max(0.0, min(100.0, value * scale))
