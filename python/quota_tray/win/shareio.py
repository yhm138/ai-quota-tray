"""Read a file without blocking the program that owns it.

CLIs like gemini-cli, codex and Claude Code refresh their login token on start
and replace the credential file with an atomic rename (write temp, then
MoveFileEx over the old one). On Windows that rename is *denied* while another
process holds the file open unless that process opened it with
FILE_SHARE_DELETE -- which Python's open() does not. So reading a credential
file the normal way can make the owning CLI fail to persist its refreshed
token, and it then re-authorizes on every run.

read_text() opens with FILE_SHARE_READ | WRITE | DELETE on Windows, so the
owner can replace the file even while QuotaTray is reading it. Elsewhere it is
a plain read.
"""
from __future__ import annotations

import sys
from pathlib import Path


def read_bytes(path: Path) -> bytes | None:
    """File contents, or None if it cannot be read. Never blocks the owner."""
    if sys.platform != "win32":
        try:
            return path.read_bytes()
        except OSError:
            return None
    return _read_bytes_shared(path)


def read_text(path: Path, encoding: str = "utf-8", errors: str = "replace") -> str | None:
    data = read_bytes(path)
    if data is None:
        return None
    if data.startswith(b"\xef\xbb\xbf"):            # strip a UTF-8 BOM
        data = data[3:]
    try:
        return data.decode(encoding, errors)
    except LookupError:
        return data.decode("utf-8", "replace")


def _read_bytes_shared(path: Path) -> bytes | None:
    import ctypes
    from ctypes import wintypes

    GENERIC_READ = 0x80000000
    FILE_SHARE_READ_WRITE_DELETE = 0x1 | 0x2 | 0x4
    OPEN_EXISTING = 3
    INVALID = ctypes.c_void_p(-1).value

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                     wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.ReadFile.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                                  ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    kernel32.GetFileSizeEx.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_longlong)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    handle = kernel32.CreateFileW(str(path), GENERIC_READ, FILE_SHARE_READ_WRITE_DELETE, None,
                                  OPEN_EXISTING, 0x80, None)         # FILE_ATTRIBUTE_NORMAL
    if handle == INVALID or not handle:
        return None
    try:
        size = ctypes.c_longlong(0)
        if not kernel32.GetFileSizeEx(handle, ctypes.byref(size)) or size.value < 0:
            return None
        if size.value == 0:
            return b""
        if size.value > 64 * 1024 * 1024:            # credential files are tiny; cap defensively
            return None
        buf = ctypes.create_string_buffer(size.value)
        read = wintypes.DWORD(0)
        got = bytearray()
        offset = 0
        while offset < size.value:
            if not kernel32.ReadFile(handle, ctypes.byref(buf, offset),
                                     size.value - offset, ctypes.byref(read), None):
                return None
            if read.value == 0:
                break
            got += buf.raw[offset:offset + read.value]
            offset += read.value
        return bytes(got)
    finally:
        kernel32.CloseHandle(handle)
