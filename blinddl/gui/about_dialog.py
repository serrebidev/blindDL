"""Keyboard-first About dialog: plain text, then every link as its own tab stop.

    show_about(parent, title, heading, description, links)

``links`` is a list of ``(label, url)``. Each becomes a HyperlinkCtrl that Tab
reaches and Enter, Space or a click opens in the default browser.

    report_issue(parent, github_repo, app_line, _)

Help > Report an issue: asks a title and a description, then opens GitHub's
new-issue page with both filled in and ``app_line`` plus the system added.
"""
import platform
import urllib.parse

import wx
import wx.adv


def _open(url):
    wx.LaunchDefaultBrowser(url)


class AboutDialog(wx.Dialog):
    def __init__(self, parent, title, heading, description, links):
        super().__init__(parent, title=title, style=wx.DEFAULT_DIALOG_STYLE)
        panel = wx.Panel(self)
        layout = wx.BoxSizer(wx.VERTICAL)

        head = wx.StaticText(panel, label=heading)
        head.SetFont(head.GetFont().Bold())
        layout.Add(head, 0, wx.ALL, 12)

        text = wx.StaticText(panel, label=description)
        text.Wrap(500)
        layout.Add(text, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)

        self.links = []
        for label, url in links:
            link = wx.adv.HyperlinkCtrl(panel, label=label, url=url)
            link.SetName(label)

            # wxMSW's HyperlinkCtrl has no key handling of its own: Enter would
            # press the default OK button instead. Open on Enter/Space only, so
            # Tab and arrows still just move.
            def on_key(event, _url=url):
                if event.GetKeyCode() in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER, wx.WXK_SPACE):
                    _open(_url)
                    return
                event.Skip()

            link.Bind(wx.EVT_CHAR_HOOK, on_key)
            link.Bind(wx.adv.EVT_HYPERLINK, lambda _e, _url=url: _open(_url))
            layout.Add(link, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
            self.links.append(link)

        # Buttons are children of the panel; dialog-owned buttons in a panel
        # sizer leave wxMSW without a command route to close the dialog.
        buttons = wx.StdDialogButtonSizer()
        self.ok_btn = wx.Button(panel, id=wx.ID_OK, label="OK")
        buttons.AddButton(self.ok_btn)
        buttons.Realize()
        layout.Add(buttons, 0, wx.ALIGN_RIGHT | wx.ALL, 12)
        panel.SetSizerAndFit(layout)
        self.SetMinSize((540, -1))
        self.Fit()
        self.CentreOnParent()
        self.ok_btn.Bind(wx.EVT_BUTTON, lambda _e: self._finish(wx.ID_OK))
        self.Bind(wx.EVT_CLOSE, lambda _e: self._finish(wx.ID_CANCEL))
        self.SetEscapeId(wx.ID_OK)
        self.ok_btn.SetDefault()
        self.ok_btn.SetFocus()

    def _finish(self, result):
        if self.IsModal():
            self.EndModal(result)
        else:
            self.Destroy()


def serrebi_links(github_repo, forgejo_repo, branch="main"):
    """The standard link set: GitHub, Forgejo, projects site, Telegram, credits."""
    forgejo = f"https://git.serrebiradio.com/{forgejo_repo}"
    return [
        (f"GitHub: {github_repo}", f"https://github.com/{github_repo}"),
        (f"Forgejo: {forgejo_repo}", forgejo),
        ("Projects: projects.serrebiradio.com", "https://projects.serrebiradio.com"),
        ("Telegram: SerrebiProjects", "https://t.me/SerrebiProjects"),
        ("Full credits (CREDITS.md)", f"{forgejo}/src/branch/{branch}/CREDITS.md"),
    ]


def show_about(parent, title, heading, description, links):
    dlg = AboutDialog(parent, title, heading, description, links)
    try:
        dlg.ShowModal()
    finally:
        if dlg:
            dlg.Destroy()


def issue_url(github_repo, title, details, app_line):
    """GitHub's new-issue page with the title and body filled in."""
    body = f"{details}\n\n---\n{app_line} on {platform.platform()} {platform.machine()}"
    query = urllib.parse.urlencode({"title": title, "body": body}, quote_via=urllib.parse.quote)
    return f"https://github.com/{github_repo}/issues/new?{query}"


def report_issue(parent, github_repo, app_line, _=str):
    """Ask the title and description here, where the screen reader knows the
    boxes, then open GitHub prefilled. GitHub needs the user's own account, so
    the last step, Create, is theirs in the browser. ``_`` translates."""
    caption = _("Report an issue")
    summary = _ask(
        parent,
        _("Issue title: one line saying what went wrong or what you would like"),
        caption,
        0,
    )
    if not summary:
        return
    details = _ask(
        parent,
        _(
            "Describe what you did, what happened, and what you expected.\n"
            "Your browser then opens GitHub with this filled in. Sign in and press Create."
        ),
        caption,
        wx.TE_MULTILINE,
    )
    if details is None:
        return
    _open(issue_url(github_repo, summary, details, app_line))


def _ask(parent, message, caption, style):
    """Text from a dialog, or None on Cancel. Multiline: Enter is a new line, Tab reaches OK."""
    dlg = wx.TextEntryDialog(parent, message, caption, style=wx.OK | wx.CANCEL | style)
    try:
        if dlg.ShowModal() != wx.ID_OK:
            return None
        return dlg.GetValue().strip()
    finally:
        dlg.Destroy()
