# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

import os
import unittest
from unittest import mock

from Crypto.Cipher import AES, ChaCha20_Poly1305

from blinddl import app_bound


def _aes_gcm_pack(key, nonce, plaintext):
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    ciphertext, tag = cipher.encrypt_and_digest(plaintext)
    return ciphertext, tag


class AppBoundKeyTests(unittest.TestCase):
    def test_unwrap_returns_raw_32_byte_key(self):
        raw = os.urandom(32)
        self.assertEqual(app_bound._unwrap_app_bound_content(raw), raw)

    def test_unwrap_flag_1_aes_gcm(self):
        master = os.urandom(32)
        nonce = os.urandom(12)
        ciphertext, tag = _aes_gcm_pack(app_bound._AES_KEY_V1, nonce, master)
        content = b"\x01" + nonce + ciphertext + tag
        self.assertEqual(app_bound._unwrap_app_bound_content(content), master)

    def test_unwrap_flag_2_chacha20(self):
        master = os.urandom(32)
        nonce = os.urandom(12)
        cipher = ChaCha20_Poly1305.new(key=app_bound._CHACHA20_KEY_V2, nonce=nonce)
        ciphertext, tag = cipher.encrypt_and_digest(master)
        content = b"\x02" + nonce + ciphertext + tag
        self.assertEqual(app_bound._unwrap_app_bound_content(content), master)

    def test_unwrap_flag_3_cng_key(self):
        master = os.urandom(32)
        nonce = os.urandom(12)
        decrypted = os.urandom(32)
        aes_key = bytes(a ^ b for a, b in zip(decrypted, app_bound._CNG_XOR_KEY_V3))
        ciphertext, tag = _aes_gcm_pack(aes_key, nonce, master)
        content = b"\x03" + decrypted + nonce + ciphertext + tag
        with (
            mock.patch.object(app_bound, "_SystemImpersonation"),
            mock.patch.object(app_bound, "_cng_decrypt", return_value=decrypted),
        ):
            self.assertEqual(app_bound._unwrap_app_bound_content(content), master)

    def test_unwrap_rejects_unknown_content(self):
        with self.assertRaises(app_bound.AppBoundError):
            app_bound._unwrap_app_bound_content(b"\x09" + os.urandom(64))


class AppBoundCookieTests(unittest.TestCase):
    def test_decrypt_v20_cookie_value(self):
        master = os.urandom(32)
        nonce = os.urandom(12)
        plaintext = os.urandom(32) + b"the-cookie-value"
        ciphertext, tag = _aes_gcm_pack(master, nonce, plaintext)
        encrypted = b"v20" + nonce + ciphertext + tag
        self.assertEqual(
            app_bound.decrypt_cookie_value(master, encrypted), "the-cookie-value"
        )

    def test_decrypt_returns_none_for_legacy_values(self):
        master = os.urandom(32)
        # A v10 (DPAPI) blob never starts with the v20 marker.
        self.assertIsNone(
            app_bound.decrypt_cookie_value(master, b"v10" + os.urandom(20))
        )

    def test_netscape_line_format(self):
        line = app_bound._netscape_line(
            ".music.apple.com",
            "media-user-token",
            "abc",
            "/",
            1337000000000000,
            True,
            True,
        )
        fields = line.split("\t")
        self.assertEqual(fields[0], ".music.apple.com")
        self.assertEqual(fields[1], "TRUE")
        self.assertEqual(fields[5], "media-user-token")
        self.assertEqual(fields[6], "abc")
        self.assertEqual(fields[7], "#HttpOnly_")


if __name__ == "__main__":
    unittest.main()
