"""Keep the tray icon actually visible.

pystray adds the icon once and never checks the result. Shell_NotifyIcon can
fail (Explorer busy or still starting at login), and Explorer can drop an
icon, which leaves QuotaTray running with nothing in the tray. On top of
that, Windows 11 puts every icon from a new exe path under the ^ overflow,
so a freshly downloaded QuotaTray.exe looked like it never started.

- icon_present() asks Explorer whether our icon exists (Shell_NotifyIconGetRect)
- promote() marks our icon "show in the taskbar" in Windows 11's per-icon
  settings, but only while the user has not chosen for it themselves
"""
from __future__ import annotations

import logging
import os
import re
import sys

log = logging.getLogger(__name__)

NOTIFY_SETTINGS = r"Control Panel\NotifyIconSettings"


def icon_present(hwnd, uid: int = 0) -> bool | None:
    """True/False when Explorer knows our icon; None when it cannot tell."""
    if sys.platform != "win32" or not hwnd:
        return None
    import ctypes
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                    ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

    class NOTIFYICONIDENTIFIER(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND),
                    ("uID", wintypes.UINT), ("guidItem", GUID)]

    try:
        fn = ctypes.windll.shell32.Shell_NotifyIconGetRect
    except AttributeError:
        return None
    fn.argtypes = [ctypes.POINTER(NOTIFYICONIDENTIFIER), ctypes.POINTER(wintypes.RECT)]
    fn.restype = ctypes.c_long
    ident = NOTIFYICONIDENTIFIER(cbSize=ctypes.sizeof(NOTIFYICONIDENTIFIER), hWnd=hwnd, uID=uid)
    rect = wintypes.RECT()
    # S_OK even for an icon in the overflow; E_FAIL when there is none.
    return fn(ctypes.byref(ident), ctypes.byref(rect)) == 0


def _known_folder(guid: str) -> str | None:
    """Explorer stores paths under known folders as {GUID}\\rest."""
    if sys.platform != "win32":
        return None
    import ctypes
    import uuid
    from ctypes import wintypes

    try:
        raw = uuid.UUID(guid).bytes_le
    except ValueError:
        return None
    buf = (ctypes.c_ubyte * 16).from_buffer_copy(raw)
    out = ctypes.c_wchar_p()
    fn = ctypes.windll.shell32.SHGetKnownFolderPath
    fn.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.HANDLE, ctypes.POINTER(ctypes.c_wchar_p)]
    fn.restype = ctypes.c_long
    if fn(ctypes.byref(buf), 0, None, ctypes.byref(out)) != 0:
        return None
    try:
        return out.value
    finally:
        ctypes.windll.ole32.CoTaskMemFree(out)


def expand_path(stored: str, resolve=_known_folder) -> str:
    m = re.match(r"^(\{[0-9A-Fa-f-]{36}\})\\(.*)$", stored or "")
    if m:
        base = resolve(m.group(1))
        if base:
            return os.path.join(base, *re.split(r"[\\/]", m.group(2)))
    return stored or ""


def same_path(a: str, b: str) -> bool:
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def find_entries(entries, exe: str, resolve=_known_folder) -> list:
    """entries: [(subkey, ExecutablePath, IsPromoted or None)] -> matching ones."""
    return [e for e in entries if e[1] and same_path(expand_path(e[1], resolve), exe)]


def _read_entries() -> list:
    import winreg

    out = []
    try:
        root = winreg.OpenKey(winreg.HKEY_CURRENT_USER, NOTIFY_SETTINGS)
    except OSError:
        return out
    with root:
        i = 0
        while True:
            try:
                name = winreg.EnumKey(root, i)
            except OSError:
                break
            i += 1
            try:
                with winreg.OpenKey(root, name) as key:
                    path = winreg.QueryValueEx(key, "ExecutablePath")[0]
                    try:
                        promoted = winreg.QueryValueEx(key, "IsPromoted")[0]
                    except OSError:
                        promoted = None
                    out.append((name, str(path), promoted))
            except OSError:
                continue
    return out


def promote(exe: str) -> str:
    """Show our icon on the taskbar instead of the overflow (Windows 11).
    Returns what happened, for the log. Never overrides the user's choice."""
    if sys.platform != "win32":
        return "not Windows"
    import winreg

    matches = find_entries(_read_entries(), exe)
    if not matches:
        return "no entry yet"
    changed = 0
    for name, _path, promoted in matches:
        if promoted is not None:
            continue                          # the user (or we, earlier) already decided
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, f"{NOTIFY_SETTINGS}\\{name}", 0,
                                winreg.KEY_SET_VALUE) as key:
                winreg.SetValueEx(key, "IsPromoted", 0, winreg.REG_DWORD, 1)
            changed += 1
        except OSError as exc:
            log.info("could not promote the tray icon: %s", exc)
    return f"promoted {changed} entr{'y' if changed == 1 else 'ies'}" if changed else "left as the user set it"
