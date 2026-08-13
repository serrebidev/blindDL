# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Decrypt Chromium app-bound ("v20") cookies, with the user's permission.

Chromium 127+ ties cookie decryption to the browser's own elevation service:
the app-bound key in ``Local State`` is wrapped by a first DPAPI layer that
can only be unwrapped as the SYSTEM account, and the browser refuses to do it
for any caller that is not the browser itself. The only known workaround is
to run as SYSTEM for that first layer -- which needs administrator rights and
briefly impersonating the SYSTEM token -- then finish the unwrap as the
logged-in user. This module does exactly that, and nothing else.

It must run in a short-lived, UAC-elevated helper process; the ordinary
window never calls it, so a UAC prompt is the only side effect.
"""

import base64
import ctypes
import json
import os
import shutil
import sqlite3
import struct
import sys
import tempfile

from Crypto.Cipher import AES, ChaCha20_Poly1305

# win32api / win32con / win32crypt / win32security are imported lazily inside
# the functions that need them so this module stays importable on macOS and
# Linux, where app-bound decryption never runs.

# Keys embedded in Chromium's elevation_service.exe. Public Chromium
# constants, not browser secrets; they only finish unwrapping the app-bound
# master key after both DPAPI layers are already removed.
_AES_KEY_V1 = bytes.fromhex(
    "B31C6E241AC846728DA9C1FAC4936651CFFB944D143AB816276BCC6DA0284787"
)
_CHACHA20_KEY_V2 = bytes.fromhex(
    "E98F37D7F4E1FA433D19304DC2258042090E2D1D7EEA7670D41F738D08729660"
)
_CNG_XOR_KEY_V3 = bytes.fromhex(
    "CCF8A1CEC56605B8517552BA1A2D061C03A29E90274FB2FCF59BA4B75C392390"
)
_CNG_KEY_NAME = "Google Chromekey1"
_CNG_PROVIDER = "Microsoft Software Key Storage Provider"
_NCRYPT_SILENT_FLAG = 0x40

_APPB_PREFIX = b"APPB"


class AppBoundError(RuntimeError):
    """The app-bound key or a cookie could not be decrypted."""


def _dpapi_unprotect(data):
    import win32crypt

    return win32crypt.CryptUnprotectData(data, None, None, None, 0)[1]


def _find_process_id(name):
    """PID of the first process whose image name matches, or None."""
    from ctypes import wintypes

    class _PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_void_p),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)  # process
    if snapshot in (0, wintypes.HANDLE(-1).value):
        return None
    entry = _PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
    try:
        if kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            while True:
                if entry.szExeFile.casefold() == name.casefold():
                    return entry.th32ProcessID
                if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                    break
    finally:
        kernel32.CloseHandle(snapshot)
    return None


class _SystemImpersonation:
    """Impersonate the SYSTEM token for the lifetime of the context manager."""

    def __enter__(self):
        import win32api
        import win32con
        import win32security

        process = win32api.GetCurrentProcess()
        token = win32security.OpenProcessToken(
            process, win32con.TOKEN_ADJUST_PRIVILEGES | win32con.TOKEN_QUERY
        )
        try:
            luid = win32security.LookupPrivilegeValue(None, win32security.SE_DEBUG_NAME)
            win32security.AdjustTokenPrivileges(
                token, 0, [(luid, win32con.SE_PRIVILEGE_ENABLED)]
            )
        finally:
            win32api.CloseHandle(token)

        pid = _find_process_id("lsass.exe")
        if pid is None:
            raise AppBoundError("could not find lsass.exe to obtain SYSTEM")
        process = win32api.OpenProcess(
            win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if not process:
            raise AppBoundError("could not open lsass.exe")
        self._process = process
        try:
            system_token = win32security.OpenProcessToken(
                process,
                win32con.TOKEN_DUPLICATE
                | win32con.TOKEN_IMPERSONATE
                | win32con.TOKEN_QUERY,
            )
        except BaseException:
            win32api.CloseHandle(process)
            raise
        try:
            self._duplicate = win32security.DuplicateToken(
                system_token, win32security.SecurityImpersonation
            )
        finally:
            win32api.CloseHandle(system_token)
        win32security.SetThreadToken(None, self._duplicate)
        return self

    def __exit__(self, _exc_type, _exc, _tb):
        import win32api
        import win32security

        win32security.RevertToSelf()
        if getattr(self, "_duplicate", None):
            win32api.CloseHandle(self._duplicate)
        if getattr(self, "_process", None):
            win32api.CloseHandle(self._process)
        return False


def _cng_decrypt(data):
    """RSA-decrypt with the ``Google Chromekey1`` CNG key (as SYSTEM)."""
    from ctypes import wintypes

    ncrypt = ctypes.WinDLL("ncrypt", use_last_error=True)
    c_void_p = ctypes.c_void_p
    ncrypt.NCryptOpenStorageProvider.argtypes = [
        ctypes.POINTER(c_void_p),
        wintypes.LPCWSTR,
        wintypes.DWORD,
    ]
    ncrypt.NCryptOpenStorageProvider.restype = ctypes.c_long
    ncrypt.NCryptOpenKey.argtypes = [
        c_void_p,
        ctypes.POINTER(c_void_p),
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    ncrypt.NCryptOpenKey.restype = ctypes.c_long
    ncrypt.NCryptDecrypt.argtypes = [
        c_void_p,
        ctypes.POINTER(ctypes.c_ubyte),
        wintypes.DWORD,
        c_void_p,
        ctypes.POINTER(ctypes.c_ubyte),
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.DWORD,
    ]
    ncrypt.NCryptDecrypt.restype = ctypes.c_long
    ncrypt.NCryptFreeObject.argtypes = [c_void_p]
    ncrypt.NCryptFreeObject.restype = ctypes.c_long

    provider = c_void_p()
    status = ncrypt.NCryptOpenStorageProvider(ctypes.byref(provider), _CNG_PROVIDER, 0)
    if status != 0:
        raise AppBoundError(f"NCryptOpenStorageProvider failed: {status:#x}")
    key = c_void_p()
    try:
        status = ncrypt.NCryptOpenKey(provider, ctypes.byref(key), _CNG_KEY_NAME, 0, 0)
        if status != 0:
            raise AppBoundError(f"NCryptOpenKey failed: {status:#x}")
        try:
            result_len = wintypes.DWORD()
            buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
            status = ncrypt.NCryptDecrypt(
                key,
                buffer,
                len(buffer),
                None,
                None,
                0,
                ctypes.byref(result_len),
                _NCRYPT_SILENT_FLAG,
            )
            if status != 0:
                raise AppBoundError(f"NCryptDecrypt failed: {status:#x}")
            output = (ctypes.c_ubyte * result_len.value)()
            status = ncrypt.NCryptDecrypt(
                key,
                buffer,
                len(buffer),
                None,
                output,
                result_len.value,
                ctypes.byref(result_len),
                _NCRYPT_SILENT_FLAG,
            )
            if status != 0:
                raise AppBoundError(f"NCryptDecrypt failed: {status:#x}")
            return bytes(output[: result_len.value])
        finally:
            ncrypt.NCryptFreeObject(key)
    finally:
        ncrypt.NCryptFreeObject(provider)


# -- key derivation ----------------------------------------------------------


def _derive_master_key(flag, iv, ciphertext, tag, encrypted_aes_key=None):
    if flag == 1:
        cipher = AES.new(_AES_KEY_V1, AES.MODE_GCM, nonce=iv)
    elif flag == 2:
        cipher = ChaCha20_Poly1305.new(key=_CHACHA20_KEY_V2, nonce=iv)
    elif flag == 3:
        with _SystemImpersonation():
            decrypted = _cng_decrypt(encrypted_aes_key)
        xored = bytes(a ^ b for a, b in zip(decrypted, _CNG_XOR_KEY_V3))
        cipher = AES.new(xored, AES.MODE_GCM, nonce=iv)
    else:
        raise AppBoundError(f"unsupported app-bound flag: {flag}")
    return cipher.decrypt_and_verify(ciphertext, tag)


def _unwrap_app_bound_content(content):
    """Turn the DPAPI-decrypted content into a 32-byte master key.

    Google-branded Chromium (Chrome, Edge) wraps the key with the elevation
    service's own key -- flagged 1/2/3 as the scheme changed across versions.
    Other builds (Brave and most forks) store the raw key, which is exactly
    32 bytes.
    """
    if len(content) == 32:
        return content
    flag = content[0]
    if flag in (1, 2) and len(content) == 61:
        return _derive_master_key(flag, content[1:13], content[13:45], content[45:61])
    if flag == 3 and len(content) == 93:
        return _derive_master_key(
            flag,
            content[33:45],
            content[45:77],
            content[77:93],
            encrypted_aes_key=content[1:33],
        )
    raise AppBoundError(
        f"unrecognised app-bound key content (flag {flag}, {len(content)} bytes)"
    )


def _read_app_bound_key(user_data_dir):
    with open(os.path.join(user_data_dir, "Local State"), encoding="utf-8") as handle:
        state = json.load(handle)
    encoded = (state.get("os_crypt") or {}).get("app_bound_encrypted_key")
    if not encoded:
        raise AppBoundError("no app-bound key in Local State")
    blob = base64.b64decode(encoded)
    if not blob.startswith(_APPB_PREFIX):
        raise AppBoundError("app-bound key has no APPB prefix")
    return blob[len(_APPB_PREFIX) :]


def master_key_for(user_data_dir):
    """Return the 32-byte app-bound master key for one browser install.

    Requires administrator rights: it briefly impersonates SYSTEM to unwrap
    the first DPAPI layer, then the logged-in user for the second.
    """
    system_wrapped = _read_app_bound_key(user_data_dir)
    with _SystemImpersonation():
        user_wrapped = _dpapi_unprotect(system_wrapped)
    blob = _dpapi_unprotect(user_wrapped)
    # DecryptData writes [size][validation_data][size][content]; the
    # validation_data carries the caller path and protection level.
    size1 = struct.unpack("<I", blob[0:4])[0]
    rest = blob[4 + size1 :]
    size2 = struct.unpack("<I", rest[0:4])[0]
    content = rest[4 : 4 + size2]
    return _unwrap_app_bound_content(content)


# -- cookie decryption and export --------------------------------------------


def decrypt_cookie_value(master_key, encrypted_value):
    """Decrypt one ``v20`` cookie value, or None when it is not v20."""
    if isinstance(encrypted_value, memoryview):
        encrypted_value = encrypted_value.tobytes()
    if not isinstance(encrypted_value, (bytes, bytearray)):
        encrypted_value = bytes(encrypted_value)
    if encrypted_value[:3] != b"v20":
        return None
    nonce = encrypted_value[3:15]
    ciphertext = encrypted_value[15:-16]
    tag = encrypted_value[-16:]
    cipher = AES.new(master_key, AES.MODE_GCM, nonce=nonce)
    plaintext = cipher.decrypt_and_verify(ciphertext, tag)
    return plaintext[32:].decode("utf-8", errors="replace")


def export_app_bound_cookies(user_data_dir, profile_dir, dest_path, require=None):
    """Decrypt a profile's v20 cookies into a Netscape file.

    ``require`` is an optional ``(name, domain_suffixes)`` pair where
    ``domain_suffixes`` is a string or iterable of suffixes; when given, a
    matching cookie must exist for the export to count as usable. Returns
    True when the file was written and matched, False when the browser had no
    matching cookie. Raises :class:`AppBoundError` on decryption failure.
    """
    master_key = master_key_for(user_data_dir)
    cookie_db = _find_cookie_db(profile_dir)
    with tempfile.TemporaryDirectory(prefix="blinddl_v20_") as tmpdir:
        copy_path = os.path.join(tmpdir, "cookies.sqlite")
        shutil.copy2(cookie_db, copy_path)
        connection = sqlite3.connect(
            "file:" + copy_path.replace("\\", "/") + "?mode=ro", uri=True
        )
        try:
            rows = connection.execute(
                "SELECT host_key, name, encrypted_value, path, expires_utc, "
                "is_secure, is_httponly FROM cookies"
            ).fetchall()
        finally:
            connection.close()

    require_name = None
    require_domains = ()
    if require is not None:
        require_name, require_domains = require
        if isinstance(require_domains, str):
            require_domains = (require_domains,)

    lines = ["# Netscape HTTP Cookie File", ""]
    matched = False
    for host, name, encrypted, path, expires, secure, httponly in rows:
        value = decrypt_cookie_value(master_key, encrypted)
        if value is None:
            continue
        if (
            require_name is not None
            and name == require_name
            and any(host.endswith(domain) for domain in require_domains)
        ):
            matched = True
        lines.append(_netscape_line(host, name, value, path, expires, secure, httponly))

    if require is not None and not matched:
        return False
    if len(lines) == 2:
        return False
    with open(dest_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines) + "\n")
    return True


def _find_cookie_db(profile_dir):
    for candidate in (
        os.path.join(profile_dir, "Network", "Cookies"),
        os.path.join(profile_dir, "Cookies"),
    ):
        if os.path.isfile(candidate):
            return candidate
    raise AppBoundError(f"no Cookies database in {profile_dir}")


def _netscape_line(host, name, value, path, expires, secure, httponly):
    include_subdomains = "TRUE" if host.startswith(".") else "FALSE"
    # Chromium stores microseconds since 1601-01-01; Netscape wants Unix
    # seconds since 1970-01-01.
    expires_s = "" if not expires else str(int(expires // 1000000) - 11644473600)
    return "\t".join(
        [
            host,
            include_subdomains,
            path or "/",
            "TRUE" if secure else "FALSE",
            expires_s,
            name,
            value,
        ]
    ) + ("\t#HttpOnly_" if httponly else "")


# -- elevated helper ----------------------------------------------------------

_SEE_MASK_NOCLOSEPROCESS = 0x40


def _helper_invocation(request_path):
    """Return ``(executable, parameters, directory)`` for the helper launch.

    A frozen build relaunches itself with ``--app-bound-export``; a source
    checkout relaunches ``python -m blinddl.app_bound --export`` from the
    repository root so the package resolves without a pip install.
    """
    if getattr(sys, "frozen", False):
        return (sys.executable, f'--app-bound-export "{request_path}"', None)
    package_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return (
        sys.executable,
        f'-m blinddl.app_bound --export "{request_path}"',
        package_root,
    )


def export_elevated(candidates, dest_path, require=None):
    """Elevate once and export app-bound cookies from the first match.

    ``candidates`` is a list of ``(label, user_data_dir, profile)`` for the
    app-bound browsers, in preference order. ``require`` is an optional
    ``(name, domain_suffixes)`` pair mirroring :func:`export_app_bound_cookies`.

    Returns ``(label, errors)``: ``label`` is the browser that matched, or
    None; ``errors`` lists per-browser failures for the caller to report.
    A single UAC prompt is the only visible side effect.
    """
    import win32api  # noqa: F401 - used for handle cleanup
    import win32com.shell.shell as shell
    import win32event

    request = {
        "out": dest_path,
        "require_name": require[0] if require else None,
        "require_domains": (
            list(require[1])
            if require and not isinstance(require[1], str)
            else ([require[1]] if require else None)
        ),
        "candidates": [
            {"label": label, "user_data_dir": user_data_dir, "profile": profile}
            for label, user_data_dir, profile in candidates
        ],
    }
    descriptor, request_path = tempfile.mkstemp(
        prefix="blinddl_app_bound_", suffix=".json"
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(request, stream)
        result_path = request_path + ".result"
        try:
            os.remove(result_path)
        except OSError:
            pass

        executable, parameters, directory = _helper_invocation(request_path)
        info = shell.ShellExecuteEx(
            lpVerb="runas",
            lpFile=executable,
            lpParameters=parameters,
            lpDirectory=directory,
            nShow=0,
            fMask=_SEE_MASK_NOCLOSEPROCESS,
        )
        process = info.get("hProcess") if isinstance(info, dict) else None
        if not process or (info.get("hInstApp") or 0) <= 32:
            return None, [
                "app-bound cookie export was cancelled or could not start (UAC prompt)"
            ]
        win32event.WaitForSingleObject(process, win32event.INFINITE)
        win32api.CloseHandle(process)
    finally:
        try:
            os.remove(request_path)
        except OSError:
            pass

    try:
        with open(result_path, encoding="utf-8") as stream:
            result = json.load(stream)
    except (OSError, ValueError):
        return None, ["app-bound cookie export failed (no result from the helper)"]
    finally:
        try:
            os.remove(result_path)
        except OSError:
            pass

    if result.get("matched"):
        return result["label"], []
    return None, result.get("errors") or ["app-bound cookie export failed"]


def _write_result(request_path, result):
    """Write the helper's outcome next to the request file; always exit 0."""
    with open(request_path + ".result", "w", encoding="utf-8") as stream:
        json.dump(result, stream)
    return 0


def _export_cli(request_path):
    """Body of the elevated helper: decrypt candidates, write the first match."""
    try:
        with open(request_path, encoding="utf-8") as stream:
            request = json.load(stream)
    except (OSError, ValueError) as exc:
        return _write_result(
            request_path,
            {"matched": False, "errors": [f"could not read the export request: {exc}"]},
        )

    out = request.get("out")
    require_name = request.get("require_name")
    require = None
    if require_name:
        require = (require_name, request.get("require_domains") or [])

    errors = []
    for candidate in request.get("candidates") or []:
        label = candidate.get("label", "browser")
        try:
            matched = export_app_bound_cookies(
                candidate["user_data_dir"], candidate["profile"], out, require=require
            )
        except Exception as exc:  # noqa: BLE001 - one browser failing is not fatal
            errors.append(f"{label}: {type(exc).__name__}: {exc}")
            continue
        if matched:
            return _write_result(request_path, {"matched": True, "label": label})
        if require:
            errors.append(f"{label}: no {require[0]} cookie")
        else:
            errors.append(f"{label}: no cookies found")
    return _write_result(request_path, {"matched": False, "errors": errors})


def main(argv=None):
    """CLI entry point for the elevated helper (``-m blinddl.app_bound``)."""
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) == 2 and args[0] == "--export":
        return _export_cli(args[1])
    sys.stderr.write("usage: python -m blinddl.app_bound --export <request.json>\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
