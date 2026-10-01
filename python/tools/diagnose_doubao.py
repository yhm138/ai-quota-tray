"""Read-only Doubao request probe; never prints credentials or response values.

Run from any directory with Python. The default config is the app's config.json.
Only the two fixed doubao.com endpoints are requested, without redirects.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from quota_tray.config import CONFIG_PATH, Config
from quota_tray.providers.base import session
from quota_tray.providers.doubao import (
    BROWSER_UA, OVERVIEW_PARAMS, OVERVIEW_URL, PROFILE_URL, DoubaoProvider, parse_overview,
)


def shape(value):
    """JSON field names and types only; no scalar values or array counts."""
    if isinstance(value, dict):
        return {key: shape(item) for key, item in value.items()}
    if isinstance(value, list):
        kinds = []
        for item in value:
            kind = shape(item)
            if kind not in kinds:
                kinds.append(kind)
        return kinds
    return "null" if value is None else type(value).__name__


def safe_message(value):
    if not isinstance(value, str):
        return None
    # Service errors can echo an unknown credential, including short values
    # which cannot be reliably detected by length or regular expressions.
    # Only these fixed, non-sensitive messages are emitted verbatim.
    if value in ("", "success", "ok", "OK", "login invalid", "\u7cfb\u7edf\u9519\u8bef", "\u767b\u5f55\u5df2\u8fc7\u671f"):
        return value
    return "[REDACTED: unrecognized server message]"


def probe(label, cookie, compare_profile_post=False):
    headers = {"Cookie": cookie, "Accept": "application/json",
               "Referer": "https://www.doubao.com/chat/", "User-Agent": BROWSER_UA}
    requests = [("GET", PROFILE_URL), ("POST", OVERVIEW_URL)]
    if compare_profile_post:
        requests.append(("POST", PROFILE_URL))
    for method, url in requests:
        options = {"headers": dict(headers), "timeout": 20, "allow_redirects": False}
        if method == "POST":
            options["headers"].update({"Accept": "application/json, text/plain, */*",
                                       "Content-Type": "application/json", "agw-js-conv": "str"})
            options.update(params=OVERVIEW_PARAMS, json=(
                {"product_line": "membership"} if url == OVERVIEW_URL else {"avatar_format": "png"}))
        report = {"credential_mode": label, "method": method,
                  "endpoint": url, "query_names": sorted(OVERVIEW_PARAMS) if method == "POST" else []}
        try:
            response = session().request(method, url, **options)
            report["http_status"] = response.status_code
            try:
                payload = response.json()
            except ValueError:
                payload = None
            report["json_object"] = isinstance(payload, dict)
            if isinstance(payload, dict):
                code = payload.get("code")
                report["code"] = code if isinstance(code, (int, float)) else (
                    code if isinstance(code, str) and code.isdigit() else None)
                report["msg"] = safe_message(payload.get("msg", payload.get("message")))
                if code == 0 and url == OVERVIEW_URL:
                    plan, windows, rows = parse_overview(payload)
                    report.update(plan_present=bool(plan), window_count=len(windows), info_row_count=len(rows))
                    if not windows:
                        report["data_structure"] = shape(payload.get("data"))
        except Exception as exc:
            # Exception text can contain headers/URLs; never print it.
            report["request_error"] = type(exc).__name__
        print(json.dumps(report, ensure_ascii=True), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=CONFIG_PATH())
    parser.add_argument("--compare-profile-post", action="store_true",
                        help="also try the browser's profile method with the minimal public body")
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    try:
        config = Config(json.loads(args.config.read_text(encoding="utf-8-sig")))
        provider = DoubaoProvider(config)
        if not (provider.settings.get("session_id") or "").strip():
            print('{"config_error":"session_id is empty; no browser-store fallback in this probe"}')
            return 1
        cookie, _ = provider._cookie_header()
    except Exception as exc:
        print(json.dumps({"config_error": type(exc).__name__}))
        return 1
    probe("configured_cookie_header", cookie, args.compare_profile_post)
    bare = next((part.partition("=")[2].strip() for part in cookie.split(";")
                 if part.partition("=")[0].strip() == "sessionid"), None)
    if bare:
        probe("sessionid_only", "sessionid=" + bare, args.compare_profile_post)
    else:
        print('{"sessionid_only":"not available in configured header"}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
