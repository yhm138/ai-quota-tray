"""Render the tray icon on the fly: one small bar per product, so the remaining
quota is readable at 16x16."""
from __future__ import annotations

from PIL import Image, ImageDraw

from . import theme

SIZE = 128


def _rgb(hex_color: str, alpha: int = 255) -> tuple[int, int, int, int]:
    h = hex_color.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), alpha)


def render(entries: list[tuple[str, float | None]], *, warn=75, danger=90, style="bars") -> Image.Image:
    """entries: [(provider_id, percent used or None), ...]; at most 3 are drawn."""
    entries = entries[:3]
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # Dark rounded plate so the icon stays legible on light and dark taskbars.
    d.rounded_rectangle([2, 2, SIZE - 3, SIZE - 3], radius=26, fill=_rgb("#1B1D22", 245))

    if style == "ring":
        return _ring(img, d, entries, warn, danger)

    if not entries:
        d.rounded_rectangle([26, 58, SIZE - 27, 70], radius=6, fill=_rgb(theme.IDLE, 200))
        return img

    pad_x = 20
    total_h = SIZE - 2 * 26
    gap = 12 if len(entries) > 1 else 0
    bar_h = max(10, (total_h - gap * (len(entries) - 1)) // len(entries))
    y = (SIZE - (bar_h * len(entries) + gap * (len(entries) - 1))) // 2

    for pid, pct in entries:
        x0, x1 = pad_x, SIZE - pad_x
        radius = bar_h // 2
        d.rounded_rectangle([x0, y, x1, y + bar_h], radius=radius, fill=_rgb("#41454D", 255))
        if pct is not None and pct > 0:
            width = max(bar_h, int((x1 - x0) * min(100.0, pct) / 100.0))
            color = theme.status_color(pct, warn, danger)
            d.rounded_rectangle([x0, y, x0 + width, y + bar_h], radius=radius, fill=_rgb(color))
        elif pct is None:
            d.rounded_rectangle(
                [x0, y, x0 + bar_h, y + bar_h], radius=radius, fill=_rgb(theme.IDLE)
            )
        # A dot of product color on the left tells the rows apart.
        tint = theme.PROVIDER_TINT.get(pid, theme.ACCENT)
        d.ellipse([x0 - 8, y + bar_h // 2 - 3, x0 - 2, y + bar_h // 2 + 3], fill=_rgb(tint))
        y += bar_h + gap
    return img


def _ring(img, d, entries, warn, danger):
    vals = [p for _, p in entries if p is not None]
    pct = max(vals) if vals else None
    box = [22, 22, SIZE - 23, SIZE - 23]
    d.ellipse(box, outline=_rgb("#41454D"), width=14)
    if pct is not None:
        end = -90 + 360 * min(100.0, pct) / 100.0
        d.arc(box, start=-90, end=end, fill=_rgb(theme.status_color(pct, warn, danger)), width=14)
    return img


def tooltip(summaries: list[str]) -> str:
    """Windows tray tooltips cap out around 127 characters."""
    text = "QuotaTray\n" + "\n".join(summaries)
    return text[:126]
