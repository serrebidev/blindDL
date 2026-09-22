# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

import unittest

from blinddl.global_hotkey import (
    MOD_ALT,
    MOD_CONTROL,
    MOD_SHIFT,
    format_hotkey,
    parse_hotkey,
)


class ParseHotkeyTests(unittest.TestCase):
    def test_default_hotkey(self):
        modifiers, vk = parse_hotkey("Ctrl+Alt+B")
        self.assertEqual(modifiers, MOD_CONTROL | MOD_ALT)
        self.assertEqual(vk, ord("B"))

    def test_case_and_spacing_do_not_matter(self):
        self.assertEqual(
            parse_hotkey("  ctrl + alt + b "), parse_hotkey("Ctrl+Alt+B")
        )

    def test_control_alias_and_function_key(self):
        modifiers, vk = parse_hotkey("Control+Shift+F9")
        self.assertEqual(modifiers, MOD_CONTROL | MOD_SHIFT)
        self.assertEqual(vk, 0x70 + 8)

    def test_digit_key(self):
        modifiers, vk = parse_hotkey("Alt+5")
        self.assertEqual(modifiers, MOD_ALT)
        self.assertEqual(vk, ord("5"))

    def test_round_trip(self):
        for text in ("Ctrl+Alt+B", "Ctrl+Shift+F12", "Alt+X", "Shift+0"):
            modifiers, vk = parse_hotkey(text)
            self.assertEqual(format_hotkey(modifiers, vk), text)

    def test_empty_means_disabled(self):
        with self.assertRaises(ValueError):
            parse_hotkey("")
        with self.assertRaises(ValueError):
            parse_hotkey("   ")

    def test_bare_key_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_hotkey("B")

    def test_windows_key_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_hotkey("Win+B")

    def test_unknown_modifier_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_hotkey("Cmd+B")

    def test_unknown_key_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_hotkey("Ctrl+Alt+PrintScreen")


if __name__ == "__main__":
    unittest.main()
