'''
Function:
    Implementation of FreeMp3CloudMusicClient: https://g2.freemp3cloud.com/
Origin:
    Ported for blindDL from MusicGrabber's freemp3cloud.py (gitlab.com/g33kphr33k/musicgrabber,
    The Unlicense) and adapted to the musicdl SongInfo/BaseMusicClient contract.

    MP3 search portal backed by the meln.top CDN. The site is server-rendered
    ASP.NET: a search is a form POST that needs a session cookie plus an
    antiforgery token harvested from the landing page first. The HTML it returns
    lists tracks with artist, title, duration, a direct cdnm.meln.top MP3
    download link, and - crucially - an "HQ" label on the good ones.

    HQ-tagged results are 320 kbps (some 256) and even ship embedded cover art;
    the un-tagged ones are a sad 128 kbps. Results carrying the HQ marker get a
    head start in the quality row shown to the user.

    The download URL carries its own session_key + hash, so it works later from
    the download thread with no cookie needed; only the search step needs the
    session. Note the load-balancing quirk: g2 currently 301s to a2 (the
    subdomain changes without warning) and a POST to the pre-redirect URL gets
    silently downgraded to a GET by the redirect follow, so the search POST goes
    to wherever the landing page actually answered from.
'''
import hashlib
import html
import re
from contextlib import suppress
from typing import Unpack

from rich.progress import Progress

from ..sources import BaseMusicClient, BaseMusicClientKwargs
from ..utils import legalizestring, usesearchheaderscookies, SongInfo, SongInfoUtils, AudioLinkTester


_BASE_URL = "https://g2.freemp3cloud.com"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0",
    "Referer": _BASE_URL + "/",
}

# Each result lives in a <div class="play-item"> ... </div>.
_RE_TOKEN  = re.compile(r'name="__RequestVerificationToken"[^>]*value="([^"]+)"')
_RE_ARTIST = re.compile(r'class="s-artist">(.*?)</div>', re.DOTALL)
_RE_TITLE  = re.compile(r'class="s-title">(.*?)</div>', re.DOTALL)
_RE_TIME   = re.compile(r'class="s-time">(.*?)</div>', re.DOTALL)
_RE_HREF   = re.compile(r'class="downl">\s*<a href="(https://[^"]+\.mp3[^"]*)"', re.DOTALL)
_RE_HQ     = re.compile(r'class="s-hq"')
_RE_TAGS   = re.compile(r"<[^>]+>")


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


'''FreeMp3CloudMusicClient'''
class FreeMp3CloudMusicClient(BaseMusicClient):
    source = 'FreeMp3CloudMusicClient'
    def __init__(self, **kwargs: Unpack[BaseMusicClientKwargs]):
        super(FreeMp3CloudMusicClient, self).__init__(**kwargs)
        self.default_search_headers = dict(_HEADERS)
        self.default_download_headers = dict(_HEADERS)
        self.default_headers = self.default_search_headers
        self._initsession()
    '''_constructsearchurls'''
    def _constructsearchurls(self, keyword: str, rule: dict = None, request_overrides: dict = None):
        # init
        rule, request_overrides = rule or {}, request_overrides or {}
        # the search URL is the landing page: its session cookie + antiforgery
        # token are required before the search POST will be accepted
        search_urls = [_BASE_URL + "/"]
        self.search_size_per_page = self.search_size_per_source
        # return
        return search_urls
    '''_parsesearchresultfromblock'''
    def _parsesearchresultfromblock(self, block: str, request_overrides: dict = None) -> SongInfo:
        # init
        request_overrides, song_info = request_overrides or {}, SongInfo(source=self.source)
        # parse fields
        title_m, href_m = _RE_TITLE.search(block), _RE_HREF.search(block)
        if not (title_m and href_m): return song_info
        title = cleanhtmltext(title_m.group(1))
        download_url = html.unescape(href_m.group(1))
        if not title or not download_url.startswith("http"): return song_info
        artist_m, time_m, is_hq = _RE_ARTIST.search(block), _RE_TIME.search(block), bool(_RE_HQ.search(block))
        artist = cleanhtmltext(artist_m.group(1)) if artist_m else ""
        dur = cleanhtmltext(time_m.group(1)) if time_m else ""
        # verify the direct MP3 link and learn its real size
        download_url_status: dict = self.audio_link_tester.test(url=download_url, request_overrides={'headers': self.default_download_headers}, renew_session=True)
        if not download_url_status.get('ok'): return song_info
        # note the declared quality tier next to the verified link: HQ is a real
        # 320kbps (some 256), the rest are 128kbps
        quality_label = '320kbps' if is_hq else '128kbps'
        # build SongInfo
        duration_in_secs = durationtoseconds(dur)
        song_info = SongInfo(
            raw_data={'search': {'artist': artist, 'title': title, 'url': download_url, 'quality': quality_label}}, source=self.source,
            song_name=legalizestring(title), singers=legalizestring(artist), album='NULL',
            ext=download_url_status.get('ext') or 'mp3', file_size_bytes=download_url_status.get('file_size_bytes') or 0,
            file_size=download_url_status.get('file_size') or 'NULL', identifier=hashlib.md5(download_url.encode()).hexdigest()[:12],
            duration_s=duration_in_secs, duration=SongInfoUtils.seconds2hms(duration_in_secs) if duration_in_secs else 'NULL',
            lyric='NULL', cover_url='', download_url=download_url_status.get('download_url') or download_url,
            download_url_status=download_url_status, default_download_headers=dict(self.default_download_headers),
        )
        song_info.bitrate = 320 if is_hq else 128
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
            # Step 1: landing page hands us the session cookie + antiforgery token
            (landing := self.get(search_url, **request_overrides)).raise_for_status()
            token_m = _RE_TOKEN.search(landing.text)
            if not token_m: raise RuntimeError("no antiforgery token on landing page")
            # Step 2: post the search to wherever we actually landed (the site
            # load-balances across subdomains and redirects without warning);
            # the session cookie travels on the client automatically
            kwargs = dict(request_overrides)
            kwargs['headers'] = {**(kwargs.get('headers') or {}), 'Content-Type': 'application/x-www-form-urlencoded'}
            (resp := self.post(str(landing.url), data={'searchSong': keyword, '__RequestVerificationToken': token_m.group(1)}, **kwargs)).raise_for_status()
            # parse result blocks
            for block in resp.text.split('<div class="play-item">')[1:]:
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