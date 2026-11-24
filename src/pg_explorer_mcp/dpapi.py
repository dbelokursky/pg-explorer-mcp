"""DPAPI: the protection Windows keeps for the account of a process. It seals the password file (ADR 0012).

Windows only, asked through ctypes as the standard library asks it itself. The key is the account's:
another account, or another machine, cannot open what was sealed here. CRYPTPROTECT_UI_FORBIDDEN
forbids any dialog: the agent starts the server with no terminal to answer one in.

The module can be imported anywhere; sealing and unsealing are called on Windows alone.
"""

from __future__ import annotations

import ctypes
import sys
from typing import Any

ENTROPY = b"pg-explorer-mcp passwords"  # ties the file to this server: a blob of another program does not open
UI_FORBIDDEN = 0x1  # CRYPTPROTECT_UI_FORBIDDEN


class _Blob(ctypes.Structure):
    """DATA_BLOB of the Windows API: the bytes and how many there are."""

    _fields_ = [("size", ctypes.c_uint32), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def _blob(data: bytes) -> _Blob:
    buffer = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))


def _found(function: str) -> tuple[Any, Any]:
    """One of the two functions of crypt32 with its signature told, and LocalFree for what they return."""
    if sys.platform == "win32":
        called = getattr(ctypes.WinDLL("crypt32", use_last_error=True), function)
        called.restype = ctypes.c_int
        called.argtypes = [
            ctypes.POINTER(_Blob),  # the bytes given
            ctypes.c_void_p,  # a description, or room for one: not asked for
            ctypes.POINTER(_Blob),  # the optional entropy
            ctypes.c_void_p,  # reserved
            ctypes.c_void_p,  # the prompt structure: forbidden anyway by the flag
            ctypes.c_uint32,  # the flags
            ctypes.POINTER(_Blob),  # the bytes returned
        ]
        free = ctypes.WinDLL("kernel32", use_last_error=True).LocalFree
        free.restype, free.argtypes = ctypes.c_void_p, [ctypes.c_void_p]  # a handle, whole on 64 bits
        return called, free
    raise OSError(f"{function}: DPAPI is Windows'")


def _run(function: str, data: bytes) -> bytes:
    """crypt32's function on the bytes, with the entropy of the server; OSError with the code when it refuses."""
    called, free = _found(function)
    given, entropy, got = _blob(data), _blob(ENTROPY), _Blob()
    if not called(given, None, entropy, None, None, UI_FORBIDDEN, got):
        raise _refusal()
    try:
        return ctypes.string_at(got.data, got.size)
    finally:
        free(got.data)


def _refusal() -> OSError:
    """The error Windows gave, with its code."""
    if sys.platform == "win32":
        return ctypes.WinError(ctypes.get_last_error())
    raise OSError("DPAPI refused")


def seal(data: bytes) -> bytes:
    """The bytes sealed for the account of this process."""
    return _run("CryptProtectData", data)


def unseal(sealed: bytes) -> bytes:
    """The bytes as they were sealed for this account, with the entropy of the server."""
    return _run("CryptUnprotectData", sealed)
