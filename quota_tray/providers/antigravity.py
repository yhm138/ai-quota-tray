"""Antigravity IDE (Google) quota collection.

Antigravity is a VS Code fork that runs a Codeium-family language server. That
server is launched with a --csrf_token argument and listens on a random port on
127.0.0.1, exposing Connect RPC. Asking it for GetUserStatus returns the prompt
credit balance and per-model remaining quota, with no Google sign-in and no
outbound network call.

Fallbacks:
  1. read --csrf_token and the listening ports off the running process
  2. csrf_token / port pinned by hand in config.json
  3. the last working endpoint is cached and reused on the next refresh
"""
from __future__ import annotations

import logging
import os
import re
import sys
from pathlib import Path

from ..model import ProviderResult, QuotaWindow, SourceAttempt, now_utc, parse_time
from ..win.proc import powershell_json, run
from .base import Provider, session

log = logging.getLogger(__name__)

RPC_PATH = "/exa.language_server_pb.LanguageServerService/GetUserStatus"
RPC_BODY = {"metadata": {"ideName": "antigravity", "extensionName": "antigravity", "locale": "en"}}

# Process list only. Port mapping is done separately with netstat, because
# Get-NetTCPConnection is missing or blocked on some locked-down machines.
# The script ALWAYS prints valid JSON: an empty result must not look like a
# failure, or "IDE not running" gets reported as "PowerShell was blocked".
PS_PROCESSES = r"""
$ErrorActionPreference='SilentlyContinue'
$procs = @(Get-CimInstance Win32_Process | Where-Object {
  $_.CommandLine -and ($_.CommandLine -match 'antigravity' -or $_.CommandLine -match 'csrf_token')
} | ForEach-Object {
  [pscustomobject]@{ ProcessId = $_.ProcessId; CommandLine = $_.CommandLine }
})
if ($procs.Count -eq 0) { '[]' } else { ConvertTo-Json -InputObject $procs -Depth 3 -Compress }
"""


def _extract_arg(cmdline: str, name: str) -> str | None:
    patterns = (
        rf"{name}[=\s]+\"([^\"]+)\"",
        rf"{name}[=\s]+'([^']+)'",
        rf"{name}[=\s]+([^\s\"']+)",
    )
    for pattern in patterns:
        m = re.search(pattern, cmdline, re.IGNORECASE)
        if m:
            return m.group(1)
    return None


def _score(cmdline: str) -> int:
    low = cmdline.lower()
    score = 0
    if "antigravity" in low:
        score += 1
    if "language_server" in low or "language-server" in low or "lsp" in low:
        score += 50
    if "--csrf_token" in low:
        score += 20
    if "--extension_server_port" in low:
        score += 10
    return score


def _processes() -> tuple[list[tuple[int, str]], str | None]:
    """Every process whose command line mentions antigravity or csrf_token.

    Returns (entries, error). PowerShell first; wmic is the fallback for
    machines where PowerShell is locked down by policy or security software.
    """
    data = powershell_json(PS_PROCESSES, timeout=30)
    if isinstance(data, dict):
        data = [data]
    if isinstance(data, list):
        out = []
        for item in data:
            if not isinstance(item, dict):
                continue
            pid = item.get("ProcessId")
            cmd = str(item.get("CommandLine") or "")
            if isinstance(pid, int) and cmd:
                out.append((pid, cmd))
        return out, None

    # PowerShell gave us nothing usable - fall back to wmic.
    code, out_text, err = run(
        [
            "wmic",
            "process",
            "get",
            "processid,commandline",
            "/format:csv",
        ],
        timeout=30,
    )
    if code != 0 and not out_text.strip():
        return [], f"PowerShell returned no JSON and wmic failed ({err.strip()[:120] or 'rc=' + str(code)})"
    entries: list[tuple[int, str]] = []
    for line in out_text.splitlines():
        low = line.lower()
        if "antigravity" not in low and "csrf_token" not in low:
            continue
        parts = line.rsplit(",", 1)
        if len(parts) != 2 or not parts[1].strip().isdigit():
            continue
        cmd = parts[0].split(",", 1)[-1]
        entries.append((int(parts[1].strip()), cmd))
    return entries, None


def _listening_ports() -> tuple[dict[int, list[int]], str | None]:
    """Map pid -> listening TCP ports via netstat, which exists everywhere."""
    code, out, err = run(["netstat", "-ano", "-p", "TCP"], timeout=25)
    if code != 0 and not out.strip():
        code, out, err = run(["netstat", "-ano"], timeout=25)
    if not out.strip():
        return {}, f"netstat produced no output ({err.strip()[:120] or 'rc=' + str(code)})"
    mapping: dict[int, list[int]] = {}
    for line in out.splitlines():
        if "LISTEN" not in line.upper():
            continue
        parts = line.split()
        if len(parts) < 4:
            continue
        pid_text = parts[-1]
        if not pid_text.isdigit():
            continue
        m = re.search(r":(\d+)$", parts[1])
        if not m:
            continue
        pid, port = int(pid_text), int(m.group(1))
        mapping.setdefault(pid, [])
        if port not in mapping[pid]:
            mapping[pid].append(port)
    return mapping, None


def discover_endpoints() -> tuple[list[tuple[str, int]], str | None, list[str]]:
    """Return ([(base_url, port), ...], csrf_token, diagnostics)."""
    notes: list[str] = []
    if sys.platform != "win32":
        return [], None, ["process discovery is Windows-only"]

    procs, proc_err = _processes()
    if proc_err:
        return [], None, [proc_err]
    if not procs:
        return [], None, ["no Antigravity language server process - open Antigravity IDE first"]

    ports_by_pid, port_err = _listening_ports()
    if port_err:
        notes.append(port_err)

    scored = sorted(((_score(cmd), pid, cmd) for pid, cmd in procs), reverse=True)
    token = next((tok for _, _, cmd in scored if (tok := _extract_arg(cmd, "--csrf_token"))), None)

    candidates: list[tuple[str, int]] = []
    for _, pid, _cmd in scored:
        for port in ports_by_pid.get(pid, []):
            for scheme in ("https", "http"):
                pair = (f"{scheme}://127.0.0.1:{port}", port)
                if pair not in candidates:
                    candidates.append(pair)

    if not candidates:
        notes.append(
            f"found {len(procs)} Antigravity process(es) (pids "
            f"{', '.join(str(p) for _, p, _ in scored[:4])}) but none are listening on TCP"
        )
        return [], token, notes

    notes.append(
        f"{len(procs)} process(es), candidate ports "
        f"{sorted({p for _, p in candidates})}, csrf_token {'found' if token else 'missing'}"
    )
    return candidates[:24], token, notes


def call_rpc(base_url: str, csrf: str | None, path: str = RPC_PATH, timeout: float = 6.0):
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Connect-Protocol-Version": "1",
    }
    if csrf:
        headers["X-Codeium-Csrf-Token"] = csrf
    resp = session().post(
        base_url + path, headers=headers, json=RPC_BODY, timeout=timeout, verify=False
    )
    resp.raise_for_status()
    return resp.json()


def parse_user_status(payload: dict, settings: dict) -> tuple[list[QuotaWindow], str | None, str | None]:
    status = payload.get("userStatus") if isinstance(payload.get("userStatus"), dict) else payload
    if not isinstance(status, dict):
        return [], None, None

    windows: list[QuotaWindow] = []
    account = status.get("email") if isinstance(status.get("email"), str) else None
    plan = None

    plan_status = status.get("planStatus")
    if isinstance(plan_status, dict):
        info = plan_status.get("planInfo") if isinstance(plan_status.get("planInfo"), dict) else {}
        plan = info.get("planName") or info.get("name") or plan_status.get("planName")
        available = plan_status.get("availablePromptCredits")
        monthly = info.get("monthlyPromptCredits") or plan_status.get("monthlyPromptCredits")
        if isinstance(available, (int, float)) and isinstance(monthly, (int, float)) and monthly > 0:
            used = max(0.0, float(monthly) - float(available))
            windows.append(
                QuotaWindow(
                    key="prompt_credits",
                    label="Prompt credits",
                    percent=min(100.0, used / float(monthly) * 100.0),
                    detail=f"{available:,.0f} / {monthly:,.0f} left",
                    resets_at=parse_time(plan_status.get("resetTime") or info.get("resetTime")),
                    order=5,
                    exhausted=available <= 0,
                )
            )

    if settings.get("show_models", True):
        cascade = status.get("cascadeModelConfigData")
        configs = cascade.get("clientModelConfigs") if isinstance(cascade, dict) else None
        if isinstance(configs, list):
            limit = int(settings.get("max_models", 6))
            rank = 10
            seen_labels: set[str] = set()
            for model in configs:
                if not isinstance(model, dict):
                    continue
                quota_info = model.get("quotaInfo")
                if not isinstance(quota_info, dict):
                    continue
                remaining = quota_info.get("remainingFraction")
                if not isinstance(remaining, (int, float)):
                    continue
                alias = model.get("modelOrAlias")
                model_id = alias.get("model") if isinstance(alias, dict) else None
                label = str(model.get("label") or model_id or "Model")
                # The same display name can appear twice with different model
                # ids; keep the first and disambiguate the rest.
                if label in seen_labels:
                    suffix = str(model_id or "").rsplit("_", 1)[-1].title()
                    label = f"{label} ({suffix})" if suffix else f"{label} #2"
                seen_labels.add(label)
                windows.append(
                    QuotaWindow(
                        key=str(model_id or label),
                        label=str(label),
                        percent=max(0.0, min(100.0, (1.0 - float(remaining)) * 100.0)),
                        resets_at=parse_time(quota_info.get("resetTime")),
                        order=rank,
                        exhausted=float(remaining) <= 0,
                    )
                )
                rank += 1
                if len(windows) >= limit + 1:
                    break
    return windows, account, plan


class AntigravityProvider(Provider):
    id = "antigravity"
    name = "Antigravity"

    def __init__(self, config):
        super().__init__(config)
        self._cached: tuple[str, str | None] | None = None

    def detect(self) -> bool:
        for env in ("APPDATA", "LOCALAPPDATA", "USERPROFILE"):
            base = os.environ.get(env)
            if not base:
                continue
            for name in ("Antigravity IDE", "Antigravity", ".antigravity", ".antigravity-ide"):
                if (Path(base) / name).is_dir():
                    return True
        return (Path.home() / ".antigravity").is_dir()

    def _attempt(self, base_url: str, csrf: str | None):
        payload = call_rpc(base_url, csrf)
        windows, account, plan = parse_user_status(payload, self.settings)
        return windows, account, plan

    def collect(self, result: ProviderResult) -> None:
        import urllib3

        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        manual_port = int(self.settings.get("port") or 0)
        manual_csrf = (self.settings.get("csrf_token") or "").strip() or None

        tries: list[tuple[str, str | None]] = []
        if self._cached:
            tries.append(self._cached)
        if manual_port:
            for scheme in ("https", "http"):
                tries.append((f"{scheme}://127.0.0.1:{manual_port}", manual_csrf))

        notes: list[str] = []
        if not tries or not self._cached:
            candidates, token, notes = discover_endpoints()
            token = manual_csrf or token
            tries.extend((url, token) for url, _ in candidates)

        if not tries:
            result.attempts.append(
                SourceAttempt(
                    "local language server",
                    False,
                    "; ".join(notes) or "no Antigravity language server found",
                )
            )
            result.status = "IDE not running" if result.installed else "Antigravity not detected"
            return

        last_error = ""
        for base_url, csrf in tries:
            try:
                windows, account, plan = self._attempt(base_url, csrf)
            except Exception as exc:                            # noqa: BLE001
                last_error = f"{base_url}: {type(exc).__name__}"
                continue
            if not windows:
                last_error = f"{base_url}: connected but no quota fields in the response"
                continue
            self._cached = (base_url, csrf)
            result.windows = windows
            result.ok = True
            result.source = f"local language server {base_url}"
            result.account = account
            result.plan = plan
            result.status = "connected"
            result.data_time = now_utc()
            result.attempts.append(SourceAttempt("local language server", True, base_url))
            return

        self._cached = None
        result.attempts.append(
            SourceAttempt(
                "local language server",
                False,
                last_error or "; ".join(notes) or "no endpoint responded",
            )
        )
        result.status = "IDE not running or not signed in"
