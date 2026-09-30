"""AES-128-CBC decryption, for TRAE's storage.json "byte crypto" blobs.

On Windows it uses the system CNG (bcrypt.dll) so the trimmed build needs no
extra crypto library; other platforms (dev, tests) fall back to `cryptography`.
"""
from __future__ import annotations

import sys


class AesCbcError(ValueError):
    pass


def decrypt(key: bytes, iv: bytes, ciphertext: bytes) -> bytes:
    """AES-CBC decrypt with no unpadding (the caller strips its own padding)."""
    if len(ciphertext) == 0 or len(ciphertext) % 16 != 0:
        raise AesCbcError("ciphertext is not a whole number of blocks")
    if sys.platform == "win32":
        return _bcrypt_cbc(key, iv, ciphertext)
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    return dec.update(ciphertext) + dec.finalize()


def _bcrypt_cbc(key: bytes, iv: bytes, ciphertext: bytes) -> bytes:
    import ctypes
    from ctypes import wintypes

    bcrypt = ctypes.WinDLL("bcrypt")
    bcrypt.BCryptOpenAlgorithmProvider.argtypes = [
        ctypes.POINTER(ctypes.c_void_p), wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.ULONG]
    bcrypt.BCryptSetProperty.argtypes = [
        ctypes.c_void_p, wintypes.LPCWSTR, ctypes.c_void_p, wintypes.ULONG, wintypes.ULONG]
    bcrypt.BCryptGenerateSymmetricKey.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, wintypes.ULONG,
        ctypes.c_void_p, wintypes.ULONG, wintypes.ULONG]
    bcrypt.BCryptDecrypt.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, wintypes.ULONG, ctypes.c_void_p, ctypes.c_void_p,
        wintypes.ULONG, ctypes.c_void_p, wintypes.ULONG, ctypes.POINTER(wintypes.ULONG),
        wintypes.ULONG]
    bcrypt.BCryptDestroyKey.argtypes = [ctypes.c_void_p]
    bcrypt.BCryptCloseAlgorithmProvider.argtypes = [ctypes.c_void_p, wintypes.ULONG]
    for fn in ("BCryptOpenAlgorithmProvider", "BCryptSetProperty", "BCryptGenerateSymmetricKey",
               "BCryptDecrypt", "BCryptDestroyKey", "BCryptCloseAlgorithmProvider"):
        getattr(bcrypt, fn).restype = ctypes.c_long

    def check(status: int, what: str) -> None:
        if status != 0:
            raise AesCbcError(f"{what} failed (NTSTATUS 0x{status & 0xFFFFFFFF:08X})")

    alg = ctypes.c_void_p()
    check(bcrypt.BCryptOpenAlgorithmProvider(ctypes.byref(alg), "AES", None, 0), "open AES")
    key_handle = ctypes.c_void_p()
    try:
        mode = ctypes.create_unicode_buffer("ChainingModeCBC")
        check(bcrypt.BCryptSetProperty(alg, "ChainingMode", mode, ctypes.sizeof(mode), 0), "select CBC")
        key_buf = ctypes.create_string_buffer(key, len(key))
        check(bcrypt.BCryptGenerateSymmetricKey(alg, ctypes.byref(key_handle), None, 0,
                                                key_buf, len(key), 0), "import key")
        iv_buf = ctypes.create_string_buffer(iv, len(iv))       # BCryptDecrypt updates the IV in place
        src = ctypes.create_string_buffer(ciphertext, len(ciphertext))
        out = ctypes.create_string_buffer(len(ciphertext))
        written = wintypes.ULONG()
        check(bcrypt.BCryptDecrypt(key_handle, src, len(ciphertext), None, iv_buf, len(iv),
                                   out, len(ciphertext), ctypes.byref(written), 0), "decrypt")
        return out.raw[:written.value]
    finally:
        if key_handle:
            bcrypt.BCryptDestroyKey(key_handle)
        bcrypt.BCryptCloseAlgorithmProvider(alg, 0)
