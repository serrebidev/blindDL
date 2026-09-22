# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""A configurable global hotkey that shows or hides the main window.

Windows+B used to be the documented way back to a hidden blindDL, but on
Windows 11 that combination belongs to the system: it moves focus to the
notification area and can never reach an application. So blindDL registers
its own hotkey instead -- Ctrl+Alt+B unless the user picks something else in
Settings, Interface -- through RegisterHotKey, which only exists on
Windows. Everything in here except new_hotkey_window() is plain Python so
it can be tested anywhere; the window itself is only ever built where
supported() is true.
"""

import sys

# RegisterHotKey modifier flags.
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004

# The id handed to RegisterHotKey. One is enough: there is only ever one
# global hotkey.
_HOTKEY_ID = 0xB11D

# First virtual-key code of the F-keys.
_VK_F1 = 0x70

_MODIFIER_NAMES = {
    "ctrl": MOD_CONTROL,
    "control": MOD_CONTROL,
    "alt": MOD_ALT,
    "shift": MOD_SHIFT,
}


def supported():
    """Whether this platform can host a global hotkey at all."""
    return sys.platform == "win32"


def parse_hotkey(text):
    """Split "Ctrl+Alt+B" into a (modifiers, virtual-key) pair.

    Raises ValueError carrying a message fit for a message box when the
    text is not a usable hotkey.
    """
    parts = [part.strip().lower() for part in str(text or "").split("+")]
    parts = [part for part in parts if part]
    if not parts:
        raise ValueError(
            "Type a hotkey such as Ctrl+Alt+B, or clear the box to switch "
            "the hotkey off."
        )
    *modifier_names, key_name = parts
    modifiers = 0
    for name in modifier_names:
        if name == "win":
            raise ValueError(
                "The Windows key cannot be used here: Windows keeps "
                "Win+letter combinations for itself, so it never hands them "
                "to an application."
            )
        flag = _MODIFIER_NAMES.get(name)
        if flag is None:
            raise ValueError(
                f"Unknown modifier {name!r}. Use Ctrl, Alt and Shift, then "
                "one key -- for example Ctrl+Alt+B."
            )
        modifiers |= flag
    if not modifier_names:
        raise ValueError(
            "A global hotkey needs at least one of Ctrl, Alt or Shift: a "
            "bare key would fire while typing."
        )
    vk = _key_to_vk(key_name)
    if vk is None:
        raise ValueError(
            f"Unknown key {key_name!r}. Use a letter, a digit or an F-key -- "
            "for example Ctrl+Alt+B or Ctrl+Shift+F9."
        )
    return modifiers, vk


def _key_to_vk(name):
    if len(name) == 1:
        upper = name.upper()
        if "A" <= upper <= "Z" or "0" <= upper <= "9":
            return ord(upper)
        return None
    if name.startswith("f"):
        try:
            number = int(name[1:])
        except ValueError:
            return None
        if 1 <= number <= 24:
            return _VK_F1 + number - 1
    return None


def format_hotkey(modifiers, vk):
    """Turn a (modifiers, virtual-key) pair back into "Ctrl+Alt+B"."""
    names = []
    if modifiers & MOD_CONTROL:
        names.append("Ctrl")
    if modifiers & MOD_ALT:
        names.append("Alt")
    if modifiers & MOD_SHIFT:
        names.append("Shift")
    names.append(_vk_to_key(vk))
    return "+".join(names)


def _vk_to_key(vk):
    if 0x41 <= vk <= 0x5A or 0x30 <= vk <= 0x39:
        return chr(vk)
    if _VK_F1 <= vk < _VK_F1 + 24:
        return f"F{vk - _VK_F1 + 1}"
    return f"key {vk:#x}"


def new_hotkey_window(on_press):
    """Build the hidden window that owns the hotkey registration.

    Only call this where supported() is true. RegisterHotKey delivers
    WM_HOTKEY to a window, so the window overrides MSWWindowProc to catch
    it; wx.Frame is hidden at birth and never shown.
    """
    import wx  # noqa: PLC0415 - Windows-only, and only when actually used

    class _HotkeyWindow(wx.Frame):
        WM_HOTKEY = 0x0312

        def __init__(self):
            super().__init__(None, size=(0, 0))
            self.Hide()
            self._registered = False

        def MSWWindowProc(self, hwnd, msg, wparam, lparam):
            if msg == self.WM_HOTKEY and int(wparam) == _HOTKEY_ID:
                on_press()
                return 0
            return super().MSWWindowProc(hwnd, msg, wparam, lparam)

        def register(self, text):
            """(Re)register the hotkey described by text.

            An empty text unregisters. Returns True when the hotkey is
            registered, or when there is nothing to register. Raises
            ValueError for text parse_hotkey() rejects.
            """
            import ctypes  # noqa: PLC0415 - Windows-only

            self.unregister()
            if not str(text or "").strip():
                return True
            modifiers, vk = parse_hotkey(text)
            user32 = ctypes.windll.user32
            self._registered = bool(
                user32.RegisterHotKey(self.GetHandle(), _HOTKEY_ID, modifiers, vk)
            )
            return self._registered

        def unregister(self):
            if self._registered:
                import ctypes  # noqa: PLC0415 - Windows-only

                ctypes.windll.user32.UnregisterHotKey(
                    self.GetHandle(), _HOTKEY_ID
                )
                self._registered = False

        def destroy(self):
            self.unregister()
            self.Destroy()

    return _HotkeyWindow()
