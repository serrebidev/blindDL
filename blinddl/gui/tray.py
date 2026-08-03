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


def _tray_bitmap():
    """An icon for the tray without shipping an icon file.

    The stock "find" art is what the platform already draws for a search
    tool, and it exists on every wx backend, so no asset can go missing from
    a frozen build.
    """
    size = wx.Size(16, 16)
    bitmap = wx.ArtProvider.GetBitmap(wx.ART_FIND, wx.ART_OTHER, size)
    if not bitmap.IsOk():
        bitmap = wx.ArtProvider.GetBitmap(wx.ART_INFORMATION, wx.ART_OTHER,
                                          size)
    # wxWidgets 3.3 takes a bundle here; older builds want a wx.Icon.
    return wx.BitmapBundle(bitmap)


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
        self.SetIcon(_tray_bitmap(), APP_NAME)
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

    def dispose(self):
        """Take the icon out of the tray and drop the native object."""
        self.RemoveIcon()
        self.Destroy()
