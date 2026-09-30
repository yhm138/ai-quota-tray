"""DeepSeek API balance (pay as you go).

DeepSeek bills API keys against a prepaid balance, so there are no usage
windows: the card shows what is left. The key is read from the tools that
already hold it, never typed into QuotaTray:

  1. OpenCode         ~/.local/share/opencode/auth.json ({"deepseek": {"type": "api",
                      "key": ...}}), and opencode.json provider options.apiKey
  2. DeepSeek Harness ~/.dsh/.credentials.yaml (refs.DEEPSEEK_API_KEY), then
                      ~/.dsh/.env; $DSH_HOME moves the folder
  3. the DEEPSEEK_API_KEY environment variable
  4. api_key pasted into config.json

Both Windows and each WSL distro are searched. Every distinct key gets its own
page; the same key found in two tools shares one. A key is only ever sent to
api.deepseek.com, and only when the tool uses it for DeepSeek's own API (not
a relay with its own base URL).
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

from ..model import InfoRow, ProviderResult, SourceAttempt, now_utc
from .account import money, to_float
from .base import Provider, session

log = logging.getLogger(__name__)

BALANCE_URL = "https://api.deepseek.com/user/balance"
ENV_NAME = "DEEPSEEK_API_KEY"


# ------------------------------------------------------------ file formats


def strip_jsonc(text: str) -> str:
    """JSON with // and /* */ comments and trailing commas (opencode.json) -> JSON."""
    out = []
    i, n = 0, len(text)
    in_str = False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
        elif text.startswith("//", i):
            while i < n and text[i] not in "\r\n":
                i += 1
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
        else:
            out.append(c)
            i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def _unquote(value: str) -> str:
    value = value.strip()
    if value[:1] in ("'", '"'):
        end = value.find(value[0], 1)
        return value[1:end] if end > 0 else value[1:]
    # An unquoted YAML / dotenv value ends at a comment.
    return re.split(r"\s+#", value, maxsplit=1)[0].strip()


def dsh_credentials_key(text: str) -> str | None:
    """refs.DEEPSEEK_API_KEY from a DeepSeek Harness .credentials.yaml, or the
    top-level entry of the pre-release flat layout. Only simple scalars are
    read; the file holds nothing else we need."""
    section = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        m = re.match(r"^\s*([A-Za-z0-9_./-]+)\s*:\s*(.*)$", line)
        if not m:
            continue
        key, value = m.group(1), m.group(2)
        if indent == 0:
            section = key if not value.strip() else None
            if key == ENV_NAME and value.strip():
                return _unquote(value) or None           # flat layout
            continue
        if section == "refs" and key == ENV_NAME and value.strip() not in ("", "|", ">"):
            return _unquote(value) or None
    return None


def dotenv_key(text: str, name: str = ENV_NAME) -> str | None:
    for line in text.splitlines():
        m = re.match(rf"^\s*(?:export\s+)?{re.escape(name)}\s*=\s*(.*)$", line)
        if m:
            return _unquote(m.group(1)) or None
    return None


def _is_official(base_url) -> bool:
    """No base URL (the built-in provider) or DeepSeek's own API host."""
    if not base_url:
        return True
    return bool(re.match(r"^https?://api\.deepseek\.com(?:[:/]|$)", str(base_url).strip(), re.I))


def opencode_auth_keys(data) -> list[str]:
    """API keys stored for DeepSeek providers in OpenCode's auth.json."""
    keys = []
    if isinstance(data, dict):
        for provider, entry in data.items():
            if "deepseek" not in str(provider).lower() or not isinstance(entry, dict):
                continue
            key = entry.get("key") if entry.get("type", "api") == "api" else None
            if isinstance(key, str) and key.strip():
                keys.append(key.strip())
    return keys


def opencode_config_keys(data, env=None) -> list[str]:
    """provider.<deepseek>.options.apiKey from opencode.json, resolving
    {env:NAME}. Providers pointed at a relay are skipped."""
    env = os.environ if env is None else env
    keys = []
    providers = data.get("provider") if isinstance(data, dict) else None
    if not isinstance(providers, dict):
        return keys
    for name, entry in providers.items():
        if not isinstance(entry, dict):
            continue
        options = entry.get("options") if isinstance(entry.get("options"), dict) else {}
        base = options.get("baseURL") or options.get("baseUrl")
        is_deepseek = "deepseek" in str(name).lower() or "deepseek.com" in str(base or "").lower()
        if not is_deepseek or not _is_official(base):
            continue
        key = options.get("apiKey")
        if not isinstance(key, str):
            continue
        m = re.fullmatch(r"\{env:([A-Za-z_][A-Za-z0-9_]*)\}", key.strip())
        if m:
            key = env.get(m.group(1), "")
        if key.strip() and not key.strip().startswith("{"):
            keys.append(key.strip())
    return keys


# ------------------------------------------------------------ discovery


def _homes(settings: dict) -> list[tuple[Path, str]]:
    """[(home folder, WSL distro or "")]: Windows, then each WSL user."""
    homes = [(Path.home(), "")]
    if settings.get("scan_wsl", True):
        from .claude import _wsl_distros

        for distro in _wsl_distros():
            root = Path(f"\\\\wsl.localhost\\{distro}")
            candidates = [root / "root"]
            try:
                if (root / "home").is_dir():
                    candidates += list((root / "home").iterdir())[:10]
            except OSError:
                pass
            homes += [(c, distro) for c in candidates]
    return homes


# ------------------------------------------------------------ OpenCode CLI vs Desktop

# OpenCode Desktop keeps its own settings in %APPDATA%\<app id>, but the
# OpenCode server it runs reads the same auth.json as the CLI (it only moves
# XDG_STATE_HOME). So on Windows the two share one key, and QuotaTray names
# every app that uses it. Desktop can also run servers inside WSL distros.
DESKTOP_APP_IDS = ("ai.opencode.desktop", "ai.opencode.desktop.beta")
_CLI_NAMES = ("opencode.exe", "opencode.cmd", "opencode")


def _which(names, env) -> Path | None:
    for folder in (env.get("PATH") or "").split(os.pathsep):
        folder = folder.strip().strip('"')
        if not folder:
            continue
        for name in names:
            try:
                if (Path(folder) / name).is_file():
                    return Path(folder) / name
            except OSError:
                continue
    return None


def opencode_cli(home: Path, env, local: bool) -> Path | None:
    """Where the OpenCode CLI is installed for this home, if anywhere."""
    if local:
        found = _which(_CLI_NAMES, env)
        if found:
            return found
    candidates = [home / ".opencode" / "bin" / n for n in _CLI_NAMES] + [
        home / ".bun" / "bin" / "opencode.exe", home / ".bun" / "bin" / "opencode",
        home / ".local" / "bin" / "opencode", home / ".npm-global" / "bin" / "opencode",
        home / "scoop" / "shims" / "opencode.exe",
    ]
    if local:
        for var, rel in (("APPDATA", ("npm", "opencode.cmd")),
                         ("LOCALAPPDATA", ("Microsoft", "WinGet", "Links", "opencode.exe"))):
            if env.get(var):
                candidates.append(Path(env[var], *rel))
    for path in candidates:
        try:
            if path.is_file():
                return path
        except OSError:
            continue
    return None


def opencode_desktop(env) -> tuple[Path | None, set[str]]:
    """(OpenCode Desktop's settings folder, WSL distros it runs servers in)."""
    appdata = env.get("APPDATA")
    if not appdata:
        return None, set()
    for app_id in DESKTOP_APP_IDS:
        folder = Path(appdata) / app_id
        try:
            if not folder.is_dir():
                continue
        except OSError:
            continue
        distros: set[str] = set()
        text = _read(folder / "opencode.settings")
        try:
            data = json.loads(text) if text else {}
            servers = (data.get("wslServers") or {}).get("servers") or []
            distros = {s["distro"] for s in servers if isinstance(s, dict) and isinstance(s.get("distro"), str)}
        except (ValueError, AttributeError, TypeError):
            pass
        return folder, distros
    return None, set()


def opencode_label(cli: bool, desktop: bool, distro: str = "", desktop_distros=()) -> str:
    """Which OpenCode apps use the auth.json of this home."""
    if not distro:
        names = (["OpenCode CLI"] if cli else []) + (["OpenCode Desktop"] if desktop else [])
        return " + ".join(names) or "OpenCode"
    names = (["OpenCode CLI"] if cli else []) + (["OpenCode Desktop"] if distro in desktop_distros else [])
    return f"{' + '.join(names) or 'OpenCode'} - WSL {distro}"


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8-sig", errors="replace") if path.is_file() else None
    except OSError:
        return None


def discover_keys(settings: dict, env=None) -> tuple[list[tuple[str, str]], list[str]]:
    """([(key, where)], notes), in priority order, duplicates kept (the
    caller merges them so a shared key lists every tool that holds it)."""
    env = os.environ if env is None else env
    found: list[tuple[str, str]] = []
    notes: list[str] = []

    manual = (settings.get("api_key") or "").strip()
    if manual:
        found.append((manual, "config.json"))

    desktop_dir, desktop_distros = opencode_desktop(env)
    for home, distro in _homes(settings):
        suffix = f" - WSL {distro}" if distro else ""
        if settings.get("scan_opencode", True):
            cli = opencode_cli(home, env, local=not distro)
            oc_label = opencode_label(cli is not None, desktop_dir is not None, distro, desktop_distros)
            data_dirs = [home / ".local" / "share" / "opencode"]
            if not suffix:
                if env.get("XDG_DATA_HOME"):
                    data_dirs.insert(0, Path(env["XDG_DATA_HOME"]) / "opencode")
                for var in ("LOCALAPPDATA", "APPDATA"):
                    if env.get(var):
                        data_dirs.append(Path(env[var]) / "opencode")
            auth_files = [d / "auth.json" for d in data_dirs]
            if not suffix and env.get("OPENCODE_AUTH_JSON"):
                auth_files.insert(0, Path(env["OPENCODE_AUTH_JSON"]))
            for path in auth_files:
                text = _read(path)
                if text is None:
                    continue
                try:
                    keys = opencode_auth_keys(json.loads(text))
                except ValueError:
                    notes.append(f"{path}: not valid JSON")
                    continue
                found += [(k, oc_label) for k in keys]
                if not keys:
                    notes.append(f"{path}: no DeepSeek key (add one with /connect in OpenCode)")
            config_dirs = [home / ".config" / "opencode"]
            if not suffix and env.get("XDG_CONFIG_HOME"):
                config_dirs.insert(0, Path(env["XDG_CONFIG_HOME"]) / "opencode")
            config_files = [d / n for d in config_dirs for n in ("opencode.json", "opencode.jsonc", "config.json")]
            if not suffix and env.get("OPENCODE_CONFIG"):
                config_files.insert(0, Path(env["OPENCODE_CONFIG"]))
            for path in config_files:
                text = _read(path)
                if text is None:
                    continue
                try:
                    keys = opencode_config_keys(json.loads(strip_jsonc(text)), env)
                except ValueError:
                    notes.append(f"{path}: could not be parsed")
                    continue
                found += [(k, oc_label) for k in keys]

        if settings.get("scan_dsh", True):
            dsh_home = Path(env["DSH_HOME"]) if (not suffix and (env.get("DSH_HOME") or "").strip()) \
                else home / ".dsh"
            text = _read(dsh_home / ".credentials.yaml")
            key = dsh_credentials_key(text) if text else None
            if key is None:
                env_text = _read(dsh_home / ".env")
                key = dotenv_key(env_text) if env_text else None
            if key:
                found.append((key, f"DeepSeek Harness{suffix}"))
            elif text is not None:
                notes.append(f"{dsh_home}: no {ENV_NAME} saved (Settings > Models in dsh)")

    env_key = (env.get(ENV_NAME) or "").strip()
    if env_key:
        found.append((env_key, f"env {ENV_NAME}"))
    if not found:
        notes.append("no DeepSeek API key in OpenCode, DeepSeek Harness or DEEPSEEK_API_KEY")
    return found, notes


def mask(key: str) -> str:
    return f"sk-...{key[-4:]}" if len(key) > 8 else "sk-..."


# ------------------------------------------------------------ parsing


def balance_rows(payload: dict, low: float) -> tuple[list[InfoRow], str | None, bool]:
    """(rows, headline, available) from /user/balance."""
    rows: list[InfoRow] = []
    headline = None
    available = payload.get("is_available")
    for info in payload.get("balance_infos") or []:
        if not isinstance(info, dict):
            continue
        cur = info.get("currency") or "CNY"
        total = to_float(info.get("total_balance"))
        if total is None:
            continue
        granted = to_float(info.get("granted_balance"))
        topped = to_float(info.get("topped_up_balance"))
        warn = available is False or total < low
        rows.append(InfoRow("Balance", money(total, cur), "warn" if warn else "good"))
        parts = []
        if topped is not None:
            parts.append(f"{money(topped, cur)} topped up")
        if granted:
            parts.append(f"{money(granted, cur)} granted (can expire)")
        if parts:
            rows.append(InfoRow("", " \u00b7 ".join(parts)))
        headline = headline or money(total, cur)
    if available is False:
        rows.append(InfoRow("Status", "balance too low for API calls; top up at platform.deepseek.com",
                            "warn"))
    return rows, headline, available is not False


class DeepSeekProvider(Provider):
    id = "deepseek"
    name = "DeepSeek"
    billing = "payg"

    def detect(self) -> bool:
        return bool(discover_keys(self.settings)[0])

    def _page(self, key: str, where: str, result: ProviderResult) -> ProviderResult:
        page = ProviderResult(provider_id=self.id, name=self.name, fetched_at=result.fetched_at,
                              billing=self.billing, label=where)
        tag = f"balance API ({where})"
        try:
            resp = session().get(BALANCE_URL, headers={
                "Authorization": f"Bearer {key}", "Accept": "application/json"}, timeout=20)
        except Exception as exc:                                # noqa: BLE001
            page.attempts.append(SourceAttempt(tag, False, f"request failed: {exc}"))
            return page
        if resp.status_code in (401, 403):
            page.attempts.append(SourceAttempt(tag, False, f"key {mask(key)} rejected (HTTP {resp.status_code})"))
            return page
        if resp.status_code >= 400:
            page.attempts.append(SourceAttempt(tag, False, f"HTTP {resp.status_code}: {resp.text[:160]}"))
            return page
        try:
            payload = resp.json()
        except ValueError:
            page.attempts.append(SourceAttempt(tag, False, "response was not JSON"))
            return page
        try:
            low = float(self.settings.get("low_balance", 5))
        except (TypeError, ValueError):
            low = 5.0
        rows, headline, available = balance_rows(payload if isinstance(payload, dict) else {}, low)
        if not rows:
            page.attempts.append(SourceAttempt(tag, False, f"no balance in response: {str(payload)[:160]}"))
            return page
        page.ok = True
        page.info = rows
        page.headline = headline
        page.plan = "Pay as you go"
        page.account = f"key {mask(key)}"
        page.source = "api.deepseek.com/user/balance"
        page.status = "connected" if available else "balance too low"
        page.data_time = now_utc()
        page.attempts.append(SourceAttempt(tag, True, mask(key)))
        return page

    def collect(self, result: ProviderResult) -> None:
        found, notes = discover_keys(self.settings)
        if not found:
            result.attempts.append(SourceAttempt("API key", False, "; ".join(notes)))
            result.status = "no DeepSeek API key found (OpenCode, DeepSeek Harness, DEEPSEEK_API_KEY)"
            return
        for note in notes:
            result.attempts.append(SourceAttempt("key search", False, note))
        if self.settings.get("scan_opencode", True):
            desktop, distros = opencode_desktop(os.environ)
            cli = opencode_cli(Path.home(), os.environ, local=True)
            detail = f"CLI: {cli or 'not found'}; Desktop: {desktop or 'not found'}"
            if distros:
                detail += f" (WSL servers: {', '.join(sorted(distros))})"
            if cli and desktop:
                detail += "; both read the same auth.json, so they share one key"
            result.attempts.append(SourceAttempt("OpenCode apps", True, detail))
        # One page per distinct key; every tool holding it is named on it.
        merged: dict[str, list[str]] = {}
        for key, where in found:
            places = merged.setdefault(key, [])
            if where not in places:
                places.append(where)
        pages = []
        for key, places in merged.items():
            page = self._page(key, " + ".join(places), result)
            result.attempts.extend(page.attempts)
            if page.ok:
                pages.append(page)
        if pages:
            result.adopt(pages[0])
            result.alternates = pages[1:]
            return
        failed = [a for a in result.attempts if not a.ok and a.name.startswith("balance")]
        result.status = failed[-1].detail if failed else "could not read the DeepSeek balance"
