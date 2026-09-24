"""Daily nudge about banked limit resets that are going unused."""
from __future__ import annotations

from datetime import datetime

from .model import ProviderResult
from .providers.account import fmt_expiry


def due(last_day: str | None, now_local: datetime, hour: int) -> bool:
    """Once a day, from `hour` local time on."""
    return now_local.hour >= hour and last_day != now_local.date().isoformat()


def unused_resets_text(results: list[ProviderResult]) -> str | None:
    lines = []
    for r in results:
        active = r.active_resets()
        if not active:
            continue
        count = sum(g.count for g in active)
        first = min((g.expires_at for g in active if g.expires_at), default=None)
        line = f"{r.name}: {count} unused reset{'s' if count != 1 else ''}"
        if first:
            line += f", first expires {fmt_expiry(first)}"
        lines.append(line)
    if not lines:
        return None
    return "\n".join(lines) + "\nApply them in the app's Settings > Usage before they expire."
