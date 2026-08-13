# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from blinddl import app_bound as app_bound_module
from blinddl import browser_cookies


def _cookie(name, domain, value="v"):
    return SimpleNamespace(name=name, domain=domain, value=value)


class _FakeJar:
    """A cookie jar stand-in that iterates its cookies and records saves."""

    def __init__(self, *cookies):
        self.cookies = list(cookies)
        self.saved = None

    def __iter__(self):
        return iter(self.cookies)

    def save(self, path):
        self.saved = path


class BrowserCookiesTests(unittest.TestCase):
    def test_has_apple_music_token(self):
        jar = [_cookie("itspod", ".music.apple.com")]
        self.assertFalse(browser_cookies._has_apple_music_token(jar))

        jar.append(_cookie("media-user-token", ".music.apple.com"))
        self.assertTrue(browser_cookies._has_apple_music_token(jar))

    def test_export_writes_first_browser_with_token(self):
        jar_without = _FakeJar(_cookie("itspod", ".music.apple.com"))
        jar_with = _FakeJar(_cookie("media-user-token", ".music.apple.com"))
        with (
            mock.patch.object(
                browser_cookies,
                "candidate_browsers",
                return_value=[
                    ("Chrome", "chrome", "/x/Default", None),
                    ("Firefox (p)", "firefox", "/y/p", None),
                ],
            ),
            mock.patch.object(
                browser_cookies,
                "extract_cookies_from_browser",
                side_effect=[jar_without, jar_with],
            ),
            mock.patch.object(
                browser_cookies,
                "_has_apple_music_token",
                side_effect=[False, True],
            ),
        ):
            label = browser_cookies.export_apple_music_cookies(
                "/out.txt", preferred="firefox"
            )

        self.assertEqual(label, "Firefox (p)")
        self.assertEqual(jar_with.saved, "/out.txt")

    def test_export_explains_dpapi_failure_and_raises(self):
        with (
            mock.patch.object(
                browser_cookies,
                "candidate_browsers",
                return_value=[("Brave Beta", "brave", "/x/Default", None)],
            ),
            mock.patch.object(
                browser_cookies,
                "extract_cookies_from_browser",
                side_effect=RuntimeError("Failed to decrypt with DPAPI. See ..."),
            ),
        ):
            with self.assertRaises(browser_cookies.CookieExportError) as ctx:
                browser_cookies.export_apple_music_cookies("/out.txt")

        errors = ctx.exception.errors
        self.assertEqual(len(errors), 1)
        self.assertIn("Brave Beta", errors[0])
        self.assertIn("export from Firefox", errors[0])

    def test_detects_installed_profiles(self):
        with tempfile.TemporaryDirectory() as tmp:
            brave = os.path.join(tmp, "Brave", "User Data")
            default = os.path.join(brave, "Default")
            os.makedirs(os.path.join(default, "Network"))
            open(os.path.join(default, "Network", "Cookies"), "w").close()

            firefox_root = os.path.join(tmp, "Firefox")
            profile = os.path.join(firefox_root, "abc.default-release")
            os.makedirs(profile)
            open(os.path.join(profile, "cookies.sqlite"), "w").close()

            with (
                mock.patch.object(
                    browser_cookies,
                    "_chromium_installs",
                    return_value=[("brave", "Brave", brave)],
                ),
                mock.patch.object(
                    browser_cookies,
                    "_firefox_installs",
                    return_value=[("Firefox", firefox_root)],
                ),
            ):
                candidates = browser_cookies.candidate_browsers()

        detected = {
            (label, name, prof, ud)
            for label, name, prof, ud in candidates
            if name in ("brave", "firefox")
        }
        self.assertIn(("Brave", "brave", default, brave), detected)
        self.assertIn(
            ("Firefox (abc.default-release)", "firefox", profile, None), detected
        )

    def test_preferred_browser_is_tried_first(self):
        with (
            mock.patch.object(browser_cookies, "_chromium_installs", return_value=[]),
            mock.patch.object(browser_cookies, "_firefox_installs", return_value=[]),
        ):
            candidates = browser_cookies.candidate_browsers(preferred="firefox")

        self.assertEqual(candidates[0], ("Firefox", "firefox", None, None))

    def test_uses_app_bound_reads_local_state(self):
        import json

        with tempfile.TemporaryDirectory() as tmp:
            user_data = os.path.join(tmp, "User Data")
            profile = os.path.join(user_data, "Default")
            os.makedirs(profile)

            # No Local State yet -> not app-bound.
            self.assertFalse(
                browser_cookies._uses_app_bound("brave", profile, user_data)
            )

            with open(
                os.path.join(user_data, "Local State"), "w", encoding="utf-8"
            ) as handle:
                json.dump({"os_crypt": {"encrypted_key": "x"}}, handle)
            self.assertFalse(
                browser_cookies._uses_app_bound("brave", profile, user_data)
            )

            with open(
                os.path.join(user_data, "Local State"), "w", encoding="utf-8"
            ) as handle:
                json.dump({"os_crypt": {"app_bound_encrypted_key": "APPB..."}}, handle)
            self.assertTrue(
                browser_cookies._uses_app_bound("brave", profile, user_data)
            )

        # Firefox cookies are never app-bound encrypted.
        self.assertFalse(
            browser_cookies._uses_app_bound("firefox", "/some/profile", None)
        )

    def test_export_elevates_for_app_bound_browsers(self):
        candidate = ("Brave Beta", "brave", "/x/Default", "/x")
        with (
            mock.patch.object(
                browser_cookies,
                "candidate_browsers",
                return_value=[candidate],
            ),
            mock.patch.object(browser_cookies, "_uses_app_bound", return_value=True),
            mock.patch.object(
                browser_cookies, "extract_cookies_from_browser"
            ) as extract,
            mock.patch.object(
                browser_cookies, "_elevate", return_value="Brave Beta"
            ) as elevate,
        ):
            label = browser_cookies.export_apple_music_cookies("/out.txt")

        extract.assert_not_called()
        elevate.assert_called_once()
        args, kwargs = elevate.call_args
        self.assertEqual(args[0], [("Brave Beta", "/x", "/x/Default")])
        self.assertEqual(args[2], ("media-user-token", ("music.apple.com",)))
        self.assertEqual(kwargs, {})
        self.assertEqual(label, "Brave Beta")

    def test_export_reports_app_bound_failure(self):
        def fake_elevate(candidates, dest_path, require, errors):
            errors.append("Brave Beta: no media-user-token cookie")
            return None

        with (
            mock.patch.object(
                browser_cookies,
                "candidate_browsers",
                return_value=[("Brave Beta", "brave", "/x/Default", "/x")],
            ),
            mock.patch.object(browser_cookies, "_uses_app_bound", return_value=True),
            mock.patch.object(browser_cookies, "_elevate", side_effect=fake_elevate),
        ):
            with self.assertRaises(browser_cookies.CookieExportError) as ctx:
                browser_cookies.export_apple_music_cookies("/out.txt")

        # The elevated helper's per-browser errors flow into the exception.
        self.assertIn("no media-user-token cookie", ctx.exception.errors[0])

    def test_elevate_calls_the_app_bound_helper(self):
        errors = []
        with mock.patch.object(browser_cookies, "sys") as sys_mod:
            sys_mod.platform = "win32"
            with mock.patch.object(
                app_bound_module,
                "export_elevated",
                return_value=("Edge", ["Edge: boom"]),
            ) as export:
                label = browser_cookies._elevate(
                    [("Edge", "/ud", "/ud/Default")],
                    "/out.txt",
                    ("media-user-token", ("music.apple.com",)),
                    errors,
                )

        self.assertEqual(label, "Edge")
        self.assertEqual(errors, ["Edge: boom"])
        export.assert_called_once_with(
            [("Edge", "/ud", "/ud/Default")],
            "/out.txt",
            require=("media-user-token", ("music.apple.com",)),
        )

    def test_elevate_is_unavailable_off_windows(self):
        errors = []
        with mock.patch.object(browser_cookies, "sys") as sys_mod:
            sys_mod.platform = "darwin"
            label = browser_cookies._elevate(
                [("Edge", "/ud", "/ud/Default")], "/out.txt", None, errors
            )

        self.assertIsNone(label)
        self.assertIn("export from Firefox", errors[0])

    def test_export_cookies_accepts_any_cookies_without_needs(self):
        jar = _FakeJar(_cookie("SID", ".youtube.com"))
        with (
            mock.patch.object(
                browser_cookies,
                "candidate_browsers",
                return_value=[("Firefox", "firefox", "/y/p", None)],
            ),
            mock.patch.object(browser_cookies, "_uses_app_bound", return_value=False),
            mock.patch.object(
                browser_cookies, "extract_cookies_from_browser", return_value=jar
            ),
        ):
            label = browser_cookies.export_cookies("/out.txt")

        self.assertEqual(label, "Firefox")
        self.assertEqual(jar.saved, "/out.txt")

    def test_extract_cookie_value_returns_matching_cookie(self):
        jar = _FakeJar(
            _cookie("sid", ".deezer.com"),
            _cookie("arl", ".deezer.com", "the-arl-value"),
        )
        with (
            mock.patch.object(
                browser_cookies,
                "candidate_browsers",
                return_value=[("Firefox", "firefox", "/y/p", None)],
            ),
            mock.patch.object(browser_cookies, "_uses_app_bound", return_value=False),
            mock.patch.object(
                browser_cookies, "extract_cookies_from_browser", return_value=jar
            ),
        ):
            value, label = browser_cookies.extract_cookie_value("arl", ["deezer.com"])

        self.assertEqual(label, "Firefox")
        self.assertEqual(value, "the-arl-value")

    def test_extract_cookie_value_raises_when_missing(self):
        jar = _FakeJar(_cookie("sid", ".deezer.com"))
        with (
            mock.patch.object(
                browser_cookies,
                "candidate_browsers",
                return_value=[("Firefox", "firefox", "/y/p", None)],
            ),
            mock.patch.object(browser_cookies, "_uses_app_bound", return_value=False),
            mock.patch.object(
                browser_cookies, "extract_cookies_from_browser", return_value=jar
            ),
        ):
            with self.assertRaises(browser_cookies.CookieExportError) as ctx:
                browser_cookies.extract_cookie_value("arl", ["deezer.com"])

        self.assertIn("no arl cookie", ctx.exception.errors[0])


if __name__ == "__main__":
    unittest.main()
