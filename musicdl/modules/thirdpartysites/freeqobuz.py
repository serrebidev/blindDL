'''
Function:
    Implementation of FreeQobuzMusicClient: Qobuz catalogue search and lossless
    FLAC stream resolution without any Qobuz account.
Origin:
    Ported for blindDL from MusicGrabber's qbdlx.py (gitlab.com/g33kphr33k/musicgrabber,
    The Unlicense) and adapted to the musicdl SongInfo/BaseMusicClient contract.

    The official Qobuz API is talked to directly, signing requests with shared
    free-account tokens. The token pool comes from the same webhook the qbdlx
    web UI (qbdlx.launchpd.cloud, "Free account" tab) hands out:

      1. GET a pool of {token, app_id, app_secret, country} from the webhook.
      2. catalog/search?query=...  ->  track items (title, artist, ISRC, id)
      3. track/getFileUrl (signed) ->  a real streaming-qobuz-std.akamaized.net URL

    Signature scheme (verified live):
      request_sig = MD5("trackgetFileUrl" + "format_id" + str(fmt) + "intent" +
                        "stream" + "track_id" + str(id) + str(ts) + app_secret)
      headers: X-App-Id, X-User-Auth-Token

    The shared tokens resolve to 16-bit/44.1kHz lossless FLAC (format_id 6),
    NOT 24-bit hi-res. Some tokens get quietly downgraded by Qobuz to 30-second
    preview MP3s (the response carries "sample": true); those are skipped and
    the next token in the pool is tried, so a sample can never dress up as the
    track.

    Health caveat: this source stands or falls with the third-party webhook
    that publishes the token pool. When the pool is empty or unreachable the
    source simply answers with no results - it never raises into a search.
'''
import hashlib
import threading
import time
from contextlib import suppress
from typing import Unpack

import requests
from rich.progress import Progress

from ..sources import BaseMusicClient, BaseMusicClientKwargs
from ..utils import legalizestring, usesearchheaderscookies, SongInfo, SongInfoUtils, AudioLinkTester


QBDLX_SHARED_TOKENS_URL = "https://citegptapi.f5.si/webhook/qbdlx/shared"
QBDLX_QOBUZ_API_BASE = "https://www.qobuz.com/api.json/0.2/"
# re-fetch the token pool every N seconds; the pool rotates upstream
QBDLX_TOKEN_CACHE_TTL = 600
# 16-bit/44.1kHz lossless FLAC - all the shared free tokens can serve
QOBUZ_STREAM_FORMAT_ID = 6
# Qobuz catalog/search refuses absurd page sizes
QOBUZ_SEARCH_LIMIT_CAP = 50
# Upper bound on how many catalogue hits one search resolves to stream URLs.
# Every hit costs a signed getFileUrl call plus a CDN probe, so resolving the
# whole catalogue page would blow a single source's search budget.
QOBUZ_RESOLVE_CAP = 10

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0",
}

# Cached token pool: list of {token, app_id, app_secret, country}
_pool_cache: list[dict] = []
_pool_cache_at: float = 0.0
_pool_lock = threading.Lock()
# Tokens ruled out this cache cycle (call failed, or Qobuz downgraded them to
# sample previews). Cleared whenever the pool is refetched with fresh data.
_known_bad: set[str] = set()
_known_bad_lock = threading.Lock()
# The token that last handed back a genuine stream, tried first next time.
# In-memory only: a blindDL restart pays one cold walk, which is acceptable.
_good_token: str | None = None
_good_token_lock = threading.Lock()


'''_fetchSharedTokens'''
def _fetch_shared_tokens(force: bool = False) -> list[dict]:
    """Return the shared token pool, cached for QBDLX_TOKEN_CACHE_TTL seconds.

    Returns whatever we last had on a fetch failure rather than blowing up; a
    stale token still beats no token, and the caller treats an empty list as
    "this source unavailable for now".
    """
    global _pool_cache, _pool_cache_at
    with _pool_lock:
        fresh = _pool_cache and (time.time() - _pool_cache_at) < QBDLX_TOKEN_CACHE_TTL
        if fresh and not force:
            return _pool_cache
        try:
            resp = requests.get(QBDLX_SHARED_TOKENS_URL, headers=_HEADERS, timeout=(10, 30), allow_redirects=True)
            resp.raise_for_status()
            data = resp.json()
            tokens = [
                t for t in (data if isinstance(data, list) else [])
                if t.get("token") and t.get("app_id") and t.get("app_secret")
            ]
            if tokens:
                _pool_cache, _pool_cache_at = tokens, time.time()
                with _known_bad_lock:
                    _known_bad.clear()
                return tokens
        except Exception:
            pass
        return _pool_cache  # last known good, possibly empty


'''_signedcall'''
def _signed_call(token: dict, path: str, params: dict, signed_concat: str | None = None) -> dict | None:
    """Make an (optionally signed) Qobuz API call with this token. None on failure."""
    p = dict(params)
    p["app_id"] = token["app_id"]
    if signed_concat is not None:
        ts = int(time.time())
        sig = hashlib.md5((signed_concat + str(ts) + token["app_secret"]).encode()).hexdigest()
        p["request_ts"] = ts
        p["request_sig"] = sig
    try:
        resp = requests.get(
            f"{QBDLX_QOBUZ_API_BASE.rstrip('/')}/{path}",
            params=p,
            headers={**_HEADERS, "X-App-Id": str(token["app_id"]), "X-User-Auth-Token": token["token"]},
            timeout=(10, 30),
            allow_redirects=True,
        )
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException:
        return None
    except Exception:
        return None


'''_markTokenBad'''
def _mark_token_bad(token: str | None) -> None:
    """Note a token as dead for the rest of this cache cycle.

    Only call this for a token-level fault (the call itself failed, or Qobuz
    downgraded the entitlement), never for a clean "this track isn't in the
    catalogue" answer: that would wrongly write off a perfectly healthy token
    just because one track was missing.
    """
    if not token:
        return
    with _known_bad_lock:
        _known_bad.add(token)


'''_rememberGoodToken'''
def _remember_good_token(token: dict) -> None:
    """Note the token that just delivered, so the next call tries it first."""
    global _good_token
    tok = token.get("token")
    with _good_token_lock:
        _good_token = tok


'''_tokensBestFirst'''
def _tokens_best_first(tokens: list[dict]) -> list[dict]:
    """Return the pool with the last known-good token promoted to the front."""
    with _good_token_lock:
        favourite = _good_token
    if not favourite:
        return tokens
    promoted = [t for t in tokens if t.get("token") == favourite]
    if not promoted:
        return tokens  # pool has rotated since; no harm done
    return promoted + [t for t in tokens if t.get("token") != favourite]


'''_usableTokens'''
def _usable_tokens(tokens: list[dict]) -> list[dict]:
    """Favourite first, then this cycle's known-bad tokens filtered out.

    Known-bad tokens are excluded rather than merely deprioritised: they rot
    without warning and don't un-rot inside one cache cycle, so there's no
    point paying their timeout again before the pool itself is refetched. If
    filtering would leave nothing to try, fall back to the full list; a last
    honest attempt beats refusing to try at all.
    """
    with _known_bad_lock:
        bad = set(_known_bad)
    filtered = [t for t in tokens if t.get("token") not in bad]
    return _tokens_best_first(filtered or tokens)


'''searchQobuzCatalog'''
def search_qobuz_catalog(query: str, limit: int = 10) -> list[dict]:
    """Search the Qobuz catalogue by free text via the shared token pool.

    Returns the raw Qobuz track item dicts so the caller does the shaping.
    Returns [] when the query is empty or no token in the pool can answer.
    """
    if not (query or "").strip():
        return []
    tokens = _fetch_shared_tokens()
    for token in _usable_tokens(tokens):
        body = _signed_call(token, "catalog/search", {"query": query, "limit": min(int(limit) or 10, QOBUZ_SEARCH_LIMIT_CAP)})
        if body is None:
            _mark_token_bad(token.get("token"))
            continue
        items = ((body.get("tracks") or {}).get("items")) or []
        if items:
            return items
    return []


'''resolveQobuzStreamUrl'''
def resolve_qobuz_stream_url(track_id, quality_fmt: int = QOBUZ_STREAM_FORMAT_ID) -> str | None:
    """Resolve a catalogue track id to a direct Qobuz CDN FLAC URL.

    Tries each token in the pool until one yields a genuine (non-sample) stream
    URL, starting with whichever token worked last time. Returns None when the
    pool is empty/unreachable, or no token can resolve the track.
    """
    tokens = _fetch_shared_tokens()
    if not tokens:
        return None
    for token in _usable_tokens(tokens):
        concat = f"trackgetFileUrlformat_id{quality_fmt}intentstreamtrack_id{track_id}"
        body = _signed_call(
            token,
            "track/getFileUrl",
            {"track_id": track_id, "format_id": quality_fmt, "intent": "stream"},
            signed_concat=concat,
        )
        if body is None:
            _mark_token_bad(token.get("token"))
            continue
        if body.get("sample"):
            # This token's entitlement has been downgraded server-side: Qobuz
            # hands back a 30-second preview. Not a stream, move on, and don't
            # bother asking this token again until the pool rotates.
            _mark_token_bad(token.get("token"))
            continue
        url = body.get("url") or ""
        if url:
            _remember_good_token(token)
            return url
    return None


'''FreeQobuzMusicClient'''
class FreeQobuzMusicClient(BaseMusicClient):
    source = 'FreeQobuzMusicClient'
    def __init__(self, **kwargs: Unpack[BaseMusicClientKwargs]):
        super(FreeQobuzMusicClient, self).__init__(**kwargs)
        self.default_search_headers = dict(_HEADERS)
        self.default_download_headers = dict(_HEADERS)
        self.default_headers = self.default_search_headers
        self._initsession()
    '''_constructsearchurls'''
    def _constructsearchurls(self, keyword: str, rule: dict = None, request_overrides: dict = None):
        # init
        rule, request_overrides = rule or {}, request_overrides or {}
        # a single "search URL": the catalogue query travels in the token call
        search_urls = ["qobuz://catalog-search"]
        self.search_size_per_page = self.search_size_per_source
        # return
        return search_urls
    '''_parsesearchresultfromitem'''
    def _parsesearchresultfromitem(self, item: dict, request_overrides: dict = None) -> SongInfo:
        # init
        request_overrides, song_info = request_overrides or {}, SongInfo(source=self.source)
        # catalogue fields
        title, track_id = item.get("title") or "", item.get("id")
        if not title or not track_id:
            return song_info
        artist = ((item.get("performer") or {}).get("name") or "")
        album = ((item.get("album") or {}).get("title") or "")
        album_cover = ((item.get("album") or {}).get("image") or {})
        cover_url = album_cover.get("large") or album_cover.get("small") or ""
        duration_in_secs = int(item.get("duration") or 0)
        # resolve a real FLAC stream URL for this track
        stream_url = resolve_qobuz_stream_url(track_id)
        if not stream_url:
            return None
        # verify the CDN link and learn its real size
        download_url_status: dict = self.audio_link_tester.test(url=stream_url, request_overrides={'headers': self.default_download_headers}, renew_session=True)
        if not download_url_status.get('ok'):
            return None
        # Qobuz FLAC streams carry no .flac suffix on the URL, so the verified
        # ext from the link tester is meaningless; the CDN serves FLAC bytes
        download_url_status['ext'] = 'flac'
        song_info = SongInfo(
            raw_data={'search': item, 'download': {'url': stream_url}}, source=self.source,
            song_name=legalizestring(title), singers=legalizestring(artist) if artist else 'NULL',
            album=legalizestring(album) if album else 'NULL', ext='flac',
            file_size_bytes=download_url_status.get('file_size_bytes') or 0,
            file_size=download_url_status.get('file_size') or 'NULL',
            identifier=str(track_id), duration_s=duration_in_secs,
            duration=SongInfoUtils.seconds2hms(duration_in_secs) if duration_in_secs else 'NULL',
            lyric='NULL', cover_url=cover_url, download_url=download_url_status.get('download_url') or stream_url,
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
            items = search_qobuz_catalog(keyword, limit=min(self.search_size_per_page, QOBUZ_SEARCH_LIMIT_CAP))
            for item in items[:QOBUZ_RESOLVE_CAP]:
                song_info = None
                with suppress(Exception): song_info = self._parsesearchresultfromitem(item, request_overrides)
                if not isinstance(song_info, SongInfo) or not song_info.with_valid_download_url: continue
                song_infos.append(song_info)
                if self.strict_limit_search_size_per_page and len(song_infos) >= self.search_size_per_page: break
            progress.update(task_id, description=f'{self.source}._search >>> {len(song_infos)} results for "{keyword}"')
        # failure
        except Exception as err:
            progress.update(task_id, description=f'{self.source}._search >>> {keyword} (Error: {err})')
            self.logger_handle.error(f'{self.source}._search >>> {keyword} (Error: {err})', disable_print=self.disable_print)