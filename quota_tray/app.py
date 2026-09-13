"""Main program: tray icon, background refresh loop, panel orchestration."""
from __future__ import annotations

import logging
import os
import queue
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from . import APP_NAME, __version__
from .config import (
    CONFIG_PATH,
    Config,
    DIAG_PATH,
    LOG_PATH,
    acquire_single_instance,
    load_cache,
    save_cache,
    setup_logging,
)
from .model import ProviderResult, humanize_age, humanize_delta, now_utc
from .providers import build_providers
from .ui import icon as icon_render
from .win import autostart

log = logging.getLogger(__name__)


def _require_tk():
    """tkinter ships with Python but some trimmed installs omit it."""
    try:
        import tkinter as tk
    except ImportError as exc:                                  # pragma: no cover
        raise SystemExit(
            "tkinter is missing. Reinstall Python with the "
            '"tcl/tk and IDLE" option enabled, e.g. '
            "winget install Python.Python.3.12"
        ) from exc
    return tk


class QuotaTrayApp:
    def __init__(self) -> None:
        tk = _require_tk()
        from .ui.panel import Panel

        self.config = Config.load()
        self.providers = build_providers(self.config)
        self.results: list[ProviderResult] = self._load_cached()
        self.last_refresh: datetime | None = None
        self.refreshing = False
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._ui_queue: "queue.Queue" = queue.Queue()

        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title(APP_NAME)
        self.panel = Panel(
            self.root,
            {
                "refresh": self.request_refresh,
                "quit": self.quit,
                "open_config": lambda: autostart.open_folder(CONFIG_PATH()),
                "diagnostics_text": self.diagnostics_text,
            },
        )
        self.icon = self._build_icon()

    # ------------------------------------------------------------ threading

    def _post(self, fn) -> None:
        """Schedule a callback onto the Tk main thread from any thread.

        Tkinter is not thread-safe, and calling .after() across threads can
        crash in edge cases, so everything goes through this queue which the
        main thread drains in _pump.
        """
        self._ui_queue.put(fn)

    def _pump(self) -> None:
        while True:
            try:
                fn = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except Exception:                                   # noqa: BLE001
                log.exception("UI callback failed")
        if not self._stop.is_set():
            self.root.after(120, self._pump)

    # ------------------------------------------------------------ cache

    def _load_cached(self) -> list[ProviderResult]:
        cache = load_cache()
        out = []
        for item in cache.get("results", []):
            try:
                out.append(ProviderResult.from_cache(item))
            except Exception:                                   # noqa: BLE001
                continue
        return out

    def _save_cached(self) -> None:
        save_cache(
            {
                "saved_at": now_utc().isoformat(),
                "results": [r.to_cache() for r in self.results],
            }
        )

    # ------------------------------------------------------------ tray

    def _build_icon(self):
        import pystray

        menu = pystray.Menu(
            pystray.MenuItem("Show quota panel", self._on_show, default=True),
            pystray.MenuItem("Refresh now", lambda *_a: self.request_refresh()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Run at login",
                self._on_toggle_autostart,
                checked=lambda _i: autostart.is_enabled(),
            ),
            pystray.MenuItem("Diagnostics", self._on_show_diag),
            pystray.MenuItem("Open config file", lambda *_a: autostart.open_folder(CONFIG_PATH())),
            pystray.MenuItem("Open log file", lambda *_a: autostart.open_folder(LOG_PATH())),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", lambda *_a: self.quit()),
        )
        return pystray.Icon(
            APP_NAME,
            icon=self._icon_image(),
            title=f"{APP_NAME} {__version__}",
            menu=menu,
        )

    def _icon_image(self):
        entries = [(r.provider_id, r.worst_percent if r.ok else None) for r in self.results]
        if not entries:
            entries = [(p.id, None) for p in self.providers]
        return icon_render.render(
            entries,
            warn=float(self.config.get("warn_percent", 75)),
            danger=float(self.config.get("danger_percent", 90)),
            style=str(self.config.get("icon_style", "bars")),
        )

    def _tooltip(self) -> str:
        bits = []
        for r in self.results:
            if not r.ok:
                bits.append(f"{r.name}: {r.status[:16]}")
                continue
            top = r.sorted_windows()[:2]
            bits.append(f"{r.name}: " + " / ".join(w.percent_text for w in top))
        if not bits:
            bits = ["loading..."]
        return icon_render.tooltip(bits)

    # ------------------------------------------------------------ menu callbacks

    def _on_show(self, *_args) -> None:
        self._post(lambda: self.panel.show("usage"))

    def _on_show_diag(self, *_args) -> None:
        self._post(lambda: self.panel.show("diagnostics"))

    def _on_toggle_autostart(self, *_args) -> None:
        if autostart.is_enabled():
            autostart.disable()
        else:
            autostart.enable()
        try:
            self.icon.update_menu()
        except Exception:                                       # noqa: BLE001
            pass

    # ------------------------------------------------------------ refresh

    def request_refresh(self) -> None:
        self._wake.set()

    def _refresh_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.refresh_once()
            except Exception:                                   # noqa: BLE001
                log.exception("refresh failed")
            self._wake.wait(self.config.refresh_seconds)
            self._wake.clear()

    def refresh_once(self) -> None:
        self.refreshing = True
        try:
            with ThreadPoolExecutor(max_workers=max(1, len(self.providers))) as pool:
                results = list(pool.map(_safe_fetch, self.providers))
        finally:
            self.refreshing = False

        hide = bool(self.config.get("hide_not_installed", True))
        visible = [r for r in results if r.installed or r.ok or not hide]
        self.results = visible or results
        self.last_refresh = now_utc()
        self._save_cached()
        self._write_diag_file()

        try:
            self.icon.icon = self._icon_image()
            self.icon.title = self._tooltip()
        except Exception:                                       # noqa: BLE001
            log.debug("failed to update the tray icon", exc_info=True)
        self._post(self._push_to_panel)

    def _push_to_panel(self) -> None:
        self.panel.set_data(self.results, self._meta())

    def _meta(self) -> dict:
        return {
            "warn": self.config.get("warn_percent", 75),
            "danger": self.config.get("danger_percent", 90),
            "subtitle": f"updated {humanize_age(self.last_refresh)}" if self.last_refresh else "",
            "footer": f"v{__version__} - every {self.config.refresh_seconds // 60} min",
        }

    # ------------------------------------------------------------ diagnostics

    def diagnostics_text(self) -> str:
        lines = [
            f"{APP_NAME} v{__version__}",
            f"Python {sys.version.split()[0]} on {sys.platform}",
            "",
        ]
        lines.append(f"Config file: {CONFIG_PATH()}")
        lines.append(f"Log file:    {LOG_PATH()}")
        lines.append(f"Run at login: {'enabled' if autostart.is_enabled() else 'disabled'}")
        lines.append(f"Launch cmd:   {autostart.launch_command()}")
        lines.append("")
        if not self.results:
            lines.append("(no results collected yet)")
        for r in self.results:
            lines.append(f"-- {r.name} --")
            lines.append(f"   installed: {'yes' if r.installed else 'no'}")
            lines.append(f"   status:    {r.status or '-'}")
            lines.append(f"   source:    {r.source or 'none'}")
            if r.data_time:
                lines.append(f"   data time: {r.data_time.astimezone().strftime('%Y-%m-%d %H:%M')}")
            for attempt in r.attempts:
                mark = "OK  " if attempt.ok else "FAIL"
                lines.append(f"   [{mark}] {attempt.name}: {attempt.detail}")
            for w in r.sorted_windows():
                reset = humanize_delta(w.resets_at)
                lines.append(
                    f"   * {w.label}: {w.percent_text}" + (f"  (resets in {reset})" if reset else "")
                )
            lines.append("")
        return "\n".join(lines)

    def _write_diag_file(self) -> None:
        try:
            DIAG_PATH().write_text(self.diagnostics_text(), encoding="utf-8")
        except OSError:
            pass

    # ------------------------------------------------------------ start / stop

    def start(self) -> None:
        self.root.after(120, self._pump)
        threading.Thread(target=self._refresh_loop, name="refresh", daemon=True).start()
        threading.Thread(target=self.icon.run, name="tray", daemon=True).start()
        self.root.after(200, self._push_to_panel)
        self.root.mainloop()

    def quit(self, *_args) -> None:
        self._stop.set()
        self._wake.set()
        try:
            self.icon.stop()
        except Exception:                                       # noqa: BLE001
            pass
        try:
            self.panel.destroy()
        except Exception:                                       # noqa: BLE001
            pass
        self._post(self.root.quit)


def _safe_fetch(provider) -> ProviderResult:
    try:
        return provider.fetch()
    except Exception as exc:                                    # noqa: BLE001
        log.exception("%s fetch crashed", provider.id)
        r = ProviderResult(provider_id=provider.id, name=provider.name, fetched_at=now_utc())
        r.status = f"internal error: {exc}"
        return r


# ---------------------------------------------------------------- entry points


def _emit(lines: list[str]) -> None:
    """Print if there is a console, otherwise save the report and open it.

    A --noconsole PyInstaller build has sys.stdout set to None, so a bare
    print() raises. Someone double-clicking QuotaTray.exe --diagnose should
    still get their report rather than a silent crash.
    """
    text = "\n".join(lines)
    try:
        if sys.stdout is not None:
            print(text)
            return
    except Exception:                                           # noqa: BLE001
        pass

    path = DIAG_PATH()
    try:
        path.write_text(text, encoding="utf-8")
    except OSError:
        return
    # QUOTATRAY_NO_OPEN lets the CI smoke test run the windowed build without
    # spawning Notepad on the runner.
    if not os.environ.get("QUOTATRAY_NO_OPEN"):
        autostart.open_folder(path)


def run_console(diagnose: bool = False) -> int:
    """`--once` / `--diagnose`: skip the tray, report to the console or a file."""
    setup_logging(verbose=True)
    config = Config.load()
    providers = build_providers(config)
    with ThreadPoolExecutor(max_workers=max(1, len(providers))) as pool:
        results = list(pool.map(_safe_fetch, providers))

    lines: list[str] = []
    for r in results:
        head = f"{r.name}"
        if r.plan:
            head += f" [{r.plan}]"
        lines.append("")
        lines.append(f"=== {head} ===")
        lines.append(f"  status: {r.status or ('connected' if r.ok else 'unavailable')}")
        if r.source:
            lines.append(f"  source: {r.source}")
        for w in r.sorted_windows():
            reset = humanize_delta(w.resets_at)
            extra = f"  {w.detail}" if w.detail else ""
            lines.append(
                f"  {w.label:<18} {w.percent_text:>6}{extra}"
                + (f"   resets in {reset}" if reset else "")
            )
        if diagnose:
            for a in r.attempts:
                lines.append(f"    [{'OK' if a.ok else 'FAIL'}] {a.name}: {a.detail}")
    lines.append("")
    _emit(lines)
    return 0


def run_panel_demo() -> int:
    """Draw the panel once with fake data, to confirm the UI works on a machine."""
    from datetime import timedelta

    from .model import QuotaWindow

    setup_logging(verbose=True)
    app = QuotaTrayApp()
    t = now_utc()
    r1 = ProviderResult(
        "claude", "Claude", ok=True, status="connected", source="Claude Code OAuth",
        plan="max", account="you@example.com", fetched_at=t, data_time=t,
    )
    r1.windows = [
        QuotaWindow("five_hour", "5-hour window", 42.0, t + timedelta(hours=2, minutes=40), order=10),
        QuotaWindow("seven_day", "7-day window", 78.5, t + timedelta(days=3), order=20),
        QuotaWindow("seven_day_opus", "7-day Opus", 93.0, t + timedelta(days=3), order=30),
    ]
    r2 = ProviderResult(
        "codex", "Codex", ok=True, status="connected (offline snapshot)",
        source="session log rollout-2026-09-13.jsonl", plan="Plus",
        fetched_at=t, data_time=t - timedelta(hours=2),
    )
    r2.windows = [
        QuotaWindow("primary", "5-hour window", 12.0, t + timedelta(hours=4), order=10),
        QuotaWindow("secondary", "7-day window", 22.0, t + timedelta(days=4), order=20),
    ]
    r3 = ProviderResult(
        "antigravity", "Antigravity", ok=False,
        status="IDE not running or not signed in", fetched_at=t,
    )

    app.results = [r1, r2, r3]
    app.last_refresh = t
    app._post(lambda: app.panel.show("usage"))
    app.root.after(120, app._pump)
    app.root.mainloop()
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--once" in argv or "--diagnose" in argv:
        return run_console(diagnose="--diagnose" in argv)
    if "--panel-demo" in argv:
        return run_panel_demo()

    setup_logging(verbose="--verbose" in argv)
    if not acquire_single_instance():
        log.info("another instance is already running, exiting")
        return 0
    log.info("%s v%s starting", APP_NAME, __version__)
    app = QuotaTrayApp()
    # Enable run-at-login on the first run only; after that the tray menu wins.
    if "--no-autostart" not in argv and not app.config.get("autostart_initialized"):
        autostart.enable()
        app.config.data["autostart_initialized"] = True
        app.config.save()
    app.start()
    return 0
