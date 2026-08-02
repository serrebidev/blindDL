# BlindDL

A vibe-coded, screen-reader-friendly desktop media downloader for Windows, macOS, and Linux, built for fast music searches, dependable downloads, and full keyboard access.

[![Join SerrebiProjects on Telegram](https://img.shields.io/badge/Telegram-SerrebiProjects-2CA5E0?style=for-the-badge&logo=telegram&logoColor=white)](https://t.me/SerrebiProjects)
[![License: MIT](https://img.shields.io/badge/License-MIT-green?style=for-the-badge)](LICENSE)

**Have a question, hit a bug, or want news about upcoming releases?** Join the [SerrebiProjects Telegram group](https://t.me/SerrebiProjects) — the community hub for BlindDL and my other projects, and the fastest place to get help.

## Features

- Downloads individual videos, playlists, and whole channels from links supported by yt-dlp.
- Searches Deezer and every enabled musicdl service from one search box.
- Finds music across Spotify, TIDAL, SoundCloud, Netease, QQ, Kugou, Kuwo, Migu, and dozens of other services.
- Downloads Deezer tracks, albums, playlists, and artists as tagged music with cover art and synced lyrics.
- Lets you choose exactly which music sites are searched, with newly supported sites enabled automatically.
- Returns fast search results without waiting for the slowest provider; late results continue appearing as they arrive.
- Opens multi-item links as a checked list so you can download everything or only the items you want.
- Runs as many simultaneous downloads as you choose, with no artificial download limit.
- Subscribes to playlists and channels, then checks for and downloads new items automatically.
- Updates yt-dlp nightly builds, musicdl, Side B, wxPython, Deno, and FFmpeg from inside the app.
- Uses native wxWidgets controls, labeled fields, status-bar announcements, and complete keyboard operation.
- Includes context menus for batch actions in search results, downloads, and subscriptions.

## Download and install

Grab the latest build from the [Releases page](https://github.com/serrebidev/blindDL/releases).

**Windows installer (recommended)**

1. Download `blindDL-Setup-vX.Y.Z-windows-x64.exe`.
2. Run it to install BlindDL and add it to the Start Menu.

**Windows portable**

1. Download `blindDL-vX.Y.Z-windows-x64.zip`.
2. Extract it anywhere and run `blindDL.exe` — no installation required.

**macOS**

1. Download the DMG matching your Mac: `macos-arm64` for Apple silicon or `macos-x64` for an Intel Mac.
2. Open it and copy BlindDL to Applications.
3. On the first launch, you may need to choose Open from Finder because the first release is not notarized with a paid Apple certificate.

**Debian, Ubuntu, and Linux Mint**

1. Download the `.deb` matching your processor: `amd64` for most PCs or `arm64` for ARM computers.
2. Install it with `sudo apt install ./blinddl_*.deb`.

The Debian packages are built on Debian 12 for current Debian-family distributions and install FFmpeg as a system dependency.

**Other Linux distributions**

1. Download the matching Linux `.tar.gz`.
2. Extract it and run `./install.sh`.

The installer sets BlindDL up for your user account and can obtain FFmpeg through apt, dnf, pacman, or zypper when needed. Packaged releases include Deno; Windows and macOS builds also include FFmpeg.

## Run from source

1. Install Python 3.12 or newer and Git.
2. Install dependencies: `pip install -r requirements.txt`
3. Launch it: `python main.py`

On Debian-family Linux, install `python3-wxgtk4.0`, `ffmpeg`, and `git` first, then create the virtual environment with `--system-site-packages`. On Windows, BlindDL can install Deno and FFmpeg with winget; on macOS, it uses Homebrew when available.

## Building

Install the requirements and PyInstaller, then run `build.bat` on Windows or `./build.sh` on macOS and Linux. Native packages are written to `release/`.

GitHub Actions builds the Windows installer and portable ZIP, DMGs for Intel and Apple silicon Macs, and Linux tarballs and Debian packages for x64 and ARM64 whenever a version tag such as `v0.1.0` is pushed.

## Keyboard shortcuts

- Ctrl+1 / 2 / 3 / 4 — URL / Search / Downloads / Subscriptions tabs
- Ctrl+L — jump to the URL field
- Ctrl+F — jump to search
- Ctrl+O — open the download folder
- Ctrl+, — open Settings
- Ctrl+U — check for updates
- Ctrl+Shift+C — check all subscriptions now
- Ctrl+Shift+S — choose which music sites to search

## Config and downloads

Settings and subscriptions live in `%APPDATA%\blindDL` on Windows, `~/Library/Application Support/blindDL` on macOS, and `${XDG_CONFIG_HOME:-~/.config}/blindDL` on Linux.

Temporary search files stay under the platform configuration directory and are cleared at startup, so only finished downloads land in your chosen download folder.

## Contributing

Pull requests are welcome. If BlindDL has been useful to you, open a PR with a fix or feature and I'll review it.

## License

BlindDL is under the [MIT license](LICENSE) — use it, change it, redistribute it, or package it for a distro repository, no permission needed. Every source file carries an `SPDX-License-Identifier: MIT` header so packaging tools can pick the license up automatically.

Bundled dependencies keep their own licenses; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## Community and support

Report bugs and request features in [Issues](https://github.com/serrebidev/blindDL/issues). For questions, feedback, and release news, join the [SerrebiProjects Telegram group](https://t.me/SerrebiProjects).
