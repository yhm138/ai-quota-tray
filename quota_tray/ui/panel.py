"""The tray popup: a borderless dark card pinned to the bottom-right corner."""
from __future__ import annotations

import logging
import tkinter as tk
from tkinter import font as tkfont

from ..model import ProviderResult, humanize_age, humanize_delta, now_utc
from . import theme

log = logging.getLogger(__name__)

WIDTH = 430
MAX_LABEL = 24
PAD = 16


def _ellipsis(text: str, limit: int = MAX_LABEL) -> str:
    """Shorten with a visible marker, so a cut name never masquerades as a
    different one (two models both ending up as "Gemini 3.1 Pro (" is worse
    than two rows that plainly say they are truncated)."""
    text = str(text)
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _rounded(canvas: tk.Canvas, x0, y0, x1, y1, r, **kw):
    r = min(r, (y1 - y0) / 2, (x1 - x0) / 2)
    canvas.create_rectangle(x0 + r, y0, x1 - r, y1, outline="", **kw)
    canvas.create_rectangle(x0, y0 + r, x1, y1 - r, outline="", **kw)
    for cx, cy in ((x0, y0), (x1 - 2 * r, y0), (x0, y1 - 2 * r), (x1 - 2 * r, y1 - 2 * r)):
        canvas.create_oval(cx, cy, cx + 2 * r, cy + 2 * r, outline="", **kw)


class Panel:
    """A window that lives for the whole session but stays hidden until shown."""

    def __init__(self, root: tk.Tk, callbacks: dict):
        self.root = root
        self.cb = callbacks
        self.win: tk.Toplevel | None = None
        self.mode = "usage"                 # usage | diagnostics
        self._diag_box: tk.Text | None = None
        self._diag_top = "1.0"              # first visible line survives re-renders
        self._results: list[ProviderResult] = []
        self._meta: dict = {}
        self.f_title = tkfont.Font(family="Segoe UI", size=11, weight="bold")
        self.f_body = tkfont.Font(family="Segoe UI", size=9)
        self.f_small = tkfont.Font(family="Segoe UI", size=8)
        self.f_pct = tkfont.Font(family="Consolas", size=10, weight="bold")
        self.f_mono = tkfont.Font(family="Consolas", size=8)
        self.f_body_mono = tkfont.Font(family="Consolas", size=9)

    # ------------------------------------------------------------ lifecycle

    def set_data(self, results: list[ProviderResult], meta: dict) -> None:
        self._results = results
        self._meta = meta
        if self.win is not None and self.win.winfo_exists():
            self._render()

    def toggle(self) -> None:
        if self.win is not None and self.win.winfo_exists():
            self.hide()
        else:
            self.show()

    def show(self, mode: str | None = None) -> None:
        if mode:
            if mode != self.mode:
                self._diag_top = "1.0"
            self.mode = mode
        if self.win is not None and self.win.winfo_exists():
            self._render()
            self.win.deiconify()
            self.win.lift()
            self.win.focus_force()
            return
        win = tk.Toplevel(self.root)
        win.withdraw()
        win.overrideredirect(True)
        win.configure(bg=theme.BORDER)
        win.attributes("-topmost", True)
        try:
            win.attributes("-alpha", 0.985)
        except tk.TclError:
            pass
        win.bind("<Escape>", lambda _e: self.hide())
        win.bind("<FocusOut>", self._on_focus_out)
        self.win = win
        self._render()
        win.deiconify()
        win.lift()
        win.focus_force()

    def hide(self) -> None:
        if self.win is not None and self.win.winfo_exists():
            self.win.withdraw()

    def destroy(self) -> None:
        if self.win is not None and self.win.winfo_exists():
            self.win.destroy()
        self.win = None

    def _on_focus_out(self, _event=None) -> None:
        # Clicking a child widget also fires FocusOut, so check the real focus
        # owner a moment later instead of hiding immediately.
        def check():
            if self.win is None or not self.win.winfo_exists():
                return
            try:
                focus = self.win.focus_displayof()
            except (KeyError, tk.TclError):
                focus = None
            if focus is None:
                self.hide()

        self.root.after(180, check)

    # ------------------------------------------------------------ rendering

    def _render(self) -> None:
        win = self.win
        if win is None or not win.winfo_exists():
            return
        if self._diag_box is not None:
            try:
                self._diag_top = self._diag_box.index("@0,0")
            except tk.TclError:
                pass
            self._diag_box = None
        for child in win.winfo_children():
            child.destroy()

        outer = tk.Frame(win, bg=theme.BG, padx=0, pady=0)
        outer.pack(padx=1, pady=1, fill="both", expand=True)

        self._header(outer)
        body = tk.Frame(outer, bg=theme.BG)
        body.pack(fill="both", expand=True, padx=PAD, pady=(4, 0))
        if self.mode == "diagnostics":
            self._diagnostics(body)
        else:
            self._usage(body)
        self._footer(outer)

        win.update_idletasks()
        self._place(win)
        if self._diag_box is not None:
            # Only now is the final size known; restoring earlier drifts.
            win.update_idletasks()
            self._diag_box.yview(self._diag_top)

    def _header(self, parent) -> None:
        bar = tk.Frame(parent, bg=theme.BG)
        bar.pack(fill="x", padx=PAD, pady=(PAD, 2))
        title = "Diagnostics" if self.mode == "diagnostics" else "Quota overview"
        tk.Label(bar, text=title, bg=theme.BG, fg=theme.FG, font=self.f_title).pack(side="left")

        sub = self._meta.get("subtitle") or ""
        tk.Label(bar, text=sub, bg=theme.BG, fg=theme.FG_FAINT, font=self.f_small).pack(
            side="right", pady=(4, 0)
        )

    def _usage(self, parent) -> None:
        if not self._results:
            tk.Label(
                parent, text="Loading...", bg=theme.BG, fg=theme.FG_DIM, font=self.f_body
            ).pack(anchor="w", pady=20)
            return
        for result in self._results:
            self._provider_card(parent, result)

    def _provider_card(self, parent, result: ProviderResult) -> None:
        card = tk.Frame(parent, bg=theme.BG_CARD, highlightthickness=0)
        card.pack(fill="x", pady=6)
        inner = tk.Frame(card, bg=theme.BG_CARD)
        inner.pack(fill="x", padx=12, pady=10)

        head = tk.Frame(inner, bg=theme.BG_CARD)
        head.pack(fill="x")
        tint = theme.PROVIDER_TINT.get(result.provider_id, theme.ACCENT)
        dot = tk.Canvas(head, width=10, height=10, bg=theme.BG_CARD, highlightthickness=0)
        dot.create_oval(1, 1, 9, 9, fill=tint, outline="")
        dot.pack(side="left", pady=(3, 0))
        tk.Label(
            head, text=" " + result.name, bg=theme.BG_CARD, fg=theme.FG, font=self.f_title
        ).pack(side="left")

        meta_bits = [b for b in (result.plan, result.account) if b]
        if meta_bits:
            tk.Label(
                head,
                text=_ellipsis(" - ".join(meta_bits), 34),
                bg=theme.BG_CARD,
                fg=theme.FG_FAINT,
                font=self.f_small,
            ).pack(side="right", pady=(4, 0))

        if not result.ok:
            tk.Label(
                inner,
                text=result.status or "unavailable",
                bg=theme.BG_CARD,
                fg=theme.FG_DIM,
                font=self.f_small,
                wraplength=WIDTH - 60,
                justify="left",
            ).pack(anchor="w", pady=(6, 0))
            return

        rows = tk.Frame(inner, bg=theme.BG_CARD)
        rows.pack(fill="x", pady=(8, 0))
        rows.columnconfigure(1, weight=1)
        warn = float(self._meta.get("warn", 75))
        danger = float(self._meta.get("danger", 90))

        for i, window in enumerate(result.sorted_windows()):
            tk.Label(
                rows,
                text=_ellipsis(window.label),
                bg=theme.BG_CARD,
                fg=theme.FG_DIM,
                font=self.f_body,
                anchor="w",
            ).grid(row=i, column=0, sticky="w", padx=(0, 8), pady=3)

            bar = tk.Canvas(rows, height=10, bg=theme.BG_CARD, highlightthickness=0, width=110)
            bar.grid(row=i, column=1, sticky="ew", padx=(4, 8), pady=3)
            bar.bind(
                "<Configure>",
                lambda e, c=bar, w=window: self._draw_bar(c, w, warn, danger),
            )

            tk.Label(
                rows,
                text=window.percent_text,
                bg=theme.BG_CARD,
                fg=theme.status_color(window.percent, warn, danger),
                font=self.f_pct,
                anchor="e",
                width=5,
            ).grid(row=i, column=2, sticky="e", pady=3)

            trailing = window.detail or humanize_delta(window.resets_at)
            tk.Label(
                rows,
                text=trailing,
                bg=theme.BG_CARD,
                fg=theme.FG_FAINT,
                font=self.f_small,
                anchor="e",
            ).grid(row=i, column=3, sticky="e", padx=(6, 0), pady=3)

        self._account_details(inner, result)

        foot_bits = []
        if result.source:
            foot_bits.append(result.source)
        if result.data_time and (now_utc() - result.data_time).total_seconds() > 900:
            foot_bits.append(f"data {humanize_age(result.data_time)}")
        if foot_bits:
            tk.Label(
                inner,
                text=_ellipsis(" - ".join(foot_bits), 58),
                bg=theme.BG_CARD,
                fg=theme.FG_FAINT,
                font=self.f_small,
            ).pack(anchor="w", pady=(8, 0))

    def _account_details(self, parent, result: ProviderResult) -> None:
        """Plan, subscription, credits and banked resets under the bars."""
        from ..providers.account import reset_rows

        where = {"claude": "Claude > Settings > Usage", "codex": "Codex > Settings > Usage"}
        rows = list(result.info) + reset_rows(
            result.resets, where.get(result.provider_id, "the app's usage settings")
        )
        if not rows:
            return
        box = tk.Frame(parent, bg=theme.BG_CARD)
        box.pack(fill="x", pady=(8, 0))
        box.columnconfigure(1, weight=1)
        tones = {"good": theme.OK, "warn": theme.WARN}
        for i, row in enumerate(rows):
            tk.Label(
                box, text=row.label, bg=theme.BG_CARD, fg=theme.FG_FAINT,
                font=self.f_small, anchor="w",
            ).grid(row=i, column=0, sticky="nw", padx=(0, 10), pady=1)
            tk.Label(
                box, text=row.value, bg=theme.BG_CARD,
                fg=tones.get(row.tone, theme.FG_DIM), font=self.f_small,
                anchor="w", justify="left", wraplength=WIDTH - 150,
            ).grid(row=i, column=1, sticky="w", pady=1)

    def _draw_bar(self, canvas: tk.Canvas, window, warn, danger) -> None:
        canvas.delete("all")
        w = canvas.winfo_width()
        h = canvas.winfo_height()
        if w <= 2:
            return
        _rounded(canvas, 0, 1, w, h - 1, 5, fill=theme.BG_ROW)
        pct = window.percent
        if pct is None:
            return
        filled = max(h - 2, int(w * min(100.0, pct) / 100.0))
        _rounded(canvas, 0, 1, filled, h - 1, 5, fill=theme.status_color(pct, warn, danger))

    def _diagnostics(self, parent) -> None:
        wrap = tk.Frame(parent, bg=theme.BG_CARD)
        wrap.pack(fill="both", expand=True, pady=(4, 0))
        box = tk.Text(
            wrap,
            bg=theme.BG_CARD,
            fg=theme.FG_DIM,
            font=self.f_body_mono,
            relief="flat",
            highlightthickness=0,
            height=24,
            width=76,
            wrap="word",
            padx=10,
            pady=8,
            insertbackground=theme.FG,
        )
        scroll = tk.Scrollbar(
            wrap,
            command=box.yview,
            width=10,
            bg=theme.BG_CARD,
            troughcolor=theme.BG_CARD,
            activebackground=theme.FG_FAINT,
            relief="flat",
            borderwidth=0,
        )
        box.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        box.pack(side="left", fill="both", expand=True)
        try:
            text = self.cb["diagnostics_text"]()
        except Exception as exc:                                # noqa: BLE001
            text = f"could not build the diagnostics report: {exc}"
        box.insert("end", text)
        box.configure(state="disabled")
        # "break": Text has its own wheel binding, and running both doubled
        # every scroll step.
        box.bind(
            "<MouseWheel>",
            lambda e: (box.yview_scroll(int(-e.delta / 120) * 3, "units"), "break")[1],
        )
        self._diag_box = box

    def _footer(self, parent) -> None:
        bar = tk.Frame(parent, bg=theme.BG)
        bar.pack(fill="x", padx=PAD, pady=(8, PAD))

        def button(text, command, primary=False):
            b = tk.Label(
                bar,
                text=text,
                bg=theme.ACCENT if primary else theme.BG_CARD,
                fg="#FFFFFF" if primary else theme.FG_DIM,
                font=self.f_small,
                padx=10,
                pady=5,
                cursor="hand2",
            )
            b.pack(side="left", padx=(0, 6))
            b.bind("<Button-1>", lambda _e: command())
            hover = theme.BG_ROW if not primary else theme.ACCENT
            b.bind("<Enter>", lambda _e: b.configure(bg=hover, fg=theme.FG if not primary else "#FFF"))
            b.bind(
                "<Leave>",
                lambda _e: b.configure(
                    bg=theme.ACCENT if primary else theme.BG_CARD,
                    fg="#FFFFFF" if primary else theme.FG_DIM,
                ),
            )
            return b

        button("Refresh", self.cb["refresh"], primary=True)
        if self.mode == "diagnostics":
            button("Back", lambda: self.show("usage"))
            if "open_diagnostics" in self.cb:
                button("Open as text", self.cb["open_diagnostics"])
        else:
            button("Diagnostics", lambda: self.show("diagnostics"))
        button("Config", self.cb["open_config"])
        button("Quit", self.cb["quit"])

        tk.Label(
            bar,
            text=self._meta.get("footer", ""),
            bg=theme.BG,
            fg=theme.FG_FAINT,
            font=self.f_small,
        ).pack(side="right", pady=(6, 0))

    def _place(self, win) -> None:
        win.update_idletasks()
        w = max(WIDTH, win.winfo_reqwidth())
        h = win.winfo_reqheight()
        sw = win.winfo_screenwidth()
        sh = win.winfo_screenheight()
        taskbar = 56
        h = min(h, sh - taskbar - 40)
        x = sw - w - 14
        y = sh - taskbar - h - 10
        win.geometry(f"{w}x{h}+{max(8, x)}+{max(8, y)}")
