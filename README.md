# BlindDL

A vibe-coded, screen-reader-friendly desktop media downloader for Windows, macOS, and Linux, built for fast music searches, dependable downloads, and full keyboard access.

[![Join SerrebiProjects on Telegram](https://img.shields.io/badge/Telegram-SerrebiProjects-2CA5E0?style=for-the-badge&logo=telegram&logoColor=white)](https://t.me/SerrebiProjects)
[![License: MIT](https://img.shields.io/badge/License-MIT-green?style=for-the-badge)](LICENSE)

**Have a question, hit a bug, or want news about upcoming releases?** Join the [SerrebiProjects Telegram group](https://t.me/SerrebiProjects) — the community hub for BlindDL and my other projects, and the fastest place to get help.

## Features

- Downloads single videos, playlists, and whole channels from any link yt-dlp supports.
- Plays audio or video straight from a pasted URL, and previews search results before you commit to a download.
- Searches dozens of music services from one box, and saves tracks tagged, with cover art and synced lyrics where a service provides them.
- Finds free ebooks, audiobooks, and Internet Archive media — old-time radio, live concerts, movies, and classic TV — from the same search box.
- Returns results as each site answers instead of waiting for the slowest one, and lets you choose which sites are searched.
- Opens multi-item links as a checked list, so you take everything or only the items you want.
- Subscribes to playlists, channels, hashtags, and search pages, then checks for and downloads new items automatically.
- Runs as many simultaneous downloads as you choose, with no artificial limit.
- Includes a Library tab that finds and plays finished downloads, including media in subfolders.
- Updates its downloader components — yt-dlp and friends — from inside the app.
- Checks for new BlindDL releases, verifies the download checksum, and starts
  the correct platform update from inside the app.
- Uses native controls, labeled fields, status-bar announcements, context menus, and complete keyboard operation.

Optional adult and account-based sites are supported and switched off by default; see [docs/optional-sites.md](docs/optional-sites.md) if you want them.

## Download and install

Grab the latest build from the [Releases page](https://github.com/serrebidev/blindDL/releases).

**Windows installer (recommended)**

1. Download `blindDL-Setup-vX.Y.Z-windows-x64.exe`.
2. Run it to install BlindDL and add it to the Start Menu.

The installer contains BlindDL's private Python runtime, libtorrent, Deno,
FFmpeg, FFprobe, and VLC. You do not need to install Python, pip, a torrent
client, media tools, or developer software.

**Windows portable**

1. Download `blindDL-vX.Y.Z-windows-x64.zip`.
2. Extract it anywhere and run `blindDL.exe` — no installation required.

The portable ZIP contains the same complete runtime as the installer.

Bundled components update together through BlindDL releases, so updating the
app also updates yt-dlp, music and site backends, libtorrent, Deno, FFmpeg, and
the media runtime. Release builds pull the current dependency and yt-dlp
pre-release versions before packaging them.

**macOS**

1. Download the DMG matching your Mac: `macos-arm64` for Apple silicon or `macos-x64` for an Intel Mac.
2. Open it and copy BlindDL to Applications.
3. On the first launch, you may need to choose Open from Finder because the first release is not notarized with a paid Apple certificate.

**Debian, Ubuntu, and Linux Mint**

1. Download the `.deb` matching your processor: `amd64` for most PCs or `arm64` for ARM computers.
2. Install it with `sudo apt install ./blinddl_*.deb`.

The Debian packages are built on Ubuntu 24.04 for current Debian-family distributions. `apt` installs their native media-library dependencies automatically; Python and pip are not required.

**Other Linux distributions**

1. Download the matching Linux `.tar.gz`.
2. Extract it and run `./install.sh`.

The installer sets BlindDL up for your user account and obtains native media libraries through apt, dnf, pacman, or zypper when needed. Packaged releases contain BlindDL's Python runtime and Deno; Windows and macOS builds also contain FFmpeg, FFprobe, libtorrent, and VLC.

## Run from source

1. Install Python 3.12 or newer and Git.
2. Install dependencies: `pip install -r requirements.txt`
3. Launch it: `python main.py`

On Debian-family Linux, install `python3-wxgtk4.0`, `python3-wxgtk-media4.0`, `ffmpeg`, `libvlc5`, `vlc-plugin-base`, and `git` first, then create the virtual environment with `--system-site-packages`. Windows and macOS release builds bundle the VLC playback runtime. On Windows, BlindDL can install Deno and FFmpeg with winget; on macOS, it uses Homebrew when available.

## Searching

The Search source combo box switches between music, books, audiobooks, and the Internet Archive's radio, music, movie, and TV collections. Every source searches its sites in parallel and fills the list as they answer, and the result columns rename themselves to suit — a book search reads Title, Author, Library, Year, Size.

Books prefer EPUB and plain text over scanned PDFs, land in a `Books` subfolder, and open in whatever reader you already use. Audiobooks download as a folder of numbered chapters and resume where they stopped if you cancel. One Internet Archive item is often a whole series, so choosing a single result opens the same checked list used for playlists.

Ctrl+Shift+S chooses which sites each source searches, and newly supported sites are enabled automatically. Anna's Archive results resolve through the public LibGen mirrors; if you have a membership, put its key in Settings to use the fast partner servers instead.

## Torrents

The Torrents source searches Knaben, The Pirate Bay, EZTV, Nyaa, Torrents-CSV, LimeTorrents, BitSearch and the Internet Archive at once. Archive torrents are the dependable ones: every item is seeded by the Archive itself, so they download at full speed even with no other peers, and one torrent brings a whole item rather than a single file. Tools, My torrent indexers adds your own Prowlarr or Jackett instance, which is how private trackers are reached — that tool already holds the login and the passkey, so BlindDL never stores a tracker password.

A chosen torrent opens in whatever BitTorrent client you already use. Tick **Download torrents in BlindDL** in Settings, Torrents and BlindDL downloads it itself instead: progress, speed and the swarm's seed and peer counts appear in the Downloads tab, and finished files land in the Library with everything else. It offers to install libtorrent the first time you switch it on.

That page also holds a separate folder for torrents, download and upload speed limits, how many run at once, the peer connection limit, seeding limits by ratio and by time, the incoming port and whether the router is asked to forward it, encryption, sequential download for playing a file before it finishes, and a SOCKS5 or HTTP proxy for swarm traffic. Seeding carries on after a download finishes, under those limits; Stop seeding on the Downloads tab ends it early.

BlindDL joins swarms as the current qBittorrent release, which is what trackers that check the client expect to see. The version is looked up from qBittorrent's own releases once a day, and Settings can pin a particular one.

## Subscriptions

The Subscriptions tab follows a source and queues whatever appears there next. The Add subscription field takes a playlist, a channel, a hashtag page, or a search results page — a `watch?v=...&list=...` link subscribes to the playlist rather than the one video — plus the equivalents on the other sites yt-dlp supports. Shorthand works too: `@handle`, `#hashtag`, a bare playlist id, or a channel id.

Subscribing to a channel by its plain address follows every tab it publishes, so nothing is missed. Hashtag and search feeds are ranked rather than chronological and reshuffle between visits, so BlindDL reads only their top 100 entries; already-seen items are never queued twice.

**Download existing items** in the Add dialog queues everything currently listed — leave it clear to start from now on. Checks run in the background at the interval in Settings, and Ctrl+Shift+C checks everything immediately.

## Building

Install the requirements and PyInstaller, then run `build.bat` on Windows or `./build.sh` on macOS and Linux. Native packages are written to `release/`.

GitHub Actions builds the Windows installer and portable ZIP, DMGs for Intel and Apple silicon Macs, and Linux tarballs and Debian packages for x64 and ARM64 whenever a version tag such as `v0.1.0` is pushed.

## Keyboard shortcuts

- Ctrl+1 / 2 / 3 / 4 / 5 — URL / Search / Downloads / Library / Subscriptions tabs
- Ctrl+L — jump to the URL field
- Ctrl+F — jump to search
- Ctrl+O — open the download folder
- Ctrl+, — open Settings
- Ctrl+U — check for updates
- Ctrl+Shift+C — check all subscriptions now
- Ctrl+Shift+S — choose which sites are searched

## The system tray

Closing the window and minimizing it both put BlindDL in the system tray, where Windows+B reaches it. Queued downloads, seeding torrents and subscription checks keep running while it is there; the tray icon's menu and its double-click bring the window back. File, Exit always exits for real. Either behaviour can be switched off in Settings, Window.

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
