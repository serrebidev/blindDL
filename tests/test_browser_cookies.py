# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from blinddl import browser_cookies


def _cookie(name, domain):
    return SimpleNamespace(name=name, domain=domain)


class BrowserCookiesTests(unittest.TestCase):
    def test_has_apple_music_token(self):
        jar = [_cookie("itspod", ".music.apple.com")]
        self.assertFalse(browser_cookies._has_apple_music_token(jar))

        jar.append(_cookie("media-user-token", ".music.apple.com"))
        self.assertTrue(browser_cookies._has_apple_music_token(jar))

    def test_export_writes_first_browser_with_token(self):
        jar_without = mock.Mock()
        jar_with = mock.Mock()
        with (
            mock.patch.object(
                browser_cookies,
                "candidate_browsers",
                return_value=[
                    ("Chrome", "chrome", "/x/Default"),
                    ("Firefox (p)", "firefox", "/y/p"),
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
        jar_with.save.assert_called_once_with("/out.txt")

    def test_export_explains_dpapi_failure_and_raises(self):
        with (
            mock.patch.object(
                browser_cookies,
                "candidate_browsers",
                return_value=[("Brave Beta", "brave", "/x/Default")],
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
            (label, name, prof)
            for label, name, prof in candidates
            if name in ("brave", "firefox")
        }
        self.assertIn(("Brave", "brave", default), detected)
        self.assertIn(("Firefox (abc.default-release)", "firefox", profile), detected)

    def test_preferred_browser_is_tried_first(self):
        with (
            mock.patch.object(browser_cookies, "_chromium_installs", return_value=[]),
            mock.patch.object(browser_cookies, "_firefox_installs", return_value=[]),
        ):
            candidates = browser_cookies.candidate_browsers(preferred="firefox")

        self.assertEqual(candidates[0], ("Firefox", "firefox", None))

    def test_uses_app_bound_reads_local_state(self):
        import json

        with tempfile.TemporaryDirectory() as tmp:
            user_data = os.path.join(tmp, "User Data")
            profile = os.path.join(user_data, "Default")
            os.makedirs(profile)

            # No Local State yet -> not app-bound.
            self.assertFalse(browser_cookies._uses_app_bound("brave", profile))

            with open(
                os.path.join(user_data, "Local State"), "w", encoding="utf-8"
            ) as handle:
                json.dump({"os_crypt": {"encrypted_key": "x"}}, handle)
            self.assertFalse(browser_cookies._uses_app_bound("brave", profile))

            with open(
                os.path.join(user_data, "Local State"), "w", encoding="utf-8"
            ) as handle:
                json.dump({"os_crypt": {"app_bound_encrypted_key": "APPB..."}}, handle)
            self.assertTrue(browser_cookies._uses_app_bound("brave", profile))

        # Firefox cookies are never app-bound encrypted.
        self.assertFalse(browser_cookies._uses_app_bound("firefox", "/some/profile"))

    def test_export_skips_app_bound_browsers_with_a_hint(self):
        with (
            mock.patch.object(
                browser_cookies,
                "candidate_browsers",
                return_value=[("Brave Beta", "brave", "/x/Default")],
            ),
            mock.patch.object(browser_cookies, "_uses_app_bound", return_value=True),
            mock.patch.object(
                browser_cookies, "extract_cookies_from_browser"
            ) as extract,
        ):
            with self.assertRaises(browser_cookies.CookieExportError) as ctx:
                browser_cookies.export_apple_music_cookies("/out.txt")

        extract.assert_not_called()
        self.assertIn("app-bound cookie encryption", ctx.exception.errors[0])
        self.assertIn("Firefox", ctx.exception.errors[0])


if __name__ == "__main__":
    unittest.main()
