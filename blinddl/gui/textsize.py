# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Text size for every window, for a screen that is read rather than heard.

blindDL is built for the ear, but not everyone using it is listening only:
some read the results, the details and the player's labels, and a list set
at twelve points is a list read through a magnifier. Every window can be
made larger from the View menu or with Ctrl+Plus, and the size is saved.

Each control's own size is read off the platform once and kept on the
control, so growing and shrinking always work from where the platform
started rather than compounding one change onto the last -- a control that
went up 30 percent five times would otherwise end up four times its size
with no way back.
"""

import wx

# What Ctrl+Plus and Ctrl+Minus step through. Below normal is offered as
# well: an oversized setting is easy to land in by holding the key down,
# and getting back should not mean editing a settings file.
STEPS = (0.8, 0.9, 1.0, 1.15, 1.3, 1.5, 1.75, 2.0)
DEFAULT = 1.0

# Where a control's platform font size is remembered between changes.
_BASE_ATTRIBUTE = "_blinddl_base_point_size"


def nearest_step(value):
    """The offered size closest to *value*, for a hand-edited setting."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return DEFAULT
    return min(STEPS, key=lambda step: abs(step - value))


def next_step(current, direction=1):
    """The next size up (or down) from *current*, stopping at either end."""
    index = STEPS.index(nearest_step(current)) + (1 if direction >= 0 else -1)
    return STEPS[max(0, min(len(STEPS) - 1, index))]


def describe(scale):
    """The size the way it would be read out."""
    return f"{int(round(nearest_step(scale) * 100))} percent"


def scale_for(config):
    """The saved text size, as one of the offered steps."""
    getter = getattr(config, "get", None)
    if getter is None:
        return DEFAULT
    return nearest_step(getter("text_scale", DEFAULT))


def fit(window, config=None):
    """Apply the saved text size to a window that has just been built.

    The config is passed when the caller has one, and otherwise found by
    walking outwards from the window to whatever opened it -- which is the
    main window, with its config in it.
    """
    if config is None:
        config = _config_of(window)
    return apply(window, scale_for(config))


def install():
    """Make every dialog opened from now on match the saved text size.

    A dozen dialogs with no shared base class means a dozen places to
    remember, and the one that gets forgotten is the one somebody cannot
    read. They all make the same call on the way in -- ShowModal -- so
    that is where this happens instead of in each of them. A dialog is
    fitted after it is built and immediately before it appears, which is
    also the only moment every control in it exists.
    """
    if getattr(wx.Dialog, "_blinddl_text_size_installed", False):
        return
    showing = wx.Dialog.ShowModal

    def ShowModal(self, *args, **kwargs):
        try:
            fit(self)
        except Exception:  # noqa: BLE001 - a dialog must open regardless
            pass
        return showing(self, *args, **kwargs)

    wx.Dialog.ShowModal = ShowModal
    wx.Dialog._blinddl_text_size_installed = True


def _config_of(window):
    """The nearest config up the parent chain from *window*, or None."""
    for _step in range(20):
        if window is None:
            return None
        for candidate in (window, getattr(window, "frame", None)):
            config = getattr(candidate, "config", None)
            if hasattr(config, "get"):
                return config
        window = (window.GetParent()
                  if hasattr(window, "GetParent") else None)
    return None


def apply(window, scale):
    """Give *window* and everything inside it the scaled text size."""
    scale = nearest_step(scale)
    controls = _controls(window)
    # Every control's own size is read before anything is resized. On GTK a
    # child with no font of its own reports its parent's, so resizing as we
    # walk would have the next control down take the size just given to the
    # one above it as its starting point -- and a window would grow by a
    # factor of itself once for every level of nesting.
    for control in controls:
        if getattr(control, _BASE_ATTRIBUTE, None) is not None:
            continue
        font = control.GetFont()
        if font is None or not font.IsOk():
            continue
        setattr(control, _BASE_ATTRIBUTE, font.GetPointSize())
    for control in controls:
        base = getattr(control, _BASE_ATTRIBUTE, None)
        if base is None:
            continue
        font = control.GetFont()
        if font is None or not font.IsOk():
            continue
        # A platform font carries more than its size -- the face, the
        # weight, whether it is italic -- so the size is changed on a copy
        # of it rather than a new font being made.
        scaled = wx.Font(font)
        scaled.SetPointSize(max(6, int(round(base * scale))))
        if scaled.GetPointSize() != font.GetPointSize():
            control.SetFont(scaled)
            control.Refresh()
    return scale


def _controls(window):
    """Every window under *window*, itself included."""
    found = [window]
    for child in window.GetChildren():
        found.extend(_controls(child))
    return found


def apply_everywhere(scale):
    """Resize every window blindDL has open, dialogs included."""
    for window in wx.GetTopLevelWindows():
        try:
            apply(window, scale)
        except RuntimeError:
            # A window that is being destroyed is not worth resizing.
            continue
