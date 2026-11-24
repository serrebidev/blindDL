"""About dialog: every link is a tab stop; Enter/Space open it, Tab does not."""
import wx

from blinddl.gui import about_dialog


def _key(link, code):
    event = wx.KeyEvent(wx.wxEVT_CHAR_HOOK)
    event.SetKeyCode(code)
    event.SetEventObject(link)
    link.GetEventHandler().ProcessEvent(event)


def test_links_are_tab_stops_and_open_on_enter(monkeypatch):
    app = wx.GetApp() or wx.App(False)
    opened = []
    monkeypatch.setattr(about_dialog, "_open", opened.append)
    links = about_dialog.serrebi_links("serrebidev/x", "serrebi/x")
    dlg = about_dialog.AboutDialog(None, "About X", "X 1.0", "Text.", links)
    try:
        assert [link.GetLabel() for link in dlg.links] == [label for label, _ in links]
        assert all(link.AcceptsFocus() for link in dlg.links)
        _key(dlg.links[0], wx.WXK_TAB)
        assert opened == []
        _key(dlg.links[0], wx.WXK_RETURN)
        _key(dlg.links[3], wx.WXK_SPACE)
        assert opened == ["https://github.com/serrebidev/x", "https://t.me/SerrebiProjects"]
        assert links[-1][1] == "https://git.serrebiradio.com/serrebi/x/src/branch/main/CREDITS.md"
    finally:
        dlg.Destroy()
    assert app


def test_issue_url_carries_title_body_and_version():
    url = about_dialog.issue_url("serrebidev/x", "Photo & video", "Line 1\nПривет", "X 1.0")
    assert url.startswith(
        "https://github.com/serrebidev/x/issues/new?title=Photo%20%26%20video&body=Line%201%0A%D0%9F"
    )
    assert "%0A---%0AX%201.0%20on%20" in url
    assert not any(c in url for c in " \n#")


def test_report_issue_opens_prefilled_page_and_cancel_opens_nothing(monkeypatch):
    opened = []
    monkeypatch.setattr(about_dialog, "_open", opened.append)
    answers = iter([None])
    monkeypatch.setattr(about_dialog, "_ask", lambda *a: next(answers))
    about_dialog.report_issue(None, "serrebidev/x", "X 1.0")
    assert opened == []
    answers = iter(["Crash", "Steps"])
    about_dialog.report_issue(None, "serrebidev/x", "X 1.0")
    assert opened[0].startswith("https://github.com/serrebidev/x/issues/new?title=Crash&body=Steps")
