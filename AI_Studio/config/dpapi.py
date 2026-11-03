"""Windows DPAPI 加解密。

把敏感数据（API Key）绑定到当前 Windows 用户账户加密，落盘后即使拿到文件，
换一台机器或换一个用户也无法解密。
"""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

_APP_DESCRIPTION = "AI Studio"


class DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_byte)),
    ]


def is_supported() -> bool:
    return os.name == "nt"


def _require_support(action: str) -> None:
    if not is_supported():
        raise RuntimeError(f"API Key {action}功能目前仅支持 Windows")


def encrypt(data: bytes) -> bytes:
    """用当前用户身份加密字节串。"""
    _require_support("加密")

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    source_buffer = ctypes.create_string_buffer(data)
    source = DATA_BLOB(
        len(data),
        ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_byte)),
    )
    destination = DATA_BLOB()

    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(DATA_BLOB),
        wintypes.LPCWSTR,
        ctypes.POINTER(DATA_BLOB),
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(DATA_BLOB),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL

    ok = crypt32.CryptProtectData(
        ctypes.byref(source),
        _APP_DESCRIPTION,
        None,
        None,
        None,
        0,
        ctypes.byref(destination),
    )
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())

    try:
        return ctypes.string_at(destination.pbData, destination.cbData)
    finally:
        kernel32.LocalFree(destination.pbData)


def decrypt(data: bytes) -> bytes:
    """解密本机当前用户加密的字节串。"""
    _require_support("解密")

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    source_buffer = ctypes.create_string_buffer(data)
    source = DATA_BLOB(
        len(data),
        ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_byte)),
    )
    destination = DATA_BLOB()

    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(DATA_BLOB),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(DATA_BLOB),
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(DATA_BLOB),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL

    ok = crypt32.CryptUnprotectData(
        ctypes.byref(source),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(destination),
    )
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())

    try:
        return ctypes.string_at(destination.pbData, destination.cbData)
    finally:
        kernel32.LocalFree(destination.pbData)
