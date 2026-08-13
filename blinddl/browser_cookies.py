# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Export browser cookies for sites that need a sign-in.

yt-dlp's ``--cookies-from-browser`` only knows each browser's stock install
directory, so Brave Beta/Nightly, Opera GX and LibreWolf are invisible to it,
and it cannot decrypt the app-bound ("v20") cookies modern Chromium browsers
use. This module instead enumerates the browsers actually on the machine --
with their real profiles -- hands yt-dlp an explicit profile path for each
one, and writes Netscape-format cookie files (or single sign-in tokens such
as Deezer's ``arl``) from the first browser that has them.

App-bound browsers are decrypted too: when one is needed, blindDL relaunches
itself as a short-lived elevated helper (a single UAC prompt) that unwraps the
app-bound key and decrypts the cookies -- see :mod:`blinddl.app_bound`.
"""

import json
import os
import sys

from yt_dlp.cookies import extract_cookies_from_browser

# yt-dlp browser key -> human label.
_BROWSER_LABELS = {
    "brave": "Brave",
    "chrome": "Chrome",
    "chromium": "Chromium",
    "edge": "Edge",
    "firefox": "Firefox",
    "opera": "Opera",
    "vivaldi": "Vivaldi",
    "whale": "Naver Whale",
}


class CookieExportError(RuntimeError):
    """No browser could supply a valid Apple Music cookie export."""

    def __init__(self, errors):
        self.errors = list(errors)
        super().__init__("Could not export Apple Music cookies from any browser")


def _chromium_installs():
    """Yield ``(browser_key, label, user_data_dir)`` for Chromium browsers.

    Several browsers share a yt-dlp key ("brave", "opera") but ship as
    separate installs; the label tells the user which one actually supplied
    the cookies.
    """
    if sys.platform in ("win32", "cygwin"):
        local = os.environ.get("LOCALAPPDATA") or ""
        roaming = os.environ.get("APPDATA") or ""
        return [
            ("chrome", "Chrome", os.path.join(local, r"Google\Chrome\User Data")),
            ("chromium", "Chromium", os.path.join(local, r"Chromium\User Data")),
            ("edge", "Edge", os.path.join(local, r"Microsoft\Edge\User Data")),
            (
                "brave",
                "Brave",
                os.path.join(local, r"BraveSoftware\Brave-Browser\User Data"),
            ),
            (
                "brave",
                "Brave Beta",
                os.path.join(local, r"BraveSoftware\Brave-Browser-Beta\User Data"),
            ),
            (
                "brave",
                "Brave Nightly",
                os.path.join(local, r"BraveSoftware\Brave-Browser-Nightly\User Data"),
            ),
            ("vivaldi", "Vivaldi", os.path.join(local, r"Vivaldi\User Data")),
            ("opera", "Opera", os.path.join(roaming, r"Opera Software\Opera Stable")),
            (
                "opera",
                "Opera GX",
                os.path.join(roaming, r"Opera Software\Opera GX Stable"),
            ),
            (
                "whale",
                "Naver Whale",
                os.path.join(local, r"Naver\Naver Whale\User Data"),
            ),
        ]
    if sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
        return [
            ("chrome", "Chrome", os.path.join(base, "Google", "Chrome")),
            ("chromium", "Chromium", os.path.join(base, "Chromium")),
            ("edge", "Edge", os.path.join(base, "Microsoft Edge")),
            ("brave", "Brave", os.path.join(base, "BraveSoftware", "Brave-Browser")),
            (
                "brave",
                "Brave Beta",
                os.path.join(base, "BraveSoftware", "Brave-Browser-Beta"),
            ),
            (
                "brave",
                "Brave Nightly",
                os.path.join(base, "BraveSoftware", "Brave-Browser-Nightly"),
            ),
            ("vivaldi", "Vivaldi", os.path.join(base, "Vivaldi")),
            ("opera", "Opera", os.path.join(base, "com.operasoftware.Opera")),
            ("opera", "Opera GX", os.path.join(base, "com.operasoftware.OperaGX")),
            ("whale", "Naver Whale", os.path.join(base, "Naver", "Whale")),
        ]
    config = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return [
        ("chrome", "Chrome", os.path.join(config, "google-chrome")),
        ("chromium", "Chromium", os.path.join(config, "chromium")),
        ("edge", "Edge", os.path.join(config, "microsoft-edge")),
        ("brave", "Brave", os.path.join(config, "BraveSoftware", "Brave-Browser")),
        (
            "brave",
            "Brave Beta",
            os.path.join(config, "BraveSoftware", "Brave-Browser-Beta"),
        ),
        (
            "brave",
            "Brave Nightly",
            os.path.join(config, "BraveSoftware", "Brave-Browser-Nightly"),
        ),
        ("vivaldi", "Vivaldi", os.path.join(config, "vivaldi")),
        ("opera", "Opera", os.path.join(config, "opera")),
        ("opera", "Opera GX", os.path.join(config, "opera-gx")),
        ("whale", "Naver Whale", os.path.join(config, "naver-whale")),
    ]


def _firefox_installs():
    """Yield ``(label, profiles_root)`` for Firefox-family browsers."""
    if sys.platform in ("win32", "cygwin"):
        roaming = os.environ.get("APPDATA") or ""
        return [
            ("Firefox", os.path.join(roaming, "Mozilla", "Firefox", "Profiles")),
            ("LibreWolf", os.path.join(roaming, "LibreWolf", "Profiles")),
        ]
    if sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
        return [
            ("Firefox", os.path.join(base, "Firefox", "Profiles")),
            ("LibreWolf", os.path.join(base, "LibreWolf", "Profiles")),
        ]
    home = os.path.expanduser("~")
    return [
        ("Firefox", os.path.join(home, ".mozilla", "firefox")),
        ("LibreWolf", os.path.join(home, ".librewolf")),
    ]


def _has_cookies_file(directory):
    return os.path.isfile(os.path.join(directory, "Cookies")) or os.path.isfile(
        os.path.join(directory, "Network", "Cookies")
    )


def _chromium_candidates(user_data_dir):
    """Profile directories to hand yt-dlp for one Chromium install.

    Single-directory layouts (Opera and friends) keep ``Cookies`` at the top;
    profile-based layouts keep one per ``Default`` / ``Profile N`` subfolder.
    """
    if not os.path.isdir(user_data_dir):
        return []
    if _has_cookies_file(user_data_dir):
        return [user_data_dir]
    try:
        names = os.listdir(user_data_dir)
    except OSError:
        return []
    candidates = []
    for name in sorted(names, key=lambda value: (value != "Default", value.lower())):
        profile = os.path.join(user_data_dir, name)
        if os.path.isdir(profile) and _has_cookies_file(profile):
            candidates.append(profile)
    return candidates


def _firefox_candidates(profiles_root):
    if not os.path.isdir(profiles_root):
        return []
    try:
        names = os.listdir(profiles_root)
    except OSError:
        return []
    candidates = []
    for name in sorted(names, key=lambda value: (value != "default", value.lower())):
        profile = os.path.join(profiles_root, name)
        if os.path.isdir(profile) and os.path.isfile(
            os.path.join(profile, "cookies.sqlite")
        ):
            candidates.append(profile)
    return candidates


def candidate_browsers(preferred=None):
    """Yield ``(label, browser_name, profile, user_data_dir)`` per browser.

    ``profile`` is an absolute directory path, or ``None`` to let yt-dlp use
    its own default-path detection; ``user_data_dir`` is the Chromium install
    root (for ``Local State``) and is ``None`` for Firefox. ``preferred`` is a
    ``cookies_from_browser`` value to try first; when it names a browser that
    was not detected it is added as a fallback so the user's explicit choice
    is still honoured.
    """
    candidates = []

    for key, label, user_data_dir in _chromium_installs():
        for profile in _chromium_candidates(user_data_dir):
            if profile == user_data_dir:
                candidates.append((label, key, profile, user_data_dir))
            elif os.path.basename(profile) == "Default":
                candidates.append((label, key, profile, user_data_dir))
            else:
                candidates.append(
                    (
                        f"{label} ({os.path.basename(profile)})",
                        key,
                        profile,
                        user_data_dir,
                    )
                )

    for label, profiles_root in _firefox_installs():
        for profile in _firefox_candidates(profiles_root):
            candidates.append(
                (f"{label} ({os.path.basename(profile)})", "firefox", profile, None)
            )

    # yt-dlp's own default-path detection, so a browser installed somewhere
    # unusual still gets a chance. Skip keys we already enumerated.
    seen = {key for _label, key, _profile, _user_data in candidates}
    for key, label in _BROWSER_LABELS.items():
        if key not in seen:
            candidates.append((label, key, None, None))

    if preferred:
        matched = [c for c in candidates if c[1] == preferred]
        rest = [c for c in candidates if c[1] != preferred]
        candidates = matched + rest
        if not matched:
            label = _BROWSER_LABELS.get(preferred, preferred.title())
            candidates.insert(0, (label, preferred, None, None))
    return candidates


def _has_apple_music_token(jar):
    """True when the jar holds a ``media-user-token`` for music.apple.com."""
    for cookie in jar:
        if cookie.name == "media-user-token" and cookie.domain.endswith(
            "music.apple.com"
        ):
            return True
    return False


def _explain(exc):
    message = str(exc).strip()
    if "DPAPI" in message:
        return (
            "cookie decryption failed (app-bound encryption, or a different "
            "Windows account); export from Firefox instead"
        )
    return message or type(exc).__name__


def _uses_app_bound(browser_name, profile, user_data_dir):
    """True when a Chromium install's Local State advertises app-bound crypto.

    App-bound ("v20") cookies are decrypted by the elevated helper, so this
    only decides *how* a browser is read, not whether it is skipped.
    """
    if browser_name == "firefox" or not user_data_dir:
        return False
    try:
        with open(
            os.path.join(user_data_dir, "Local State"), encoding="utf-8"
        ) as handle:
            state = json.load(handle)
    except (OSError, ValueError):
        return False
    return "app_bound_encrypted_key" in (state.get("os_crypt") or {})


def _elevate(candidates, dest_path, require, errors):
    """Elevate once to export app-bound cookies; append errors, return label.

    ``candidates`` is a list of ``(label, user_data_dir, profile)`` in
    preference order. Returns the matching browser's label, or None when no
    app-bound browser matched (or elevation is unavailable).
    """
    if sys.platform not in ("win32", "cygwin"):
        errors.append(
            "app-bound cookie encryption (unsupported on this platform); "
            "export from Firefox instead"
        )
        return None
    try:
        from . import app_bound
    except ImportError as exc:
        errors.append(f"app-bound cookie decryption is unavailable: {exc}")
        return None
    label, app_errors = app_bound.export_elevated(
        candidates, dest_path, require=require
    )
    errors.extend(app_errors)
    return label


def export_cookies(dest_path, preferred=None, needs=None, why=None, require=None):
    """Extract cookies into ``dest_path`` (Netscape format) and return a label.

    Tries every detected browser in turn and keeps the first export that
    satisfies ``needs`` (an optional ``callable(jar) -> bool``); when it is
    None, the first browser with any cookies wins. ``why`` is the per-browser
    note recorded when a jar fails ``needs``. ``require`` is an optional
    ``(name, domain_suffixes)`` pair handed to the app-bound helper so it can
    skip browsers that decrypt but lack the cookie. Raises
    :class:`CookieExportError` with a per-browser ``errors`` list when none
    works.
    """
    errors = []
    app_bound = []
    for label, browser_name, profile, user_data_dir in candidate_browsers(preferred):
        if _uses_app_bound(browser_name, profile, user_data_dir):
            app_bound.append((label, user_data_dir, profile))
            continue
        try:
            jar = extract_cookies_from_browser(browser_name, profile)
        except Exception as exc:  # noqa: BLE001 - report per browser, keep going
            errors.append(f"{label}: {_explain(exc)}")
            continue
        if needs is not None and not needs(jar):
            errors.append(f"{label}: {why or 'missing the required cookie'}")
            continue
        if not list(jar):
            errors.append(f"{label}: no cookies found")
            continue
        jar.save(dest_path)
        return label

    if app_bound:
        label = _elevate(app_bound, dest_path, require, errors)
        if label:
            return label
    raise CookieExportError(errors)


def export_apple_music_cookies(dest_path, preferred=None):
    """Extract Apple Music cookies into ``dest_path`` (Netscape format).

    Keeps only exports that carry a ``media-user-token`` for
    ``music.apple.com``.
    """
    return export_cookies(
        dest_path,
        preferred=preferred,
        needs=_has_apple_music_token,
        why="no media-user-token for music.apple.com",
        require=("media-user-token", ("music.apple.com",)),
    )


def extract_cookie_value(name, domains, preferred=None):
    """Return ``(value, label)`` for the first browser with a matching cookie.

    ``domains`` are domain suffixes matched against each cookie's domain
    (``cookie.domain.endswith(suffix)``). Raises :class:`CookieExportError`
    when no browser carries the cookie -- used for single-token sign-ins like
    Deezer's ``arl``.
    """
    errors = []
    app_bound = []
    for label, browser_name, profile, user_data_dir in candidate_browsers(preferred):
        if _uses_app_bound(browser_name, profile, user_data_dir):
            app_bound.append((label, user_data_dir, profile))
            continue
        try:
            jar = extract_cookies_from_browser(browser_name, profile)
        except Exception as exc:  # noqa: BLE001 - report per browser, keep going
            errors.append(f"{label}: {_explain(exc)}")
            continue
        for cookie in jar:
            if cookie.name == name and any(
                cookie.domain.endswith(domain) for domain in domains
            ):
                return cookie.value, label
        errors.append(f"{label}: no {name} cookie for {', '.join(domains)}")

    if app_bound:
        value, label = _extract_app_bound_value(app_bound, name, domains, errors)
        if value is not None:
            return value, label
    raise CookieExportError(errors)


def _extract_app_bound_value(candidates, name, domains, errors):
    """Return ``(value, label)`` for an app-bound browser's matching cookie."""
    import tempfile
    from http.cookiejar import MozillaCookieJar

    descriptor, tmp_path = tempfile.mkstemp(suffix=".txt", prefix="blinddl_arl_")
    os.close(descriptor)
    try:
        label = _elevate(candidates, tmp_path, (name, domains), errors)
        if label is None:
            return None, None
        jar = MozillaCookieJar(tmp_path)
        jar.load(ignore_discard=True, ignore_expires=True)
        for cookie in jar:
            if cookie.name == name and any(
                cookie.domain.endswith(domain) for domain in domains
            ):
                return cookie.value, label
        errors.append(f"{label}: no {name} cookie for {', '.join(domains)}")
        return None, None
    except (OSError, ValueError) as exc:  # noqa: BLE001 - malformed export
        errors.append(f"app-bound cookie export could not be read: {exc}")
        return None, None
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
