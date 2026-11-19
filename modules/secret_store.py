"""Authenticated secret storage backed by Windows DPAPI or a private local key."""
import ctypes
import os
from ctypes import wintypes


def _dpapi(data, decrypt=False):
    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if decrypt:
        fn = crypt.CryptUnprotectData
        fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                       ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    else:
        fn = crypt.CryptProtectData
        fn.argtypes = [ctypes.POINTER(Blob), wintypes.LPCWSTR, ctypes.c_void_p,
                       ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    fn.restype = wintypes.BOOL
    if not fn(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        kernel.LocalFree(target.data)


def _portable_key(create):
    from .app_paths import user_data_dir
    path = user_data_dir() / ".secrets" / "key"
    if create:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "wb") as stream:
                stream.write(os.urandom(32))
                stream.flush()
                os.fsync(stream.fileno())
    if path.stat().st_mode & 0o077:
        raise PermissionError("密钥文件仅允许当前用户访问")
    key = path.read_bytes()
    if len(key) != 32:
        raise ValueError("本地密钥文件长度错误")
    return key


def protect(data):
    if os.name == "nt":
        return b"D" + _dpapi(data)
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from .jsonio import file_lock
    with file_lock("lovomo-portable-secret-key"):
        key = _portable_key(True)
    nonce = os.urandom(12)
    return b"A" + nonce + AESGCM(key).encrypt(nonce, data, b"Lovomo/enc3")


def unprotect(blob):
    if blob[:1] == b"D" and os.name == "nt":
        return _dpapi(blob[1:], decrypt=True)
    if blob[:1] == b"A" and os.name != "nt":
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        return AESGCM(_portable_key(False)).decrypt(blob[1:13], blob[13:], b"Lovomo/enc3")
    raise ValueError("密文不属于当前系统或格式未知")
