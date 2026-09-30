"""AES-GCM decryption with Windows' own CNG (bcrypt.dll) through ctypes.

Chromium/Electron encrypt cookies and Claude Desktop's login with AES-256-GCM.
Using the system's implementation instead of the `cryptography` package keeps
roughly 10 MB of OpenSSL/Rust bindings out of the executable. Other platforms
(development, tests) fall back to `cryptography` when it is installed.
"""
from __future__ import annotations

import sys

TAG_LEN = 16

# Known-answer vector for --selftest: key 00..1f, nonce 64..6f.
_KAT_KEY = bytes(range(32))
_KAT_NONCE = bytes(range(100, 112))
_KAT_PLAIN = b"QuotaTray AES-GCM self-test"
_KAT_SEALED = bytes.fromhex(
    "196eb11218bd24ff47421ead89482dbe0fe2756fe70ade06c2a2d8f4b3e40a873225316aa50f12de16c28a"
)


class AesGcmError(ValueError):
    pass


def decrypt(key: bytes, nonce: bytes, sealed: bytes) -> bytes:
    """sealed = ciphertext || 16-byte tag. Raises AesGcmError on a bad tag."""
    if len(sealed) < TAG_LEN:
        raise AesGcmError("ciphertext shorter than the GCM tag")
    if sys.platform == "win32":
        return _bcrypt_decrypt(key, nonce, sealed[:-TAG_LEN], sealed[-TAG_LEN:])
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    try:
        return AESGCM(key).decrypt(nonce, sealed, None)
    except Exception as exc:                                    # noqa: BLE001
        raise AesGcmError(f"authentication failed ({exc.__class__.__name__})") from exc


def selftest() -> None:
    """Decrypt the known-answer vector; raises if the implementation is broken."""
    if decrypt(_KAT_KEY, _KAT_NONCE, _KAT_SEALED) != _KAT_PLAIN:
        raise AesGcmError("AES-GCM self-test produced the wrong plaintext")
    tampered = _KAT_SEALED[:-1] + bytes([_KAT_SEALED[-1] ^ 1])
    try:
        decrypt(_KAT_KEY, _KAT_NONCE, tampered)
    except AesGcmError:
        return
    raise AesGcmError("AES-GCM self-test accepted a forged tag")


def _bcrypt_decrypt(key: bytes, nonce: bytes, ciphertext: bytes, tag: bytes) -> bytes:
    import ctypes
    from ctypes import wintypes

    class AuthInfo(ctypes.Structure):
        # BCRYPT_AUTHENTICATED_CIPHER_MODE_INFO
        _fields_ = [
            ("cbSize", wintypes.ULONG),
            ("dwInfoVersion", wintypes.ULONG),
            ("pbNonce", ctypes.c_void_p),
            ("cbNonce", wintypes.ULONG),
            ("pbAuthData", ctypes.c_void_p),
            ("cbAuthData", wintypes.ULONG),
            ("pbTag", ctypes.c_void_p),
            ("cbTag", wintypes.ULONG),
            ("pbMacContext", ctypes.c_void_p),
            ("cbMacContext", wintypes.ULONG),
            ("cbAAD", wintypes.ULONG),
            ("cbData", ctypes.c_ulonglong),
            ("dwFlags", wintypes.ULONG),
        ]

    bcrypt = ctypes.WinDLL("bcrypt")
    bcrypt.BCryptOpenAlgorithmProvider.argtypes = [
        ctypes.POINTER(ctypes.c_void_p), wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.ULONG]
    bcrypt.BCryptSetProperty.argtypes = [
        ctypes.c_void_p, wintypes.LPCWSTR, ctypes.c_void_p, wintypes.ULONG, wintypes.ULONG]
    bcrypt.BCryptGetProperty.argtypes = [
        ctypes.c_void_p, wintypes.LPCWSTR, ctypes.c_void_p, wintypes.ULONG,
        ctypes.POINTER(wintypes.ULONG), wintypes.ULONG]
    bcrypt.BCryptGenerateSymmetricKey.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, wintypes.ULONG,
        ctypes.c_void_p, wintypes.ULONG, wintypes.ULONG]
    bcrypt.BCryptDecrypt.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, wintypes.ULONG, ctypes.c_void_p, ctypes.c_void_p,
        wintypes.ULONG, ctypes.c_void_p, wintypes.ULONG, ctypes.POINTER(wintypes.ULONG),
        wintypes.ULONG]
    bcrypt.BCryptDestroyKey.argtypes = [ctypes.c_void_p]
    bcrypt.BCryptCloseAlgorithmProvider.argtypes = [ctypes.c_void_p, wintypes.ULONG]
    for fn in ("BCryptOpenAlgorithmProvider", "BCryptSetProperty", "BCryptGetProperty",
               "BCryptGenerateSymmetricKey", "BCryptDecrypt", "BCryptDestroyKey",
               "BCryptCloseAlgorithmProvider"):
        getattr(bcrypt, fn).restype = ctypes.c_long            # NTSTATUS

    def check(status: int, what: str) -> None:
        if status != 0:
            raise AesGcmError(f"{what} failed (NTSTATUS 0x{status & 0xFFFFFFFF:08X})")

    alg = ctypes.c_void_p()
    check(bcrypt.BCryptOpenAlgorithmProvider(ctypes.byref(alg), "AES", None, 0), "open AES")
    key_handle = ctypes.c_void_p()
    try:
        mode = ctypes.create_unicode_buffer("ChainingModeGCM")
        check(bcrypt.BCryptSetProperty(alg, "ChainingMode", mode, ctypes.sizeof(mode), 0),
              "select GCM")
        obj_len = wintypes.ULONG()
        got = wintypes.ULONG()
        check(bcrypt.BCryptGetProperty(alg, "ObjectLength", ctypes.byref(obj_len),
                                       ctypes.sizeof(obj_len), ctypes.byref(got), 0),
              "read key object size")
        key_object = ctypes.create_string_buffer(obj_len.value)   # must outlive the key
        key_buf = ctypes.create_string_buffer(key, len(key))
        check(bcrypt.BCryptGenerateSymmetricKey(alg, ctypes.byref(key_handle), key_object,
                                                obj_len.value, key_buf, len(key), 0),
              "import key")
        nonce_buf = ctypes.create_string_buffer(nonce, len(nonce))
        tag_buf = ctypes.create_string_buffer(tag, len(tag))
        info = AuthInfo()
        info.cbSize = ctypes.sizeof(AuthInfo)
        info.dwInfoVersion = 1                                   # BCRYPT_..._INFO_VERSION
        info.pbNonce = ctypes.cast(nonce_buf, ctypes.c_void_p)
        info.cbNonce = len(nonce)
        info.pbTag = ctypes.cast(tag_buf, ctypes.c_void_p)
        info.cbTag = len(tag)
        src = ctypes.create_string_buffer(ciphertext, len(ciphertext))
        out = ctypes.create_string_buffer(max(1, len(ciphertext)))
        written = wintypes.ULONG()
        status = bcrypt.BCryptDecrypt(key_handle, src, len(ciphertext), ctypes.byref(info),
                                      None, 0, out, len(ciphertext), ctypes.byref(written), 0)
        if status != 0:
            raise AesGcmError(f"authentication failed (NTSTATUS 0x{status & 0xFFFFFFFF:08X})")
        _ = key_object                                           # keep alive until here
        return out.raw[:written.value]
    finally:
        if key_handle:
            bcrypt.BCryptDestroyKey(key_handle)
        bcrypt.BCryptCloseAlgorithmProvider(alg, 0)
