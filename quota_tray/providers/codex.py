"""Codex (OpenAI) quota collection.

Fallback chain (providers.codex.order):
  1. wham        -- access_token from ~/.codex/auth.json
                    -> chatgpt.com/backend-api/wham/usage (live)
  2. app_server  -- spawn `codex app-server`, JSON-RPC account/rateLimits/read
                    (most accurate, needs the codex executable on PATH)
  3. jsonl       -- scan ~/.codex/sessions/**/rollout-*.jsonl backwards for the
                    last rate_limits record (works offline, may be stale)
  4. sqlite      -- newer Codex builds keep sessions in sqlite; best-effort scan
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import threading
from datetime import timedelta
from pathlib import Path
from typing import Iterator

from ..model import (
    ProviderResult,
    QuotaWindow,
    SourceAttempt,
    apply_scale,
    decide_percent_scale,
    node_reset,
    node_used,
    now_utc,
    parse_time,
)
from ..win.proc import popen
from .base import Provider, session

log = logging.getLogger(__name__)

WHAM_URL = "https://chatgpt.com/backend-api/wham/usage"


def codex_home(settings: dict) -> Path:
    manual = (settings.get("codex_home") or "").strip()
    if manual:
        return Path(manual)
    env = os.environ.get("CODEX_HOME")
    if env:
        return Path(env)
    return Path.home() / ".codex"


def find_codex_exe() -> Path | None:
    for name in ("codex", "codex.cmd", "codex.exe", "codex.bat"):
        found = shutil.which(name)
        if found:
            return Path(found)
    candidates = [
        Path.home() / ".bun" / "bin" / "codex.exe",
        Path.home() / ".bun" / "bin" / "codex",
        Path.home() / ".local" / "bin" / "codex",
        Path.home() / ".codex" / "bin" / "codex.exe",
    ]
    for env in ("APPDATA", "LOCALAPPDATA", "ProgramFiles"):
        base = os.environ.get(env)
        if base:
            candidates.append(Path(base) / "npm" / "codex.cmd")
            candidates.append(Path(base) / "Programs" / "codex" / "codex.exe")
    return next((p for p in candidates if p.is_file()), None)


def _label_from_minutes(minutes, fallback: str) -> tuple[str, int]:
    try:
        m = float(minutes)
    except (TypeError, ValueError):
        return fallback, 0
    if m <= 0:
        return fallback, 0
    if m < 90:
        return f"{m:.0f}-minute window", 5
    if m < 1440:
        return f"{m / 60:.0f}-hour window", 10
    return f"{m / 1440:.0f}-day window", 20


# Vendors have shipped "primary", "primary_window", "primaryWindow" and
# "primary_rate_limit" for the same thing, so compare on a squashed form.
_KEY_ALIASES = {
    "primary": ("Short window", 10),
    "short": ("Short window", 10),
    "fivehour": ("5-hour window", 10),
    "5h": ("5-hour window", 10),
    "hourly": ("Hourly window", 10),
    "daily": ("Daily window", 15),
    "secondary": ("Long window", 20),
    "long": ("Long window", 20),
    "weekly": ("7-day window", 20),
    "sevenday": ("7-day window", 20),
    "monthly": ("Monthly window", 25),
    "tertiary": ("Extra window", 30),
}


def _squash(key: str) -> str:
    """primary_window / primaryWindow / Primary Rate Limit -> primary"""
    k = re.sub(r"[^a-z0-9]", "", str(key).lower())
    for noise in ("ratelimit", "window", "limit", "quota", "usage"):
        k = k.replace(noise, "")
    return k


def _label_for_key(key: str) -> tuple[str, int]:
    squashed = _squash(key)
    if squashed in _KEY_ALIASES:
        return _KEY_ALIASES[squashed]
    pretty = re.sub(r"[_\-]+", " ", str(key)).strip().title()
    return pretty or "Window", 50


def bucket_of(window: QuotaWindow) -> str:
    """Group windows so the same quota coming from two sources is deduplicated."""
    if window.order <= 15:
        return "short"
    if window.order <= 25:
        return "long"
    return _squash(window.key) or window.label.lower()


def _correct_by_reset(label: str, order: int, resets_at) -> tuple[str, int]:
    """Reclassify a window when its reset is further out than the label allows."""
    if resets_at is None or order > 15:
        return label, order
    hours = (resets_at - now_utc()).total_seconds() / 3600.0
    if hours <= 26:
        return label, order
    if hours <= 8 * 24:
        return "7-day window", 20
    return "Long window", 20


def _windows_from_rate_limits(node: dict, base) -> list[QuotaWindow]:
    """Turn a {primary: {...}, secondary: {...}} structure into quota windows.

    Sub-dict names vary between Codex versions and between the wham endpoint,
    app-server and the session logs, so keys are matched loosely and the real
    window length (window_minutes) wins over the key name when present.
    """
    out: list[QuotaWindow] = []
    if not isinstance(node, dict):
        return out

    entries: list[tuple[str, dict]] = []
    for key, sub in node.items():
        if isinstance(sub, dict) and node_used(sub) is not None:
            entries.append((str(key), sub))
    if not entries:
        # One more level down: some payloads wrap the windows in a container.
        for key, sub in node.items():
            if not isinstance(sub, dict):
                continue
            for inner_key, inner in sub.items():
                if isinstance(inner, dict) and node_used(inner) is not None:
                    entries.append((f"{key}_{inner_key}", inner))

    raw = [v for _, sub in entries if (v := node_used(sub)) is not None]
    scale = decide_percent_scale(raw)

    for key, sub in entries:
        used = node_used(sub)
        if used is None:
            continue
        fallback_label, key_order = _label_for_key(key)
        minutes = (
            sub.get("window_minutes")
            or sub.get("windowMinutes")
            or sub.get("window_size_minutes")
        )
        seconds = sub.get("window_seconds") or sub.get("windowSeconds") or sub.get(
            "window_size_seconds"
        )
        if minutes is None and isinstance(seconds, (int, float)) and seconds > 0:
            minutes = float(seconds) / 60.0
        label, minutes_order = _label_from_minutes(minutes, fallback_label)
        resets_at = node_reset(sub, base=base)
        order = minutes_order or key_order
        if not minutes:
            # No declared window length, so the key name is all we had to go on.
            # Time-to-reset is a hard lower bound though: you cannot have 5 days
            # left in a 5-hour window, so that beats a key called "primary".
            label, order = _correct_by_reset(label, order, resets_at)
        pct = apply_scale(used, scale)
        out.append(
            QuotaWindow(
                key=key,
                label=label,
                percent=pct,
                resets_at=resets_at,
                order=order,
                exhausted=bool(pct is not None and pct >= 99.9),
            )
        )
    return out


def _find_rate_limits(obj) -> dict | None:
    """Find a rate_limits / rateLimits node anywhere in a nested structure."""
    if isinstance(obj, dict):
        for key in ("rate_limits", "rateLimits", "rate_limit", "rateLimit"):
            node = obj.get(key)
            if isinstance(node, dict) and node:
                return node
        for value in obj.values():
            found = _find_rate_limits(value)
            if found:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = _find_rate_limits(value)
            if found:
                return found
    return None


def iter_lines_reverse(
    path: Path,
    max_bytes: int = 4_000_000,
    max_line: int = 512_000,
    chunk: int = 256 * 1024,
) -> Iterator[str]:
    """Yield lines from the end of a file, reading at most max_bytes.

    Reads backwards in small blocks so memory stays bounded, and skips lines
    longer than max_line: Codex logs can hold multi-megabyte lines (pasted
    images, tool output), while the rate-limit records we want are tiny.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return
    floor = max(0, size - max_bytes)
    try:
        fh = path.open("rb")
    except OSError:
        return
    with fh:
        pos = size
        tail = b""          # start of the line that continues into later blocks
        skipping = False    # the line being assembled is over max_line: drop it
        while pos > floor:
            step = min(chunk, pos - floor)
            pos -= step
            try:
                fh.seek(pos)
                block = fh.read(step)
            except (OSError, MemoryError):
                return
            parts = (block + tail).split(b"\n")
            tail = parts.pop(0)
            if parts and skipping:
                parts.pop()             # the rest of the oversized line
                skipping = False
            for line in reversed(parts):
                if line.strip() and len(line) <= max_line:
                    yield line.decode("utf-8", "replace")
            if len(tail) > max_line:
                tail, skipping = b"", True
        # Only a line that starts at the beginning of the file is complete.
        if pos == 0 and tail.strip() and not skipping:
            yield tail.decode("utf-8", "replace")


class CodexProvider(Provider):
    id = "codex"
    name = "Codex"

    def detect(self) -> bool:
        return codex_home(self.settings).is_dir() or find_codex_exe() is not None

    # ---------------- 1. wham

    def _auth_tokens(self) -> tuple[dict | None, str]:
        home = codex_home(self.settings)
        candidates = [home / "auth.json"]
        secrets = home / "secrets"
        if secrets.is_dir():
            try:
                candidates.extend(sorted(secrets.glob("*.json"))[:5])
            except OSError:
                pass
        for path in candidates:
            try:
                if not path.is_file():
                    continue
                data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
            except (OSError, json.JSONDecodeError):
                continue
            tokens = data.get("tokens") if isinstance(data, dict) else None
            if isinstance(tokens, dict) and tokens.get("access_token"):
                return tokens, str(path)
        return None, ""

    def _try_wham(self, result: ProviderResult) -> bool:
        tokens, origin = self._auth_tokens()
        if not tokens:
            result.attempts.append(
                SourceAttempt(
                    "ChatGPT usage API",
                    False,
                    f"no access_token in {codex_home(self.settings)}/auth.json",
                )
            )
            return False
        headers = {
            "Authorization": f"Bearer {tokens['access_token']}",
            "Accept": "application/json",
            "User-Agent": "codex_cli_rs/0.0.0 (quota-tray)",
        }
        account_id = tokens.get("account_id")
        if account_id:
            headers["chatgpt-account-id"] = str(account_id)
        try:
            resp = session().get(WHAM_URL, headers=headers, timeout=20)
        except Exception as exc:                                # noqa: BLE001
            result.attempts.append(SourceAttempt("ChatGPT usage API", False, f"request failed: {exc}"))
            return False
        if resp.status_code >= 400:
            result.attempts.append(
                SourceAttempt(
                    "ChatGPT usage API",
                    False,
                    f"HTTP {resp.status_code} (token may be expired; run codex once to refresh)",
                )
            )
            return False
        try:
            payload = resp.json()
        except ValueError:
            result.attempts.append(SourceAttempt("ChatGPT usage API", False, "response was not JSON"))
            return False

        node = _find_rate_limits(payload)
        windows = _windows_from_rate_limits(node or {}, now_utc())
        if not windows:
            result.attempts.append(
                SourceAttempt("ChatGPT usage API", False, f"no rate limits in response: {str(payload)[:160]}")
            )
            return False
        result.windows = windows
        result.ok = True
        result.source = "chatgpt.com/wham/usage"
        result.plan = _plan_of(payload)
        result.status = "connected"
        result.data_time = now_utc()
        result.attempts.append(SourceAttempt("ChatGPT usage API", True, origin))
        return True

    # ---------------- 2. app-server

    def _try_app_server(self, result: ProviderResult) -> bool:
        exe = find_codex_exe()
        if not exe:
            result.attempts.append(
                SourceAttempt("codex app-server", False, "no codex executable on PATH")
            )
            return False
        timeout = float(self.settings.get("app_server_timeout", 20))
        try:
            payload = _app_server_rate_limits(exe, timeout)
        except Exception as exc:                                # noqa: BLE001
            result.attempts.append(SourceAttempt("codex app-server", False, str(exc)))
            return False
        node = _find_rate_limits(payload) or payload
        windows = _windows_from_rate_limits(node if isinstance(node, dict) else {}, now_utc())
        if not windows:
            result.attempts.append(
                SourceAttempt("codex app-server", False, f"no quota parsed: {str(payload)[:160]}")
            )
            return False
        result.windows = windows
        result.ok = True
        result.source = f"codex app-server ({exe.name})"
        result.status = "connected"
        result.data_time = now_utc()
        result.attempts.append(SourceAttempt("codex app-server", True, str(exe)))
        return True

    # ---------------- 3. jsonl

    def _try_jsonl(self, result: ProviderResult) -> bool:
        home = codex_home(self.settings)
        sessions_dir = home / "sessions"
        if not sessions_dir.is_dir():
            result.attempts.append(SourceAttempt("session logs", False, f"no {sessions_dir}"))
            return False
        try:
            files = sorted(
                (p for p in sessions_dir.rglob("*.jsonl") if p.is_file()),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )[:30]
        except OSError as exc:
            result.attempts.append(SourceAttempt("session logs", False, f"walk failed: {exc}"))
            return False
        if not files:
            result.attempts.append(SourceAttempt("session logs", False, "no rollout logs"))
            return False

        cutoff = now_utc() - timedelta(days=int(self.settings.get("jsonl_max_days", 14)))
        for path in files:
            for line in iter_lines_reverse(path):
                if "rate_limit" not in line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                node = _find_rate_limits(record)
                if not node:
                    continue
                stamp = parse_time(record.get("timestamp") or record.get("ts")) or parse_time(
                    path.stat().st_mtime
                )
                if stamp and stamp < cutoff:
                    continue
                windows = _windows_from_rate_limits(node, stamp or now_utc())
                if not windows:
                    continue
                result.windows = windows
                result.ok = True
                result.source = f"session log {path.name}"
                result.data_time = stamp
                result.status = "connected (offline snapshot)"
                result.attempts.append(SourceAttempt("session logs", True, str(path)))
                return True
        result.attempts.append(
            SourceAttempt("session logs", False, f"scanned {len(files)} logs, no rate_limits")
        )
        return False

    # ---------------- 4. sqlite

    def _try_sqlite(self, result: ProviderResult) -> bool:
        home = codex_home(self.settings)
        dbs: list[Path] = []
        for pattern in ("*.sqlite", "*.sqlite3", "*.db"):
            try:
                dbs.extend(p for p in home.rglob(pattern) if p.is_file())
            except OSError:
                continue
        if not dbs:
            result.attempts.append(SourceAttempt("local database", False, "no sqlite file found"))
            return False
        dbs.sort(key=lambda p: p.stat().st_mtime, reverse=True)

        for db in dbs[:4]:
            hit = _scan_sqlite_for_rate_limits(db)
            if not hit:
                continue
            node, stamp = hit
            windows = _windows_from_rate_limits(node, stamp or now_utc())
            if not windows:
                continue
            result.windows = windows
            result.ok = True
            result.source = f"local database {db.name}"
            result.data_time = stamp
            result.status = "connected (offline snapshot)"
            result.attempts.append(SourceAttempt("local database", True, str(db)))
            return True
        result.attempts.append(
            SourceAttempt("local database", False, f"scanned {len(dbs[:4])} DBs, no rate limits")
        )
        return False

    # ---------------- orchestration

    def collect(self, result: ProviderResult) -> None:
        """Walk the sources and MERGE their windows.

        The live wham endpoint sometimes reports only one of the two windows,
        while the session log holds the other. Rather than stopping at the first
        source that returns anything, keep going until both a short and a long
        window are covered, then stitch the pieces together.
        """
        steps = {
            "wham": self._try_wham,
            "app_server": self._try_app_server,
            "jsonl": self._try_jsonl,
            "sqlite": self._try_sqlite,
        }
        order = self.settings.get("order") or ["wham", "app_server", "jsonl", "sqlite"]

        merged: list[QuotaWindow] = []
        buckets: set[str] = set()
        sources: list[str] = []
        stale_source = False
        oldest_data = None

        for name in order:
            fn = steps.get(name)
            if not fn:
                continue
            probe = ProviderResult(provider_id=self.id, name=self.name, fetched_at=now_utc())
            # One broken source must not throw away what the others found.
            try:
                ok = fn(probe)
            except Exception as exc:                            # noqa: BLE001
                log.warning("codex source %s failed: %r", name, exc)
                probe.attempts.append(SourceAttempt(name, False, f"crashed: {exc!r}"))
                ok = False
            result.attempts.extend(probe.attempts)
            if not ok:
                continue

            added = False
            for window in probe.windows:
                key = bucket_of(window)
                if key in buckets:
                    continue
                buckets.add(key)
                merged.append(window)
                added = True
            if added:
                sources.append(probe.source or name)
                if probe.plan and not result.plan:
                    result.plan = probe.plan
                if "snapshot" in (probe.status or ""):
                    stale_source = True
                if probe.data_time and (oldest_data is None or probe.data_time < oldest_data):
                    oldest_data = probe.data_time

            if {"short", "long"} <= buckets:
                break

        if merged:
            result.windows = merged
            result.ok = True
            result.source = " + ".join(dict.fromkeys(sources))
            result.data_time = oldest_data
            result.status = "connected (partly from an offline snapshot)" if stale_source else "connected"
            return

        if not result.installed:
            result.status = "Codex not detected"
        else:
            result.status = "no usable quota source - see Diagnostics"


def _plan_of(payload) -> str | None:
    if isinstance(payload, dict):
        for key in ("plan", "plan_type", "planType", "subscription", "tier"):
            v = payload.get(key)
            if isinstance(v, str) and v:
                return v
            if isinstance(v, dict):
                for f in ("name", "type", "id"):
                    if isinstance(v.get(f), str):
                        return v[f]
    return None


def _app_server_rate_limits(exe: Path, timeout: float):
    """Spawn `codex app-server` and ask it for rate limits over JSON-RPC."""
    proc = popen([str(exe), "app-server"])
    result_holder: dict = {}
    error_holder: dict = {}

    def reader():
        try:
            assert proc.stdout is not None
            for raw in proc.stdout:
                try:
                    msg = json.loads(raw.decode("utf-8", "replace").strip())
                except (json.JSONDecodeError, AttributeError):
                    continue
                if not isinstance(msg, dict):
                    continue
                if msg.get("id") == 1:
                    if "error" in msg:
                        error_holder["e"] = str(msg["error"])[:200]
                    else:
                        result_holder["r"] = msg.get("result", {})
                    return
                if msg.get("id") == 0 and "result" in msg:
                    _send(proc, {"jsonrpc": "2.0", "method": "initialized", "params": {}})
                    _send(
                        proc,
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "account/rateLimits/read",
                            "params": {},
                        },
                    )
        except Exception:                                       # noqa: BLE001
            pass

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    _send(
        proc,
        {
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {
                "clientInfo": {"name": "quota-tray", "title": "QuotaTray", "version": "1.0.0"}
            },
        },
    )
    thread.join(timeout)
    try:
        proc.kill()
    except OSError:
        pass
    if "r" in result_holder:
        return result_holder["r"]
    if "e" in error_holder:
        raise RuntimeError(f"app-server error: {error_holder['e']}")
    raise TimeoutError(f"no response within {timeout:.0f}s (you may need to run `codex login`)")


def _send(proc, message: dict) -> None:
    try:
        assert proc.stdin is not None
        proc.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
        proc.stdin.flush()
    except (OSError, ValueError, AssertionError):
        pass


def _scan_sqlite_for_rate_limits(db: Path):
    """Best effort: copy the DB out and scan text columns for rate limit JSON."""
    tmp_dir = tempfile.mkdtemp(prefix="quotatray-codex-")
    tmp = Path(tmp_dir) / db.name
    try:
        try:
            shutil.copy2(db, tmp)
        except OSError:
            return None
        # Recent writes live in the -wal file; without it a copy of a busy DB
        # can look corrupt ("database disk image is malformed").
        for suffix in ("-wal", "-journal"):
            side = db.with_name(db.name + suffix)
            if side.exists():
                try:
                    shutil.copy2(side, tmp.with_name(tmp.name + suffix))
                except OSError:
                    pass
        try:
            conn = sqlite3.connect(str(tmp), timeout=5)
        except sqlite3.Error:
            return None
        try:
            tables = [
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            ]
            for table in tables[:40]:
                try:
                    rows = conn.execute(
                        f'SELECT * FROM "{table}" ORDER BY rowid DESC LIMIT 200'  # noqa: S608
                    ).fetchall()
                except sqlite3.Error:
                    continue
                for row in rows:
                    for cell in row:
                        if not isinstance(cell, (str, bytes)):
                            continue
                        text = cell if isinstance(cell, str) else cell.decode("utf-8", "ignore")
                        if "rate_limit" not in text and "rateLimit" not in text:
                            continue
                        try:
                            node = _find_rate_limits(json.loads(text))
                        except json.JSONDecodeError:
                            continue
                        if node:
                            return node, parse_time(db.stat().st_mtime)
        except sqlite3.Error as exc:
            log.info("skipping unreadable database %s: %s", db.name, exc)
            return None
        finally:
            conn.close()
        return None
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
