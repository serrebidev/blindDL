'''
Function:
    Implementation of Mp3PmMusicClient: https://mp3.pm/
Origin:
    Written for blindDL, following the shape of the MusicGrabber ports
    (freemp3cloud.py, zvu4it.py) and adapted to the musicdl
    SongInfo/BaseMusicClient contract.

    MP3.pm is a server-rendered MP3 portal whose search results carry the
    stream URL right on the page. The browser flow is two steps: the search
    box POSTs q=<query> to /public/api.search.php and gets a URL back, and
    that URL (https://mp3.pm/search?q=...) renders an HTML <ul> whose rows
    carry artist, title, duration, a csN.mp3.pm "listen" stream URL, and a
    csN.mp3.pm "download" URL of the same file. The listen URL redirects to
    the real file; the download URL serves it directly, and both expire, so
    a stored SongInfo keeps the download URL returned by its own search.

    Every result is a plain MP3. The site's own listing hints at bitrate in
    the file's name and its size; the number carried in SongInfo.bitrate is
    a declared estimate (file bytes / duration), not a verified value.
'''
import hashlib
import html
import re
from contextlib import suppress
from typing import Unpack
from urllib.parse import quote_plus

from rich.progress import Progress

from ..sources import BaseMusicClient, BaseMusicClientKwargs
from ..utils import legalizestring, usesearchheaderscookies, SongInfo, SongInfoUtils


_BASE_URL = "https://mp3.pm"
_SEARCH_API = _BASE_URL + "/public/api.search.php"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Referer": _BASE_URL + "/",
    "X-Requested-With": "XMLHttpRequest",
}
_SEARCH_CONTENT_TYPE = "application/x-www-form-urlencoded; charset=UTF-8"

# One result is a <li class="cplayer-sound-item" ...> ... </li>. The listen
# and download URLs live in data- attributes on the <li> itself; artist,
# title and duration sit further inside the block.
_RE_ITEM = re.compile(r'<li class="cplayer-sound-item".*?</li>', re.DOTALL)
_RE_SOUND_URL = re.compile(r'data-sound-url="([^"]+)"', re.DOTALL)
_RE_DOWNLOAD_URL = re.compile(r'data-download-url="([^"]+)"', re.DOTALL)
_RE_ARTIST = re.compile(r'<i class="cplayer-data-sound-author">(.*?)</i>', re.DOTALL)
_RE_TITLE = re.compile(r'<b class="cplayer-data-sound-title">(.*?)</b>', re.DOTALL)
_RE_TIME = re.compile(r'<em class="cplayer-data-sound-time">\s*([0-9:]+)\s*</em>', re.DOTALL)
_RE_TAGS = re.compile(r"<[^>]+>")


'''cleanHtmlText'''
def cleanhtmltext(value: str) -> str:
    value = _RE_TAGS.sub("", value or "")
    return html.unescape(value).strip()


'''durationToSeconds'''
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


'''Mp3PmMusicClient'''
class Mp3PmMusicClient(BaseMusicClient):
    source = 'Mp3PmMusicClient'
    def __init__(self, **kwargs: Unpack[BaseMusicClientKwargs]):
        super(Mp3PmMusicClient, self).__init__(**kwargs)
        self.default_search_headers = dict(_HEADERS)
        self.default_download_headers = {
            "User-Agent": _HEADERS["User-Agent"],
        }
        self.default_headers = self.default_search_headers
        self._initsession()
    '''_constructsearchurls'''
    def _constructsearchurls(self, keyword: str, rule: dict = None, request_overrides: dict = None):
        # init
        rule, request_overrides = rule or {}, request_overrides or {}
        # the search URL handed to _search is only a marker: the real first
        # step is the POST to the JSON search endpoint, which answers with
        # the page URL to fetch. Posting twice would double the results, so
        # the marker must be left unrequested.
        search_urls = [f"{_BASE_URL}/search?q={quote_plus(keyword)}"]
        self.search_size_per_page = self.search_size_per_source
        # return
        return search_urls
    '''_parsesearchresultfromblock'''
    def _parsesearchresultfromblock(self, block: str, request_overrides: dict = None) -> SongInfo:
        # init
        request_overrides, song_info = request_overrides or {}, SongInfo(source=self.source)
        # parse fields
        artist_m, title_m, href_m = _RE_ARTIST.search(block), _RE_TITLE.search(block), _RE_DOWNLOAD_URL.search(block)
        if not (artist_m and title_m and href_m): return song_info
        artist, title = cleanhtmltext(artist_m.group(1)), cleanhtmltext(title_m.group(1))
        download_url = html.unescape(href_m.group(1))
        if not title or not download_url.startswith("http"): return song_info
        # verify the direct MP3 link and learn its real size; the site has no
        # metadata API, so the bitrate row shown to the user is estimated
        # from the verified file size and the listed duration (or the real
        # one once the file has landed, which SongInfoUtils then overwrites)
        download_url_status: dict = self.audio_link_tester.test(url=download_url, request_overrides={'headers': self.default_download_headers}, renew_session=True)
        if not download_url_status.get('ok'): return song_info
        time_m = _RE_TIME.search(block)
        duration_in_secs = durationtoseconds(cleanhtmltext(time_m.group(1))) if time_m else 0
        file_size_bytes = download_url_status.get('file_size_bytes') or 0
        bitrate = int(file_size_bytes * 8 // duration_in_secs / 1000) if file_size_bytes and duration_in_secs else 0
        # build SongInfo
        song_info = SongInfo(
            raw_data={'search': {'artist': artist, 'title': title, 'url': download_url, 'bitrate': bitrate}}, source=self.source,
            song_name=legalizestring(title), singers=legalizestring(artist), album='NULL',
            ext=download_url_status.get('ext') or 'mp3', file_size_bytes=file_size_bytes,
            file_size=download_url_status.get('file_size') or 'NULL', identifier=hashlib.md5(str(artist + title).encode()).hexdigest()[:12],
            duration_s=duration_in_secs, duration=SongInfoUtils.seconds2hms(duration_in_secs) if duration_in_secs else 'NULL',
            lyric='NULL', cover_url='', download_url=download_url_status.get('download_url') or download_url,
            download_url_status=download_url_status, default_download_headers=dict(self.default_download_headers),
        )
        song_info.bitrate = bitrate or None
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
            # Step 1: the search endpoint answers a POST with the URL of the
            # page that carries the results (the browser follows the same
            # redirect), then fetch it from wherever it landed
            (resp := self.post(_SEARCH_API, data={'q': keyword}, **request_overrides)).raise_for_status()
            results_url = resp.text.strip()
            if not results_url.startswith("http"): raise RuntimeError(f"unexpected search answer: {results_url[:200]!r}")
            kwargs = dict(request_overrides)
            kwargs['headers'] = {**(kwargs.get('headers') or {}), 'Content-Type': _SEARCH_CONTENT_TYPE}
            (page := self.get(results_url, **kwargs)).raise_for_status()
            # parse result blocks
            seen_urls = set()
            for block in _RE_ITEM.findall(page.text):
                href_m = _RE_DOWNLOAD_URL.search(block)
                if not href_m: continue
                href = html.unescape(href_m.group(1))
                if href in seen_urls: continue
                seen_urls.add(href)
                # --parse download result
                song_info = SongInfo(source=self.source)
                with suppress(Exception): song_info = self._parsesearchresultfromblock(block, request_overrides)
                # --append to song_infos
                if song_info.with_valid_download_url: song_infos.append(song_info)
                # --judgement for search_size
                if self.strict_limit_search_size_per_page and len(song_infos) >= self.search_size_per_page: break
            progress.update(task_id, description=f'{self.source}._search >>> {len(song_infos)} results for \"{keyword}\"')
        # failure
        except Exception as err:
            progress.update(task_id, description=f'{self.source}._search >>> {keyword} (Error: {err})')
            self.logger_handle.error(f'{self.source}._search >>> {keyword} (Error: {err})', disable_print=self.disable_print)
