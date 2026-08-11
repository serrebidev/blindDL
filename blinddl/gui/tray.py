# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""System-tray icon used when closing the window only hides it.

Downloads and subscription checks keep running with the window gone, so the
tray icon is the way back to them. NVDA reaches it with Windows+B; the same
two commands are on its menu and on its double-click.
"""

import wx
import wx.adv

from .. import APP_NAME


def app_icon(size=32):
    """Return a high-contrast native icon that never depends on an asset."""
    size = max(16, int(size))
    bitmap = wx.Bitmap(size, size, 32)
    dc = wx.MemoryDC(bitmap)
    dc.SetBackground(wx.Brush(wx.Colour(0, 82, 204)))
    dc.Clear()
    dc.SetTextForeground(wx.Colour(255, 255, 255))
    dc.SetFont(
        wx.Font(
            wx.FontInfo(max(12, int(size * 0.68)))
            .Bold()
            .Family(wx.FONTFAMILY_SWISS)
        )
    )
    width, height = dc.GetTextExtent("B")
    dc.DrawText("B", (size - width) // 2, (size - height) // 2)
    dc.SelectObject(wx.NullBitmap)
    icon = wx.Icon()
    icon.CopyFromBitmap(bitmap)
    return icon


class TrayIcon(wx.adv.TaskBarIcon):
    """Tray presence for one MainFrame.

    on_restore brings the window back; on_exit closes the application for
    real, rather than hiding it again.
    """

    ID_RESTORE = wx.ID_OPEN
    ID_EXIT = wx.ID_EXIT

    def __init__(self, frame, on_restore, on_exit):
        super().__init__()
        self.frame = frame
        self._on_restore = on_restore
        self._on_exit = on_exit
        self.installed = bool(
            self.SetIcon(app_icon(), f"{APP_NAME} — click to restore")
            and self.IsIconInstalled()
        )
        self.Bind(wx.adv.EVT_TASKBAR_LEFT_UP, self._restore)
        self.Bind(wx.adv.EVT_TASKBAR_LEFT_DCLICK, self._restore)
        self.Bind(wx.EVT_MENU, self._restore, id=self.ID_RESTORE)
        self.Bind(wx.EVT_MENU, self._exit, id=self.ID_EXIT)

    def CreatePopupMenu(self):
        menu = wx.Menu()
        menu.Append(self.ID_RESTORE, f"&Open {APP_NAME}")
        menu.AppendSeparator()
        menu.Append(self.ID_EXIT, "E&xit")
        return menu

    def _restore(self, _event=None):
        self._on_restore()

    def _exit(self, _event=None):
        self._on_exit()

    def is_available(self):
        return self.installed and self.IsIconInstalled()

    def notify_hidden(self):
        """Tell the user where the app went; Windows announces this balloon."""
        try:
            self.ShowBalloon(
                f"{APP_NAME} is still running",
                "Click the blue B icon, press Windows+B, or launch blindDL "
                "again to restore this window.",
                8000,
            )
        except (AttributeError, RuntimeError):
            pass

    def dispose(self):
        """Take the icon out of the tray and drop the native object."""
        self.RemoveIcon()
        self.installed = False
        self.Destroy()
