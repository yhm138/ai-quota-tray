"""Main program: tray icon, background refresh loop, panel orchestration."""
from __future__ import annotations

import logging
import os
import queue
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from . import APP_NAME, __version__, reminders, updater
from .config import (
    CONFIG_PATH,
    Config,
    DIAG_PATH,
    LOG_PATH,
    acknowledge_show,
    acquire_single_instance,
    install_dir,
    listen_for_show,
    signal_running_instance,
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
        self.update: updater.Release | None = None
        self._updating = False
        self._notified_tag: str | None = None
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
                "open_diagnostics": self._open_diagnostics,
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
                lambda _i: f"Update to {self.update.tag} and restart" if self.update else "Update",
                self._on_apply_update,
                visible=lambda _i: self.update is not None,
            ),
            pystray.MenuItem("Check for updates", self._on_check_update),
            pystray.MenuItem(
                "Remind me about unused resets",
                self._on_toggle_reminders,
                checked=lambda _i: bool(self.config.get("remind_unused_resets", True)),
            ),
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
        # The bars are for usage limits; a prepaid balance has no percentage.
        entries = [(r.provider_id, r.worst_percent if r.ok else None) for r in self.results
                   if r.billing != "payg"]
        if not entries:
            entries = [(p.id, None) for p in self.providers if p.billing != "payg"]
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
            if not top and r.headline:
                bits.append(f"{r.name}: {r.headline}")
                continue
            bits.append(f"{r.name}: " + " / ".join(w.percent_text for w in top))
        if not bits:
            bits = ["loading..."]
        return icon_render.tooltip(bits)

    # ------------------------------------------------------------ menu callbacks

    def _on_show(self, *_args) -> None:
        self._post(lambda: self.panel.show("usage"))

    def _on_show_request(self) -> None:
        """Another launch asked for the panel: show it, then answer, so that
        launch knows this copy is alive (and does not replace it)."""
        def show():
            self.panel.show("usage")
            acknowledge_show()

        self._post(show)

    def _on_show_diag(self, *_args) -> None:
        self._post(lambda: self.panel.show("diagnostics"))

    def _on_toggle_reminders(self, *_args) -> None:
        on = not self.config.get("remind_unused_resets", True)
        self.config.data["remind_unused_resets"] = on
        self.config.save()
        try:
            self.icon.update_menu()
        except Exception:                                       # noqa: BLE001
            pass

    def _maybe_remind_resets(self) -> None:
        if not self.config.get("remind_unused_resets", True):
            return
        now_local = datetime.now()
        try:
            hour = int(self.config.get("reset_reminder_hour", 10))
        except (TypeError, ValueError):
            hour = 10
        if not reminders.due(self.config.get("last_reset_reminder"), now_local, hour):
            return
        text = reminders.unused_resets_text(self.results)
        if not text:
            return
        log.info("reminding about unused resets: %s", text.replace("\n", " | "))
        self._notify(text)
        self.config.data["last_reset_reminder"] = now_local.date().isoformat()
        self.config.save()

    def _on_toggle_autostart(self, *_args) -> None:
        if autostart.is_enabled():
            autostart.disable()
        else:
            autostart.enable()
        try:
            self.icon.update_menu()
        except Exception:                                       # noqa: BLE001
            pass

    # ------------------------------------------------------------ updates

    def _update_repo(self) -> str:
        return str(self.config.get("update_repo") or updater.DEFAULT_REPO)

    def _check_update(self, *, manual: bool) -> None:
        latest, error = updater.fetch_latest(self._update_repo())
        found = latest if latest and updater.is_newer(latest.tag) else None
        if found:
            self.update = found
        try:
            self.icon.update_menu()
        except Exception:                                       # noqa: BLE001
            pass
        self._post(self._push_to_panel)
        log.info("update check: %s", found.tag if found else (error or "up to date"))

        # The panel, not a toast, carries the answer: Windows 11 often
        # swallows tray notifications, which made the menu item look dead.
        if found:
            self._post(lambda: self.panel.set_notice(
                f"{APP_NAME} {found.tag} is available (you have v{__version__}).",
                "good", "Update now", self._on_apply_update))
            if manual:
                self._post(lambda: self.panel.show("usage"))
            elif self._notified_tag != found.tag:
                self._notified_tag = found.tag
                self._notify(f"{APP_NAME} {found.tag} is available. "
                             "Right-click the tray icon and choose \"Update\".")
        elif manual and error:
            self._post(lambda: self.panel.set_notice(
                f"Could not check for updates: {error}", "warn"))
        elif manual:
            self._post(lambda: self.panel.set_notice(
                f"You are on the latest version (v{__version__}).", "good"))
            self._post(lambda: self.root.after(8000, self._clear_up_to_date_notice))

    def _clear_up_to_date_notice(self) -> None:
        notice = self.panel._notice
        if notice and notice[0].startswith("You are on the latest version"):
            self.panel.set_notice(None)

    def _notify(self, text: str) -> None:
        try:
            self.icon.notify(text, APP_NAME)
        except Exception:                                       # noqa: BLE001
            log.debug("tray notification failed", exc_info=True)

    def _update_loop(self) -> None:
        # Give the first quota refresh a head start, then check once a day.
        if self._stop.wait(90):
            return
        while not self._stop.is_set():
            if self.config.get("check_updates", True):
                try:
                    self._check_update(manual=False)
                except Exception:                               # noqa: BLE001
                    log.exception("update check failed")
            if self._stop.wait(24 * 3600):
                return

    def _on_check_update(self, *_args) -> None:
        def begin():
            self.panel.set_notice("Checking for updates...")
            self.panel.show("usage")

        self._post(begin)
        threading.Thread(
            target=lambda: self._check_update(manual=True), name="update-check", daemon=True
        ).start()

    def _on_apply_update(self, *_args) -> None:
        """Check, download, verify and install while this version keeps
        running and shows each step; only then start the new version and exit."""
        from . import selfupdate

        release = self.update
        if not release or self._updating:
            return
        self._updating = True
        last = [0.0]

        def progress(step: int, total: int, text: str, fraction) -> None:
            import time

            now = time.time()
            if fraction is not None and 0 < fraction < 1 and now - last[0] < 0.2:
                return                                   # throttle per-chunk updates
            last[0] = now
            # Overall bar: each step is an equal share, the download fills its own.
            overall = (step - 1 + (fraction if fraction is not None else 0.0)) / total
            line = f"Updating to {release.tag} - step {step}/{total}: {text}"
            self._post(lambda: self.panel.update_progress(line, overall))

        def run():
            try:
                plan = selfupdate.install(release.tag, self._update_repo(), progress)
                progress(5, selfupdate.STEPS, plan.note, 1.0)
                log.info("update to %s installed; starting it", release.tag)
                plan.launch()
            except Exception as exc:                            # noqa: BLE001
                log.exception("update failed")
                why = str(exc) or exc.__class__.__name__
                self._updating = False
                self._post(lambda: self.panel.set_notice(
                    f"Update failed, still running v{__version__}: {why}\n"
                    f"Details: {LOG_PATH()}",
                    "warn", "Retry", self._on_apply_update,
                    more=[("Open log", lambda: autostart.open_folder(LOG_PATH()))]))
                return
            self._post(lambda: self.root.after(600, self.quit))

        self._post(lambda: self.panel.show("usage"))
        threading.Thread(target=run, name="update-apply", daemon=True).start()

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

    def _restart_if_bundle_damaged(self) -> bool:
        """Windows temp cleanup can delete the one-file build's unpacked
        files while it runs; a fresh start unpacks them again."""
        from .tls import bundle_damaged

        if not bundle_damaged():
            return False
        log.warning("the unpacked program files in %s were removed (Windows temp cleanup?); "
                    "restarting to restore them", getattr(sys, "_MEIPASS", "?"))
        try:
            import subprocess

            from .win.proc import independent_env

            subprocess.Popen(
                [sys.executable, "--autostart", "--wait-pid", str(os.getpid()), "--fresh"],
                creationflags=0x00000008 | 0x00000200,          # DETACHED | NEW_GROUP
                close_fds=True,
                env=independent_env(),      # unpack afresh, not into our folder
            )
        except Exception:                                       # noqa: BLE001
            log.exception("could not restart")
            return False
        self.quit()
        return True

    def refresh_once(self) -> None:
        if self._restart_if_bundle_damaged():
            return
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
            self._maybe_remind_resets()
        except Exception:                                       # noqa: BLE001
            log.exception("reset reminder failed")

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
            "footer": f"v{__version__} - every {self.config.refresh_seconds // 60} min"
            + (f" - {self.update.tag} available" if self.update else ""),
        }

    # ------------------------------------------------------------ diagnostics

    def diagnostics_text(self) -> str:
        lines = [f"{APP_NAME} v{__version__}", ""]
        # What is wrong, in plain words, before the details.
        lines.append("SUMMARY")
        if not self.results:
            lines.append("  (still collecting, try again in a moment)")
        for r in self.results:
            if r.ok:
                lines.append(f"  {r.name}: OK via {r.source or '?'}")
                continue
            lines.append(f"  {r.name}: NOT WORKING")
            for attempt in r.attempts:
                if not attempt.ok:
                    lines.append(f"    - {attempt.name}: {attempt.detail[:220]}")
        lines += ["", "DETAILS", f"Python {sys.version.split()[0]} on {sys.platform}"]
        lines.append(f"Config file: {CONFIG_PATH()}")
        lines.append(f"Log file:    {LOG_PATH()}")
        lines.append(f"Run at login: {'enabled' if autostart.is_enabled() else 'disabled'}")
        try:
            from .win import trayicon

            present = trayicon.icon_present(getattr(self.icon, "_hwnd", None))
        except Exception:                                       # noqa: BLE001
            present = None
        lines.append("Tray icon:    " + {True: "shown", False: "MISSING (being re-added)",
                                         None: "unknown"}[present])
        lines.append(f"Launch cmd:   {autostart.launch_command()}")
        lines.append("")
        if not self.results:
            lines.append("(no results collected yet)")
        for r in self.results:
            lines.append(f"-- {r.name} --" + (f" [{r.label}]" if r.alternates and r.label else ""))
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
            for row in r.info:
                lines.append(f"   {row.label or ' '}: {row.value}")
            for g in r.resets:
                exp = g.expires_at.astimezone().strftime("%Y-%m-%d %H:%M") if g.expires_at else "?"
                lines.append(f"   reset x{g.count}, expires {exp}" + (f" ({g.note})" if g.note else ""))
            for n, alt in enumerate(r.alternates, start=2):
                lines.append(f"   == account page {n}: {alt.label or '?'} ({alt.account or 'unknown'}) ==")
                lines.append(f"   source:    {alt.source or 'none'}")
                for w in alt.sorted_windows():
                    reset = humanize_delta(w.resets_at)
                    lines.append(f"   * {w.label}: {w.percent_text}"
                                 + (f"  (resets in {reset})" if reset else ""))
                for row in alt.info:
                    lines.append(f"   {row.label or ' '}: {row.value}")
                for g in alt.resets:
                    exp = g.expires_at.astimezone().strftime("%Y-%m-%d %H:%M") if g.expires_at else "?"
                    lines.append(f"   reset x{g.count}, expires {exp}")
            lines.append("")
        return "\n".join(lines)

    def _open_diagnostics(self) -> None:
        self._write_diag_file()
        autostart.open_folder(DIAG_PATH())

    def _write_diag_file(self) -> None:
        try:
            DIAG_PATH().write_text(self.diagnostics_text(), encoding="utf-8")
        except OSError:
            pass

    # ------------------------------------------------------------ start / stop

    def start(self) -> None:
        self.root.after(120, self._pump)
        threading.Thread(target=self._refresh_loop, name="refresh", daemon=True).start()
        threading.Thread(target=self._run_tray, name="tray", daemon=True).start()
        threading.Thread(target=self._update_loop, name="update", daemon=True).start()
        threading.Thread(
            target=listen_for_show, args=(self._on_show_request, self._stop), name="show", daemon=True
        ).start()
        self.root.after(200, self._push_to_panel)
        self.root.mainloop()

    def _run_tray(self) -> None:
        # If the tray loop dies, the process must not linger icon-less while
        # holding the single-instance lock: log it and exit the whole app.
        try:
            self.icon.run(setup=self._tray_setup)
        except Exception:                                       # noqa: BLE001
            log.exception("the tray icon stopped unexpectedly")
        if not self._stop.is_set():
            log.error("tray icon is gone; quitting so a new launch can start cleanly")
            self.quit()

    def _tray_setup(self, icon) -> None:
        icon.visible = True
        threading.Thread(target=self._tray_watchdog, name="tray-watchdog", daemon=True).start()

    def _tray_watchdog(self) -> None:
        """pystray adds the icon once and never checks. Make sure it is there
        (re-add it when Explorer lost or refused it) and, on Windows 11, that
        it is not tucked away under the ^ overflow."""
        from .win import trayicon

        exe = sys.executable if getattr(sys, "frozen", False) else None
        promote = bool(self.config.get("promote_tray_icon", True)) and exe
        checks = 0
        while not self._stop.wait(3 if checks < 20 else 30):
            checks += 1
            hwnd = getattr(self.icon, "_hwnd", None)
            present = trayicon.icon_present(hwnd)
            if present is False:
                log.warning("the tray icon is missing (check %d); adding it again", checks)
                try:
                    self.icon._show()                           # NIM_ADD
                    self.icon.icon = self._icon_image()
                except Exception:                               # noqa: BLE001
                    log.debug("re-adding the tray icon failed", exc_info=True)
            elif present and checks <= 3:
                log.info("tray icon is in place")
            if promote and present and checks <= 40:
                # Explorer writes the per-icon entry a moment after the add.
                try:
                    how = trayicon.promote(exe)
                except Exception as exc:                        # noqa: BLE001
                    how = f"failed: {exc!r}"
                if how != "no entry yet":
                    log.info("tray icon visibility: %s", how)
                    promote = False

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
    from .tls import ca_bundle

    ca_bundle()
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
        for row in r.info if r.billing == "payg" else []:
            lines.append(f"  {row.label or ' ':<18} {row.value}")
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


def run_update() -> int:
    """`--update`: install the latest release over this copy, then restart it."""
    setup_logging(verbose=True)
    repo = str(Config.load().get("update_repo") or updater.DEFAULT_REPO)
    rel = updater.check(repo)
    if not rel:
        _emit([f"{APP_NAME} v{__version__} is already the latest version."])
        return 0
    try:
        updater.launch(rel, repo)
    except Exception as exc:                                    # noqa: BLE001
        _emit([f"Could not start the update: {exc}"])
        return 1
    _emit([f"Updating {APP_NAME} to {rel.tag}; it restarts by itself when done."])
    return 0


def _retire_other_copies(include_own_dir: bool = False) -> int:
    """Stop QuotaTray copies that run from another folder (an old install),
    or from anywhere with include_own_dir. Returns how many were stopped."""
    from .win import instances

    stopped = 0
    for pid, folder in instances.other_copies(install_dir(), include_own_dir=include_own_dir):
        ok, how = instances.stop(pid)
        log.info("old copy in %s (pid %s): %s", folder, pid, how)
        stopped += ok
    return stopped


def _take_over(include_own_dir: bool = False) -> bool:
    """A v1.1.3+ copy holds the lock. If it runs from a different folder
    (or does not respond), the user just started this one on purpose: stop
    that one and take over."""
    import time

    if not _retire_other_copies(include_own_dir):
        return False
    for _ in range(10):
        if acquire_single_instance():
            return True
        time.sleep(0.5)
    log.warning("the other copy did not exit in time")
    return False


def _message_box(text: str, *, error: bool = False) -> None:
    """A plain Windows message box: visible even when the tray never came up."""
    if sys.platform != "win32" or os.environ.get("QUOTATRAY_NO_OPEN"):
        # Also in unattended runs (the CI smoke test): a modal box would hang.
        if sys.stderr is not None:
            print(text, file=sys.stderr)
        return
    try:
        import ctypes

        flags = 0x10 if error else 0x40            # MB_ICONERROR / MB_ICONINFORMATION
        ctypes.windll.user32.MessageBoxW(None, text, APP_NAME, flags | 0x40000)  # topmost
    except Exception:                                           # noqa: BLE001
        pass


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        return _main(argv)
    except SystemExit:
        raise
    except BaseException:                                       # noqa: BLE001
        # A windowed build has no console: without this a crash is invisible.
        import traceback

        report = traceback.format_exc()
        log.critical("QuotaTray crashed:\n%s", report)
        crash = CONFIG_PATH().with_name("crash.log")
        try:
            with crash.open("a", encoding="utf-8") as fh:
                fh.write(f"--- {datetime.now().isoformat()} v{__version__}\n{report}\n")
        except OSError:
            pass
        _message_box(
            f"QuotaTray v{__version__} could not start.\n\n{report.strip().splitlines()[-1]}\n\n"
            f"Details: {crash}",
            error=True,
        )
        return 1


def _relaunch_clean(argv: list[str]) -> bool:
    """A copy started by an older QuotaTray (v1.3.3 and before, after an
    update or self-restart) inherited that copy's PyInstaller state and runs
    from its unpacked folder, which the old copy deletes as it exits. Start
    a clean copy right away, while the old one is still up, and step aside.
    Our own launches pass --fresh, so this happens once per old-version hop."""
    if not getattr(sys, "frozen", False) or "--wait-pid" not in argv or "--fresh" in argv:
        return False
    import subprocess

    from .win.proc import independent_env

    try:
        subprocess.Popen([sys.executable, *argv, "--fresh"], env=independent_env(),
                         creationflags=0x00000008 | 0x00000200, close_fds=True)
    except Exception:                                           # noqa: BLE001
        return False
    return True


def _main(argv: list[str]) -> int:
    if _relaunch_clean(argv):
        return 0
    if "--update" in argv:
        return run_update()
    if "--once" in argv or "--diagnose" in argv:
        return run_console(diagnose="--diagnose" in argv)
    if "--panel-demo" in argv:
        return run_panel_demo()
    if "--selftest" in argv:
        return run_selftest()

    setup_logging(verbose="--verbose" in argv)
    from .tls import ca_bundle

    ca_bundle()                                  # copy it out while it still exists
    if "--wait-pid" in argv:
        # A self-restart: let the old copy exit and free the lock first.
        from .win import instances

        try:
            instances.wait_for_exit(int(argv[argv.index("--wait-pid") + 1]))
        except (IndexError, ValueError):
            pass
    manual = "--autostart" not in argv
    log.info("launch: v%s from %s, args %s", __version__, install_dir(), argv or "none")
    if not acquire_single_instance():
        if "--no-takeover" in argv or not _take_over():
            answer = signal_running_instance(wait=8.0)
            if answer == "acked":
                log.info("already running: that copy opened its panel")
                return 0
            if not manual or "--no-takeover" in argv:
                log.info("already running (%s); leaving it be", answer)
                return 0
            # No answer: a hung copy (or one too old to answer) holds the
            # lock and, often, no tray icon. The user just started this
            # one, so replace it instead of quietly exiting.
            log.warning("the running copy did not answer (%s); replacing it", answer)
            if not _take_over(include_own_dir=True):
                log.info("could not replace the running copy")
                if manual:
                    _message_box(
                        "QuotaTray is already running but does not respond.\n\n"
                        "End QuotaTray.exe in Task Manager, then start it again."
                    )
                return 0
    log.info("%s v%s starting from %s", APP_NAME, __version__, install_dir())
    # Old installs (v1.1.2 and earlier) used another lock, so they cannot stop
    # this copy from starting; clear them out in the background anyway so
    # there is one icon and one poller.
    threading.Thread(target=_retire_other_copies, name="retire", daemon=True).start()
    app = QuotaTrayApp()
    if "--no-autostart" not in argv:
        if not app.config.get("autostart_initialized"):
            # Enable run-at-login on the first run only; after that the menu wins.
            autostart.enable()
            app.config.data["autostart_initialized"] = True
            app.config.save()
        elif autostart.is_enabled() and autostart.registered_command() != autostart.launch_command():
            # Run-at-login points at another copy (an older install, say):
            # the copy that is running now takes it over.
            log.info("run-at-login pointed at %s, moving it here", autostart.registered_command())
            autostart.enable()
    # Delete what an update left behind (the previous exe), on every start.
    from .selfupdate import cleanup_after_update

    threading.Thread(target=cleanup_after_update, name="update-cleanup", daemon=True).start()
    if "--updated-from" in argv:
        try:
            previous = argv[argv.index("--updated-from") + 1]
        except IndexError:
            previous = "?"
        log.info("updated from v%s to v%s", previous, __version__)
        app.panel.set_notice(f"Updated from v{previous} to v{__version__}.", "good")
    if manual:
        # Started by hand: show something, since Windows 11 hides new tray icons.
        app.root.after(1500, app._on_show)
    app.start()
    return 0


def run_selftest() -> int:
    """`--selftest`: prove the frozen build has what it needs; exit 1 if not.
    The build trims unused libraries, so this exercises what is kept."""
    failures = []

    def step(name, fn):
        try:
            fn()
        except Exception as exc:                                # noqa: BLE001
            failures.append(f"{name}: {exc!r}")

    for mod in ("pystray", "requests", "tkinter", "sqlite3", "ssl"):
        step(mod, lambda m=mod: __import__(m))

    def aes():
        from .win import aesgcm

        aesgcm.selftest()                    # Windows CNG (bcrypt.dll) on Windows

    def icon():
        import io

        img = icon_render.render([("claude", 42.0), ("codex", 91.0)])
        buf = io.BytesIO()
        img.save(buf, format="ICO")          # what pystray does with the tray icon
        if not buf.getvalue():
            raise RuntimeError("empty ICO")

    def https():
        import ssl

        from .tls import ca_bundle

        ssl.create_default_context(cafile=ca_bundle())

    step("AES-GCM", aes)
    step("tray icon", icon)
    step("HTTPS certificates", https)
    _emit(failures or ["selftest OK"])
    return 1 if failures else 0
