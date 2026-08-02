# blindDL

An accessible Windows, macOS, and Linux media downloader with three engines working
together: search and download music from every site supported by
[musicdl](https://github.com/CharlesPikachu/musicdl) (Deezer, SoundCloud,
Spotify, TIDAL, Netease, QQ, Kugou, Kuwo, Migu, and dozens more), download
Deezer tracks, albums, playlists and artists as properly tagged music with
synced lyrics via [Side B](https://github.com/mosaddiqdev/sideb), and
download literally anything else — videos, playlists, whole channels — via
[yt-dlp](https://github.com/yt-dlp/yt-dlp).

## Features

- **URL download** — paste any video, playlist, or channel URL. Multi-item
  links open a checked list where you can download all or only chosen items.
  Deezer links (track/album/playlist/artist, including page.link
  shortlinks) go to Side B first and fall back to yt-dlp if Side B cannot
  handle them, so every engine that could work gets a turn.
- **Deezer** — Deezer downloads come out as finished music with metadata,
  cover art, tags, and synced lyrics. Add an ARL cookie in Settings to use
  Deezer's original FLAC or MP3 320 stream; without one, Side B obtains opus
  or m4a audio from YouTube Music. Lyrics come from LRCLIB, with word-level
  karaoke lyrics available through the ARL as well. The same Deezer
  catalog search runs alongside the musicdl sites in every music search,
  and Deezer playlists can be followed as subscriptions.
- **Search** — one box, two engines: all musicdl music sites plus Deezer
  (Side B) at once, or YouTube/web via yt-dlp. Every music site is queried
  in parallel and the search reports back after 5 seconds (Settings >
  seconds to wait per site) instead of hanging on the slowest site; sites
  that answer later keep adding to the results list on their own.
- **Choose your sites** — Tools > Choose music sites (Ctrl+Shift+S) lists
  every site musicdl supports, all of them on by default; uncheck the ones
  you never want searched. Sites added by future musicdl updates arrive
  switched on.
- **Unlimited downloads** — no caps; concurrency is your setting (default 4).
- **Subscriptions** — subscribe to playlists/channels (including Deezer
  playlists); new items are downloaded automatically every few hours
  (configurable).
- **Self-updating** — yt-dlp (nightly builds), musicdl, Side B, wxPython,
  Deno (required by yt-dlp for YouTube) and ffmpeg are checked and updated
  automatically at most once a day, or any time via Tools > Check for
  updates.
- **Accessible** — native wxWidgets controls only, labeled fields, full
  keyboard operation, status-bar announcements (NVDA: Insert+End), and
  context menus for batch actions in result, download, and subscription lists.

## Install a release

Download the package for your operating system from the
[GitHub releases page](https://github.com/serrebidev/blindDL/releases):

- **Windows x64:** run `blindDL-Setup-...exe`. A portable ZIP is also provided.
- **macOS Apple Silicon or Intel:** open the matching DMG and copy blindDL to
  Applications. The first launch may require choosing Open in Finder because
  the first release is not notarized with a paid Apple certificate.
- **Debian, Ubuntu, Linux Mint, and derivatives:** download the `.deb` matching
  your processor and run `sudo apt install ./blinddl_*.deb`. Packages are built
  on Debian 12 for compatibility with current Debian-family distributions.
- **Other Linux distributions:** extract the matching `.tar.gz` and run
  `./install.sh`. It installs per-user and obtains FFmpeg from apt, dnf, pacman,
  or zypper when necessary.

The packaged releases include Deno. Windows and macOS packages also include
FFmpeg; Debian packages declare FFmpeg as a system dependency.

## Run from source

```
pip install -r requirements.txt
python main.py
```

On Debian-family Linux, first install `python3-wxgtk4.0`, `ffmpeg`, and `git`,
then create the virtual environment with `--system-site-packages`. On Windows,
the built-in updater can install Deno and FFmpeg with winget; on macOS it uses
Homebrew when available. Side B is installed from GitHub, so git must be
available for a source installation.

## Build release packages

Install the requirements plus PyInstaller, then run `build.bat` on Windows or
`./build.sh` on macOS/Linux. Native artifacts are written to `release/`.
GitHub Actions builds both macOS architectures and Linux/Debian x64 and ARM64
packages whenever a version tag such as `v0.1.0` is pushed.

## Keyboard shortcuts

- Ctrl+1 / 2 / 3 / 4 — URL / Search / Downloads / Subscriptions tabs
- Ctrl+L — jump to the URL field, Ctrl+F — jump to search
- Ctrl+O — open the download folder, Ctrl+, — settings
- Ctrl+U — check for updates, Ctrl+Shift+C — check all subscriptions now
- Ctrl+Shift+S — choose which music sites to search

## Config

Settings and subscriptions live in the platform configuration directory:
`%APPDATA%\blindDL` on Windows, `~/Library/Application Support/blindDL` on
macOS, and `${XDG_CONFIG_HOME:-~/.config}/blindDL` on Linux. musicdl's
per-search scratch files go under the same directory and are wiped at startup,
so only finished downloads ever land in the download folder. musicdl's console
logging and progress bars are suppressed — its own log file still records
everything (`%LOCALAPPDATA%\zcjin\musicdl\Logs\musicdl.log`).

Side B keeps its temp files and local settings under
the platform configuration directory's `sideb-home` folder. It always produces opus (remuxed to .ogg)
or m4a audio — the mp3/flac/wav audio-format setting only applies to
yt-dlp and native Deezer downloads. With an ARL cookie configured, Deezer
links use MP3 320 by default or FLAC when the audio format is set to flac;
if that quality is unavailable, blindDL falls back to Side B automatically.

## Security

Never commit or share a Deezer ARL. It is an account credential. blindDL stores
it only in the user's local configuration file, and CI runs
`scripts/check_no_arl.py` plus Gitleaks before every release.

## License

blindDL is licensed under the [MIT License](LICENSE), matching BlindRSS:
Copyright (c) 2024-2026 serrebidev and contributors. Bundled dependencies keep
their own licenses; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
