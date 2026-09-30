"""Color palette."""
from __future__ import annotations

BG = "#17181C"
BG_CARD = "#1F2126"
BG_ROW = "#25272E"
FG = "#ECEDEF"
FG_DIM = "#9AA0A8"
FG_FAINT = "#6C727B"
BORDER = "#32353D"
ACCENT = "#C96442"

OK = "#3FB950"
WARN = "#D9A21B"
DANGER = "#E5534B"
IDLE = "#4B5059"

PROVIDER_TINT = {
    "claude": "#C96442",
    "codex": "#10A37F",
    "antigravity": "#4285F4",
}


def status_color(percent: float | None, warn: float = 75, danger: float = 90) -> str:
    if percent is None:
        return IDLE
    if percent >= danger:
        return DANGER
    if percent >= warn:
        return WARN
    return OK
