'''
Function:
    Implementation of Zvu4ITMusicClient: https://zvu4it.org/ (formerly zvu4no.org)
Origin:
    Ported for blindDL from MusicGrabber's zvu4no.py (gitlab.com/g33kphr33k/musicgrabber,
    The Unlicense) and adapted to the musicdl SongInfo/BaseMusicClient contract.

    The site is a Russian MP3 portal with server-rendered search pages and direct
    MP3 links. No auth required. Search results include artist, title, duration,
    optional thumbnail and a data.zvu4it.org download URL.
'''
import hashlib
import html
import re
from contextlib import suppress
from typing import Unpack
from urllib.parse import quote

from rich.progress import Progress

from ..sources import BaseMusicClient, BaseMusicClientKwargs
from ..utils import legalizestring, usesearchheaderscookies, SongInfo, SongInfoUtils, AudioLinkTester


_BASE_URL = "https://zvu4it.org"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0",
    "Referer": _BASE_URL + "/",
}

_RE_BLOCK = re.compile(r'<div class="f-table">.*?(?=<div class="f-table">|<div id="queries"|</div>\s*</div>\s*<div id="amp-player")', re.DOTALL)
_RE_ARTIST = re.compile(r'<div class="artist-name">\s*<a [^>]*>(.*?)</a>\s*</div>', re.DOTALL)
_RE_TITLE = re.compile(r'<div class="track-name">(.*?)</div>', re.DOTALL)
_RE_DUR = re.compile(r'<div class="time-text">(.*?)</div>', re.DOTALL)
# Domain-agnostic on purpose: the site rebranded from zvu4no.org to zvu4it.org
# under MusicGrabber's feet, so match whichever CDN host serves the links
# instead of duplicating the current hostname here.
_RE_HREF = re.compile(r'<a class="mp3" href="(//data\.[a-z0-9.-]+/download-track/[^"]+\.mp3)"', re.DOTALL)
_RE_IMG = re.compile(r'<img src="(//img\.[a-z0-9.-]+/[^"]+)"', re.DOTALL)
_RE_TAGS = re.compile(r"<[^>]+>")


'''cleanHtmlText'''
def cleanhtmltext(value: str) -> str:
    value = _RE_TAGS.sub("", value or "")
    return html.unescape(value).strip()


'''durationtoseconds'''
def durationtoseconds(dur: str) -> int:
    try:
        parts = dur.strip().split(":")
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    except (ValueError, AttributeError):
        pass
    return 0


'''Zvu4ITMusicClient'''
class Zvu4ITMusicClient(BaseMusicClient):
    source = 'Zvu4ITMusicClient'
    def __init__(self, **kwargs: Unpack[BaseMusicClientKwargs]):
        super(Zvu4ITMusicClient, self).__init__(**kwargs)
        self.default_search_headers = dict(_HEADERS)
        self.default_download_headers = dict(_HEADERS)
        self.default_headers = self.default_search_headers
        self._initsession()
    '''_constructsearchurls'''
    def _constructsearchurls(self, keyword: str, rule: dict = None, request_overrides: dict = None):
        # init
        rule, request_overrides = rule or {}, request_overrides or {}
        # construct search urls based on search rules
        search_urls = [f"{_BASE_URL}/tracks/{quote(keyword)}"]
        self.search_size_per_page = self.search_size_per_source
        # return
        return search_urls
    '''_parsesearchresultfromblock'''
    def _parsesearchresultfromblock(self, block: str, request_overrides: dict = None) -> SongInfo:
        # init
        request_overrides, song_info = request_overrides or {}, SongInfo(source=self.source)
        # parse fields
        artist_m, title_m, dur_m, href_m = _RE_ARTIST.search(block), _RE_TITLE.search(block), _RE_DUR.search(block), _RE_HREF.search(block)
        if not (artist_m and title_m and href_m): return song_info
        artist, title = cleanhtmltext(artist_m.group(1)), cleanhtmltext(title_m.group(1))
        dur = cleanhtmltext(dur_m.group(1)) if dur_m else ""
        download_url = "https:" + html.unescape(href_m.group(1))
        if not title or not download_url.startswith("http"): return song_info
        # verify the direct MP3 link and learn its real size
        download_url_status: dict = self.audio_link_tester.test(url=download_url, request_overrides={'headers': self.default_download_headers}, renew_session=True)
        if not download_url_status.get('ok'): return song_info
        # cover art
        img_m = _RE_IMG.search(block)
        cover_url = ("https:" + html.unescape(img_m.group(1))) if img_m else ""
        # build SongInfo
        duration_in_secs = durationtoseconds(dur)
        song_info = SongInfo(
            raw_data={'search': {'artist': artist, 'title': title, 'url': download_url}}, source=self.source,
            song_name=legalizestring(title), singers=legalizestring(artist), album='NULL',
            ext=download_url_status.get('ext') or 'mp3', file_size_bytes=download_url_status.get('file_size_bytes') or 0,
            file_size=download_url_status.get('file_size') or 'NULL', identifier=hashlib.md5(download_url.encode()).hexdigest()[:12],
            duration_s=duration_in_secs, duration=SongInfoUtils.seconds2hms(duration_in_secs) if duration_in_secs else 'NULL',
            lyric='NULL', cover_url=cover_url, download_url=download_url_status.get('download_url') or download_url,
            download_url_status=download_url_status, default_download_headers=dict(self.default_download_headers),
        )
        # return
        return song_info
    '''_search'''
    @usesearchheaderscookies
    def _search(self, keyword: str = '', search_url: str = '', request_overrides: dict = None, song_infos: list = [], progress: Progress = None):
        # init
        request_overrides = request_overrides or {}
        task_id = progress.add_task(f"{self.source}._search >>> Searching \"{keyword}\"", total=None, completed=0)
        # successful
        try:
            (resp := self.get(search_url, **request_overrides)).raise_for_status()
            seen_urls = set()
            for block in _RE_BLOCK.findall(resp.text):
                href_m = _RE_HREF.search(block)
                if not href_m: continue
                href = "https:" + html.unescape(href_m.group(1))
                if href in seen_urls: continue
                seen_urls.add(href)
                # --parse download result
                song_info = SongInfo(source=self.source)
                with suppress(Exception): song_info = self._parsesearchresultfromblock(block, request_overrides)
                # --append to song_infos
                if song_info.with_valid_download_url: song_infos.append(song_info)
                # --judgement for search_size
                if self.strict_limit_search_size_per_page and len(song_infos) >= self.search_size_per_page: break
            progress.update(task_id, description=f'{self.source}._search >>> {len(song_infos)} results for "{keyword}"')
        # failure
        except Exception as err:
            progress.update(task_id, description=f'{self.source}._search >>> {keyword} (Error: {err})')
            self.logger_handle.error(f'{self.source}._search >>> {keyword} (Error: {err})', disable_print=self.disable_print)