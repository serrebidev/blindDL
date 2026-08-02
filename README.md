# BlindDL

A vibe-coded, screen-reader-friendly desktop media downloader for Windows, macOS, and Linux, built for fast music searches, dependable downloads, and full keyboard access.

[![Join SerrebiProjects on Telegram](https://img.shields.io/badge/Telegram-SerrebiProjects-2CA5E0?style=for-the-badge&logo=telegram&logoColor=white)](https://t.me/SerrebiProjects)
[![License: MIT](https://img.shields.io/badge/License-MIT-green?style=for-the-badge)](LICENSE)

**Have a question, hit a bug, or want news about upcoming releases?** Join the [SerrebiProjects Telegram group](https://t.me/SerrebiProjects) — the community hub for BlindDL and my other projects, and the fastest place to get help.

## Features

- Downloads individual videos, playlists, and whole channels from links supported by yt-dlp.
- Plays audio or video directly from a pasted URL without downloading it first.
- Searches Deezer and every enabled musicdl service from one search box.
- Previews audio and video from the Search results list.
- Searches and downloads through all 17 EchterAlsFake `unofficial-api-for-*`
  providers. Adult features are disabled by default; enabling them adds
  separate straight, gay, lesbian, bisexual, and trans search choices.
- Searches public ThisVid videos and expands playlist-view URLs into supported
  public or browser-authenticated video downloads through yt-dlp.
- Searches the gay-only MyMuscleVideo catalog and expands playlist URLs into
  standard public or browser-authenticated video downloads.
- Downloads straight and gay AEBN movie URLs with title, performer, duration,
  cancellation, and progress support.
- Downloads ordinary media that the signed-in user can access from OnlyFans
  and JustForFans creator or post URLs; protected/DRM media is skipped.
- Downloads BoyfriendTV video URLs through a native MP4/HLS extractor.
- Finds music across Spotify, TIDAL, SoundCloud, Netease, QQ, Kugou, Kuwo, Migu, and dozens of other services.
- Downloads Deezer tracks, albums, playlists, and artists as tagged music with cover art and synced lyrics.
- Lets you choose exactly which music sites are searched, with newly supported sites enabled automatically.
- Returns fast search results without waiting for the slowest provider; late results continue appearing as they arrive.
- Opens multi-item links as a checked list so you can download everything or only the items you want.
- Runs as many simultaneous downloads as you choose, with no artificial download limit.
- Subscribes to playlists and channels, then checks for and downloads new items automatically.
- Updates yt-dlp nightly builds, musicdl, Side B, wxPython, Deno, and FFmpeg from inside the app.
- Uses native wxWidgets controls, labeled fields, status-bar announcements, and complete keyboard operation.
- Includes context menus for actions in search results, downloads, Library, and subscriptions.
- Includes a Library tab that finds and plays completed downloads, including media in subfolders.

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

The Debian packages are built on Ubuntu 24.04 for current Debian-family distributions and install FFmpeg as a system dependency.

**Other Linux distributions**

1. Download the matching Linux `.tar.gz`.
2. Extract it and run `./install.sh`.

The installer sets BlindDL up for your user account and can obtain FFmpeg through apt, dnf, pacman, or zypper when needed. Packaged releases include Deno; Windows and macOS builds also include FFmpeg.

## Run from source

1. Install Python 3.12 or newer and Git.
2. Install dependencies: `pip install -r requirements.txt`
3. Launch it: `python main.py`

Adult providers are included by the normal dependency installation and in
packaged releases. Adult features are disabled by default. Enabling **Enable
adult sites** in Settings adds separate Straight, Gay, Lesbian, Bisexual, and
Trans porn choices to the Search source combo box. Search-capable providers
can be selected in the Search sites dialog. Gay searches reject explicit
female, bisexual, and trans metadata even when a provider mixes categories;
query-only sources must also provide positive gay/male evidence. MissAV and
HQPorner are offered only under Straight porn because they have no reliable gay
catalog filter.
Beeg, Porngo, AEBN, OnlyFans, and JustForFans currently support URL downloads
only. SpankBang, Thumbzilla, and archived Sex.com are also URL-only while their
public search pages block, hang, or no longer match their upstream parsers.
BoyfriendTV URLs work through blindDL's native extractor. ThisVid public search
and URL downloads use the bundled yt-dlp extractor. MyMuscleVideo is included
only in Gay porn searches, and its playlist URLs expand into individual queue
items. Entries that redirect to signup require an eligible account through
browser cookies.
AEBN support uses the MIT-licensed `aebn-vod-downloader`; the other
adult API libraries retain their upstream licenses and require Python 3.12 or
newer.

For sites already signed into in a local browser, **Use cookies from browser**
in Settings lets yt-dlp read that browser profile. This is useful for standard
login-only MyMuscleVideo and ThisVid pages.

OnlyFans and JustForFans use user-controlled authentication JSON files selected
in Settings. blindDL stores only each file path, never a copy of its session
values. An OnlyFans file uses the `ofd`-compatible non-DRM fields:

```json
{
  "cookie": "auth_id=YOUR_ID; sess=YOUR_SESSION",
  "x_bc": "YOUR_X_BC_HEADER",
  "user_agent": "THE_MATCHING_BROWSER_USER_AGENT"
}
```

A JustForFans file uses the cookie and account ID visible on an authenticated
`ajax/getPosts.php` browser request:

```json
{
  "cookie": "userhash4=YOUR_HASH; OTHER_SESSION_COOKIES",
  "user_id": "YOUR_NUMERIC_ACCOUNT_ID",
  "user_agent": "YOUR_BROWSER_USER_AGENT"
}
```

Treat these files like passwords. Only ordinary MP4, HLS, image, audio, and GIF
media is supported. blindDL does not accept DRM device keys or decrypt
protected OnlyFans/JustForFans media.

On Debian-family Linux, install `python3-wxgtk4.0`, `python3-wxgtk-media4.0`,
`ffmpeg`, `libvlc5`, `vlc-plugin-base`, and `git` first, then create the virtual environment with
`--system-site-packages`. Windows and macOS release builds bundle the VLC
playback runtime. On Windows, BlindDL can install Deno and FFmpeg with winget;
on macOS, it uses Homebrew when available.

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
- Ctrl+Shift+S — choose which music and adult sites to search

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
