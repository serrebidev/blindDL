# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Temporary: finds a virtual-list teardown that survives wxWidgets 3.2.4.

wxGTK defers window deletion to idle time, so a panel destroyed in one test is
really deleted during a later app.Yield() -- by which point Python may have
collected the list control's own objects. Each variant below is run in its own
process so a segfault only takes down the variant that caused it.
"""

import subprocess
import sys

PREAMBLE = """
import gc, weakref, wx
"""

# Mimics the test lifecycle: build, destroy, drop the reference, collect, and
# only then let the event loop actually delete the window.
DRIVER = """
app = wx.App()
def cycle():
    host = wx.Frame(None)
    p = P(host)
    p.lst.SetItemCount(5000)
    p.lst.Refresh()
    host.Destroy()
for _ in range(4):
    cycle()
    gc.collect()
    app.Yield()
print("SURVIVED")
"""

_PANEL_TEMPLATE = """
class P(wx.Panel):
    def __init__(self, parent):
        super().__init__(parent)
        self.results = [{"title": "t" + str(i)} for i in range(5000)]
        self.lst = L(self)
        __WIRE__
        for i, h in enumerate(["a","b","c","d","e","f"]):
            self.lst.InsertColumn(i, h)
    def cell(self, row, col):
        return self.results[row]["title"] if row < len(self.results) else ""
"""


def PANEL(wire):
    return _PANEL_TEMPLATE.replace("__WIRE__", wire)

VARIANTS = {
    # What shipped: a bound method, no teardown guard.
    "current": PREAMBLE + """
class L(wx.ListCtrl):
    def __init__(self, parent):
        super().__init__(parent, style=wx.LC_REPORT | wx.LC_VIRTUAL)
        self.cell_provider = None
    def OnGetItemText(self, item, column):
        if self.cell_provider is None:
            return ""
        return self.cell_provider(item, column)
""" + PANEL("self.lst.cell_provider = self.cell") + DRIVER,

    # Stop answering, and empty the control, as destruction begins.
    "zero_on_destroy": PREAMBLE + """
class L(wx.ListCtrl):
    def __init__(self, parent):
        super().__init__(parent, style=wx.LC_REPORT | wx.LC_VIRTUAL)
        self.cell_provider = None
        self.Bind(wx.EVT_WINDOW_DESTROY, self._bye)
    def _bye(self, event):
        if event.GetEventObject() is self:
            self.cell_provider = None
            self.SetItemCount(0)
        event.Skip()
    def OnGetItemText(self, item, column):
        if self.cell_provider is None:
            return ""
        return self.cell_provider(item, column)
""" + PANEL("self.lst.cell_provider = self.cell") + DRIVER,

    # Same, but the list never strongly owns the panel.
    "weak_and_zero": PREAMBLE + """
class L(wx.ListCtrl):
    def __init__(self, parent):
        super().__init__(parent, style=wx.LC_REPORT | wx.LC_VIRTUAL)
        self._ref = None
        self.Bind(wx.EVT_WINDOW_DESTROY, self._bye)
    def set_provider(self, m):
        self._ref = weakref.WeakMethod(m)
    def _bye(self, event):
        if event.GetEventObject() is self:
            self._ref = None
            self.SetItemCount(0)
        event.Skip()
    def OnGetItemText(self, item, column):
        p = self._ref() if self._ref else None
        return p(item, column) if p else ""
""" + PANEL("self.lst.set_provider(self.cell)") + DRIVER,

    # No Python override at all: the list owns its own strings.
    "self_contained": PREAMBLE + """
class L(wx.ListCtrl):
    def __init__(self, parent):
        super().__init__(parent, style=wx.LC_REPORT | wx.LC_VIRTUAL)
        self.rows = []
        self.Bind(wx.EVT_WINDOW_DESTROY, self._bye)
    def _bye(self, event):
        if event.GetEventObject() is self:
            self.rows = []
            self.SetItemCount(0)
        event.Skip()
    def OnGetItemText(self, item, column):
        if 0 <= item < len(self.rows):
            return self.rows[item]
        return ""
""" + PANEL("self.lst.rows = [r['title'] for r in self.results]") + DRIVER,

    # Keep the control non-virtual but never rebuild: the control is emptied
    # before the window goes away.
    "explicit_clear_before_destroy": PREAMBLE + """
class L(wx.ListCtrl):
    def __init__(self, parent):
        super().__init__(parent, style=wx.LC_REPORT | wx.LC_VIRTUAL)
        self.cell_provider = None
    def OnGetItemText(self, item, column):
        if self.cell_provider is None:
            return ""
        return self.cell_provider(item, column)
""" + PANEL("self.lst.cell_provider = self.cell") + """
app = wx.App()
def cycle():
    host = wx.Frame(None)
    p = P(host)
    p.lst.SetItemCount(5000)
    p.lst.Refresh()
    p.lst.SetItemCount(0)
    p.lst.cell_provider = None
    host.Destroy()
for _ in range(4):
    cycle()
    gc.collect()
    app.Yield()
print("SURVIVED")
""",
}

for name, code in VARIANTS.items():
    proc = subprocess.run([sys.executable, "-c", code],
                          capture_output=True, text=True, timeout=180)
    verdict = "OK   " if proc.returncode == 0 else f"CRASH rc={proc.returncode}"
    print(f"{verdict}  {name}", flush=True)
