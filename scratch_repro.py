# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Temporary: isolates which virtual-list teardown pattern crashes wxGTK."""

import subprocess
import sys

VARIANTS = {
    # Exactly what the panel does today.
    "current": """
import wx
class L(wx.ListCtrl):
    def __init__(self, parent):
        super().__init__(parent, style=wx.LC_REPORT | wx.LC_VIRTUAL)
        self.cell_provider = None
    def OnGetItemText(self, item, column):
        if self.cell_provider is None:
            return ""
        return self.cell_provider(item, column)
class P(wx.Panel):
    def __init__(self, parent):
        super().__init__(parent)
        self.results = [{"title": f"t{i}"} for i in range(5000)]
        self.lst = L(self)
        self.lst.cell_provider = self.cell
        for i, h in enumerate(["a","b","c","d","e","f"]):
            self.lst.InsertColumn(i, h)
    def cell(self, row, col):
        return self.results[row]["title"] if row < len(self.results) else ""
app = wx.App()
f = wx.Frame(None)
p = P(f)
p.lst.SetItemCount(5000)
p.lst.Refresh()
f.Destroy()
app.Yield()
print("SURVIVED")
""",
    # No Refresh() after SetItemCount.
    "no_refresh": """
import wx
class L(wx.ListCtrl):
    def __init__(self, parent):
        super().__init__(parent, style=wx.LC_REPORT | wx.LC_VIRTUAL)
        self.cell_provider = None
    def OnGetItemText(self, item, column):
        if self.cell_provider is None:
            return ""
        return self.cell_provider(item, column)
class P(wx.Panel):
    def __init__(self, parent):
        super().__init__(parent)
        self.results = [{"title": f"t{i}"} for i in range(5000)]
        self.lst = L(self)
        self.lst.cell_provider = self.cell
        for i, h in enumerate(["a","b","c","d","e","f"]):
            self.lst.InsertColumn(i, h)
    def cell(self, row, col):
        return self.results[row]["title"] if row < len(self.results) else ""
app = wx.App()
f = wx.Frame(None)
p = P(f)
p.lst.SetItemCount(5000)
f.Destroy()
app.Yield()
print("SURVIVED")
""",
    # Zero the count when destruction starts.
    "zero_on_destroy": """
import wx
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
class P(wx.Panel):
    def __init__(self, parent):
        super().__init__(parent)
        self.results = [{"title": f"t{i}"} for i in range(5000)]
        self.lst = L(self)
        self.lst.cell_provider = self.cell
        for i, h in enumerate(["a","b","c","d","e","f"]):
            self.lst.InsertColumn(i, h)
    def cell(self, row, col):
        return self.results[row]["title"] if row < len(self.results) else ""
app = wx.App()
f = wx.Frame(None)
p = P(f)
p.lst.SetItemCount(5000)
p.lst.Refresh()
f.Destroy()
app.Yield()
print("SURVIVED")
""",
    # No cycle: the list reaches the panel weakly.
    "weak_provider": """
import weakref, wx
class L(wx.ListCtrl):
    def __init__(self, parent):
        super().__init__(parent, style=wx.LC_REPORT | wx.LC_VIRTUAL)
        self._ref = None
    def set_provider(self, m):
        self._ref = weakref.WeakMethod(m)
    def OnGetItemText(self, item, column):
        p = self._ref() if self._ref else None
        return p(item, column) if p else ""
class P(wx.Panel):
    def __init__(self, parent):
        super().__init__(parent)
        self.results = [{"title": f"t{i}"} for i in range(5000)]
        self.lst = L(self)
        self.lst.set_provider(self.cell)
        for i, h in enumerate(["a","b","c","d","e","f"]):
            self.lst.InsertColumn(i, h)
    def cell(self, row, col):
        return self.results[row]["title"] if row < len(self.results) else ""
app = wx.App()
f = wx.Frame(None)
p = P(f)
p.lst.SetItemCount(5000)
p.lst.Refresh()
f.Destroy()
app.Yield()
print("SURVIVED")
""",
    # Plain virtual list, no Python subclass state at all.
    "bare_virtual": """
import wx
class L(wx.ListCtrl):
    def OnGetItemText(self, item, column):
        return "x"
app = wx.App()
f = wx.Frame(None)
lst = L(f, style=wx.LC_REPORT | wx.LC_VIRTUAL)
for i, h in enumerate(["a","b","c","d","e","f"]):
    lst.InsertColumn(i, h)
lst.SetItemCount(5000)
lst.Refresh()
f.Destroy()
app.Yield()
print("SURVIVED")
""",
    # Control shown before teardown, which is what a real window does.
    "shown": """
import wx
class L(wx.ListCtrl):
    def OnGetItemText(self, item, column):
        return "x"
app = wx.App()
f = wx.Frame(None)
lst = L(f, style=wx.LC_REPORT | wx.LC_VIRTUAL)
for i, h in enumerate(["a","b","c","d","e","f"]):
    lst.InsertColumn(i, h)
lst.SetItemCount(5000)
f.Show()
app.Yield()
f.Destroy()
app.Yield()
print("SURVIVED")
""",
}

for name, code in VARIANTS.items():
    proc = subprocess.run([sys.executable, "-c", code],
                          capture_output=True, text=True, timeout=120)
    verdict = "OK  " if proc.returncode == 0 else f"CRASH rc={proc.returncode}"
    print(f"{verdict}  {name}")
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-3:]
        for line in tail:
            print(f"        {line}")
