"""Connection diagnostics for Deezer API, Deezer ARL, and YouTube cookies."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx


def _fmt(ok: bool, msg: str) -> str:
    return f"  {'OK' if ok else 'FAIL'}  {msg}"


_TEST_TRACK_ID = "412536982"  # NF - Let You Down (used for geo + lyrics checks)


async def check_deezer_api(timeout: float = 10.0) -> str:
    """Check if api.deezer.com is reachable and tracks are playable from your country."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.get("https://api.deezer.com/album/302127")
            data = r.json()
            if isinstance(data, dict) and "error" not in data:
                title = data.get("title", "?")

                # Check track readable flag — false means Deezer content is
                # blocked / not licensed in the requesting country
                tr = await c.get(f"https://api.deezer.com/track/{_TEST_TRACK_ID}")
                tdata = tr.json()
                if not tdata.get("readable"):
                    return _fmt(False, f"Deezer API reachable (album={title!r}) but tracks NOT playable in your country")

                return _fmt(True, f"Deezer API reachable (album={title!r})")
            return _fmt(False, f"Deezer API returned error: {data}")
    except httpx.TimeoutException:
        return _fmt(False, "Deezer API timeout (10s)")
    except Exception as e:
        return _fmt(False, f"Deezer API error: {e}")


_LYRICS_QUERY = """
query GetLyrics($trackId: String!) {
  track(trackId: $trackId) {
    lyrics {
      text
      synchronizedWordByWordLines { start end words { start end word } }
      synchronizedLines { lrcTimestamp line milliseconds duration }
    }
  }
}
"""


async def check_deezer_arl(arl: str | None, timeout: float = 10.0) -> str:
    """Validate a Deezer ARL by authenticating and trying to fetch lyrics for a known track."""
    if not arl:
        return _fmt(False, "No Deezer ARL configured (set SIDEB_DEEZER_ARL in .env)")
    try:
        user_agent = "sideb/0.1.0"
        async with httpx.AsyncClient(timeout=timeout) as c:
            # Step 1: exchange ARL for JWT
            r = await c.post(
                "https://auth.deezer.com/login/arl",
                params={"jo": "p", "rto": "c", "i": "c"},
                cookies={"arl": arl},
                headers={"User-Agent": user_agent},
            )
            r.raise_for_status()
            data = json.loads(r.text)
            jwt = data.get("jwt")
            if not jwt:
                return _fmt(False, "Deezer ARL login did not return a JWT — ARL may be invalid/expired")

            # Step 2: actually fetch lyrics for a known track
            r2 = await c.post(
                "https://pipe.deezer.com/api",
                headers={"Authorization": f"Bearer {jwt}", "User-Agent": user_agent},
                json={
                    "operationName": "GetLyrics",
                    "variables": {"trackId": _TEST_TRACK_ID},
                    "query": _LYRICS_QUERY,
                },
            )
            r2.raise_for_status()
            payload = r2.json()
            if "errors" in payload:
                return _fmt(False, f"Deezer ARL invalid: {payload['errors'][0].get('message', 'unknown')}")
            lyrics = payload.get("data", {}).get("track", {}).get("lyrics")
            if lyrics and (lyrics.get("text") or lyrics.get("synchronizedLines")):
                return _fmt(True, "Deezer ARL valid — lyrics fetched successfully")
            return _fmt(False, "Deezer ARL returned no lyrics (track may not have lyrics or region blocked)")
    except httpx.TimeoutException:
        return _fmt(False, "Deezer ARL check timeout (10s)")
    except Exception as e:
        return _fmt(False, f"Deezer ARL check error: {e}")


_YOUTUBE_COOKIE_KEYS = {"SAPISID", "__Secure-3PSID", "LOGIN_INFO", "SESSION_TOKEN"}


async def check_youtube_cookies(cookies_file: str | Path | None, timeout: float = 20.0) -> str:
    """Validate YouTube cookies.txt by checking for auth cookies, then testing with yt-dlp."""
    if not cookies_file:
        return _fmt(False, "No cookies.txt configured (set SIDEB_COOKIES_FILE in .env)")
    path = Path(cookies_file)
    if not path.exists():
        return _fmt(False, f"cookies.txt not found at {path}")
    if path.stat().st_size < 50:
        return _fmt(False, "cookies.txt too small (likely empty/invalid)")

    # Parse cookies file for YouTube auth cookies
    found_keys = set()
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 6:
                domain = parts[0]
                if "youtube.com" in domain or "ytimg.com" in domain:
                    name = parts[5]
                    if name in _YOUTUBE_COOKIE_KEYS:
                        found_keys.add(name)
    except Exception:
        pass

    if not found_keys:
        return _fmt(False, "No YouTube auth cookies found (missing SAPISID/__Secure-3PSID)")

    # Test with yt-dlp using a public video
    try:
        TEST_URL = "https://music.youtube.com/watch?v=wxr8AdLXFJ8"
        proc = await asyncio.create_subprocess_exec(
            "yt-dlp", "--cookies", str(path), "--dump-json",
            TEST_URL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return _fmt(False, "yt-dlp timeout (20s)")

        if proc.returncode != 0:
            err = stderr.decode("utf-8", errors="replace")[:200]
            return _fmt(False, f"YouTube cookie check failed: {err.strip()}")
        return _fmt(True, f"YouTube cookies valid (found {len(found_keys)} auth cookies)")
    except FileNotFoundError:
        return _fmt(False, "yt-dlp not found in PATH")
    except Exception as e:
        return _fmt(False, f"YouTube cookie check error: {e}")


async def check_deno() -> str:
    """Check if Deno (JavaScript runtime) is installed — required by yt-dlp for
    YouTube n-challenge / bot-detection bypass."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "deno", "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return _fmt(False, "Deno check timeout (10s)")
        if proc.returncode != 0:
            return _fmt(False, "Deno not found — install via `winget install DenoLand.Deno` (Win) or `brew install deno` (Mac)")
        first_line = stdout.decode("utf-8", errors="replace").splitlines()[0] if stdout else ""
        ver = first_line.replace("deno ", "").strip() if "deno" in first_line else "?"
        return _fmt(True, f"Deno {ver} — yt-dlp will use it for n-challenge bypass")
    except FileNotFoundError:
        return _fmt(False, "Deno not found — install via `winget install DenoLand.Deno` (Win) or `brew install deno` (Mac)")
    except Exception as e:
        return _fmt(False, f"Deno check error: {e}")


async def run_all(arl: str | None, cookies_file: str | Path | None) -> list[str]:
    results = await asyncio.gather(
        check_deezer_api(),
        check_deezer_arl(arl),
        check_youtube_cookies(cookies_file),
        check_deno(),
    )
    return list(results)
