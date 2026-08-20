# BlindDL

A vibe-coded, screen-reader-friendly desktop media downloader for Windows, macOS, and Linux, built for fast music searches, dependable downloads, and full keyboard access.

[![Join SerrebiProjects on Telegram](https://img.shields.io/badge/Telegram-SerrebiProjects-2CA5E0?style=for-the-badge&logo=telegram&logoColor=white)](https://t.me/SerrebiProjects)
[![License: MIT](https://img.shields.io/badge/License-MIT-green?style=for-the-badge)](LICENSE)

**Have a question, hit a bug, or want news about upcoming releases?** Join the [SerrebiProjects Telegram group](https://t.me/SerrebiProjects) — the community hub for BlindDL and my other projects, and the fastest place to get help.

## Features

- Downloads single videos, playlists, and whole channels from any link yt-dlp supports.
- Plays audio or video straight from a pasted URL, and previews search results before you commit to a download.
- Searches dozens of music services from one box, and saves tracks tagged, with cover art and synced lyrics where a service provides them.
- Searches Mixcloud for the DJ sets, radio shows, and long-form mixes none of the track-shaped services carry - on its own, and as part of a Music sites search.
- Fills in the details a music site left out — album artist, track and disc numbers, year, genre, cover art, and the MusicBrainz ids a music library files by — from MusicBrainz and TheAudioDB. Turn it off in Settings, Downloads.
- Finds free ebooks, audiobooks, and Internet Archive media — old-time radio, live concerts, movies, and classic TV — from the same search box.
- Starts every selected music site concurrently on background threads, returns results as each site answers, and batches GUI updates so screen readers stay responsive.
- Asks supported sites for best-match, newest, or most-popular results, while clearly naming sites that cannot provide the chosen order.
- Opens multi-item links and album track lists as a checked list, nothing ticked to start with, and plays a clip or the whole of any track in it before you choose.
- Keeps results you are not ready to download on a Download queue tab, where they survive restarts, play on demand, and download whenever you say so.
- Plays a short sound when downloads finish and a different one when one fails, so the outcome reaches you from another window. Choose your own sounds, or switch them off, in Settings, Interface.
- Files a playlist, channel, artist page, album, or Internet Archive item into a folder named after it, so its tracks arrive together instead of loose among everything else. A single video still lands straight in the download folder.
- Keeps running and finished transfers in separate lists on the Downloads and Uploads tabs, and can clear the finished ones for you — see Settings, Downloads.
- Subscribes to playlists, channels, hashtags, and search pages with a per-feed order, then checks for and downloads new items automatically.
- Runs as many simultaneous downloads as you choose, with no artificial limit.
- Picks a dropped Deezer transfer up where it stopped instead of failing it, and never leaves you with nothing for a track Deezer will only serve at a lower bitrate than you asked for.
- Includes a Library tab that finds and plays finished downloads, including media in subfolders.
- Updates its downloader components — yt-dlp and friends — from inside the app.
- When automatic updates are enabled, checks on startup and every 12 hours,
  downloads and verifies the release, waits for queued transfers to finish,
  installs it silently, and restarts BlindDL without a Download update step.
- Searches music by track title, album, or artist, and downloads a whole album as every track on it. Album rows say how many tracks are on them, so a single is not mistaken for a record.
- Opens the album or the artist behind any Deezer or Apple Music result, so a search is somewhere to browse from rather than a list that ends.
- Uses native controls, labeled fields, status-bar announcements, context menus, and complete keyboard operation.
- Speaks its status-bar announcements through NVDA, JAWS, and friends, and shows them on a Braille display, so a finished search or a failed download arrives on its own. Turn it off in Settings, Window.

Optional adult and account-based sites are supported and switched off by default; see [docs/optional-sites.md](docs/optional-sites.md) if you want them.

## Download and install

Grab the latest build from the [Releases page](https://github.com/serrebidev/blindDL/releases).

**Windows installer (recommended)**

1. Download `blindDL-Setup-vX.Y.Z-windows-x64.exe`.
2. Run it to install BlindDL and add it to the Start Menu.

The installer contains BlindDL's private Python runtime, yt-dlp, libtorrent,
and all Python application libraries. You do not need to install Python, pip,
or a torrent client. On first run, the Windows build obtains Deno, FFmpeg,
FFprobe, Node.js LTS, and VLC through WinGet.

An Installing media tools window opens while that happens. It names each tool
as its download starts and again when it finishes, and speaks every step, so a
setup that takes several minutes can be followed rather than waited out. Hide
closes the window and the installation carries on without it; the result is
spoken either way.

**Windows portable**

1. Download `blindDL-vX.Y.Z-windows-x64.zip`.
2. Extract it anywhere and run `blindDL.exe` — no installation required.

The portable ZIP uses the same automatic background setup as the installer.

Bundled components update together through BlindDL releases. Large Windows
media tools update through WinGet, so they are not duplicated inside every
BlindDL update. Release builds pull the current dependency and yt-dlp
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

The installer sets BlindDL up for your user account and obtains native media libraries through apt, dnf, pacman, or zypper when needed. Packaged releases contain BlindDL's Python runtime. Windows obtains large native tools through WinGet; macOS packages contain their required media runtimes.

## Run from source

1. Install Python 3.12 or newer and Git.
2. Install dependencies: `pip install -r requirements.txt`
3. Launch it: `python main.py`

On Debian-family Linux, source developers install `python3-wxgtk4.0`, `python3-wxgtk-media4.0`, and `git` first, then create the virtual environment with `--system-site-packages`. Released builds need no Python: BlindDL provisions Deno, FFmpeg, Node.js LTS, and VLC in a background worker through WinGet on Windows, Homebrew on macOS, or the Linux system package manager. The Linux `.deb` declares the native packages as dependencies and the portable installer installs them automatically. Deno has a user-local fallback that needs no administrator access. An operating-system authorization prompt can still appear when Linux or macOS requires permission to install system software.

## Searching

The Search source combo box switches between music, books, audiobooks, and the Internet Archive's radio, music, movie, and TV collections. **Music sites** searches everything at once - four dozen music sites, Deezer, Side B, and Mixcloud - while **YouTube**, **SoundCloud**, **Mixcloud**, **Bandcamp**, **Apple Music**, and **Deezer** each search one service on their own. Every one of those single-service choices asks for 200 results, the same depth blindDL holds every provider to, so choosing one is never a thinner search than leaving it in the crowd. Every source searches its sites in parallel and fills the list as they answer, and the result columns rename themselves to suit — a book search reads Title, Author, Library, Year, Size.

**Search type** decides what counts as a match for a music search: Best match, Track title, Album, or Artist. Track title and Artist match that field alone, so an artist search cannot be answered with a song that merely mentions the name. **Album** returns whole releases instead of tracks — pressing Enter on one opens the same checked list used for playlists and queues every track you tick. Nothing is ticked when the list opens, and Preview and Play full song work on the track you are reading, so you can listen through a release and keep the two tracks you wanted. Every album row also says how many tracks it holds, which is what tells a record from a single without opening either. Deezer and Apple Music are the two sources with a catalogue to search this way; on the Music sites choice an album search therefore asks Deezer alone rather than burying a handful of albums under several hundred tracks, and the status announcement says so. Book, torrent, Internet Archive, and Soulseek searches have no such fields, so the control is switched off for them.

**Order** changes the request sent to each site: Best match, Most recent, or Most popular. It therefore changes which page of results arrives. **Sort by** only rearranges the rows already in the list, so it takes effect the moment you choose it. Not every provider exposes every order; BlindDL keeps that provider's best-match results and names it in the status announcement instead of pretending a locally rearranged page is the requested search.

Source, Search type, and Order describe the *next* search rather than starting one, so you can walk each list to the option you want without a search running underneath you or the focus jumping into the results. Press Enter — from the query box or from any of those lists — or choose Search when you are ready. Enter while a list is open belongs to that list: it picks the option being read and nothing else, so opening a combo box with Alt+Down, arrowing to what you want and pressing Enter never starts a search.

Whole albums download into a folder named for the artist and the album, and an Artist search files what you download from it under that artist's name, so a release arrives together instead of a track at a time among everything else.

A result's own context menu turns its album and artist back into places to go rather than two columns to read. **Show album tracks** replaces the list with the release that track came off, in running order, so you can check what is on it before queueing any of it; on an album row it opens the same list without downloading anything. **Show artist's releases** lists everything that artist has out — albums, EPs, singles and compilations alike, each row saying which it is and how many tracks are on it — and any of those opens as its own track list in turn. **Go back to previous results**, Alt+Left, or Backspace steps back out one level at a time, all the way to the search you started from. Deezer and Apple Music results have a catalogue behind them and offer this; a file found on a music site has an artist's name and nothing to look it up in, so it is not offered.

**Mixcloud** is where the DJ sets, radio shows, and hours-long mixes are, and none of the track-shaped services carry them. Its rows are credited to the host who put the mix together rather than to an artist, and the Duration column is worth reading there more than anywhere else: it is the difference between a three-minute edit and a four-hour set. Mixcloud's own search takes a query and nothing else, so asking for newest or most popular gets its best match and the announcement says so - Sort by still rearranges what arrived.

Books prefer EPUB and plain text over scanned PDFs, land in a `Books` subfolder, and open in whatever reader you already use. Audiobooks download as a folder of numbered chapters and resume where they stopped if you cancel. One Internet Archive item is often a whole series, so choosing a single result opens the same checked list used for playlists.

**The Download queue tab** (Ctrl+3) is a shelf for what you found and are not ready to download. Ctrl+Shift+Q, the Add to download queue button, or the result's context menu puts the selected rows on it, with everything the search knew about them. They stay there across restarts; Preview and Play full song work there exactly as they do in the results list, so you can decide later and by ear. Download selected or Download all hands them to the Downloads tab and takes them off the queue, and Delete removes a row without downloading it. An album kept this way stays one row until you ask for it, so a whole discography can be shelved in one press.

Ctrl+Shift+S chooses which sites each source searches, and newly supported sites are enabled automatically. Anna's Archive results resolve through the public LibGen mirrors; if you have a membership, put its key in Settings to use the fast partner servers instead.

## Soulseek

Soulseek is an optional peer-to-peer backend. Enable it and enter an account on the **Soulseek** Settings page, or use **Sign in or sign up** there: Soulseek registers an unused username during its first successful login. Once connected, Search gains four Soulseek-only choices for music and audio, movies and video, books and documents, and `.torrent` files. The ordinary Music, Internet Archive, book, torrent, YouTube, and adult choices continue to search only their named sites, avoiding duplicate and unrelated peer results. Each Soulseek result identifies the peer, shows its remote folder, and reports its free-slot, queue, and average-speed information. Its context menu can download the file or its whole containing folder, browse the peer, send a message, add the peer as a friend, grant upload priority, or view the peer's profile. Downloads use Soulseek's remote queue and report progress, speed, ETA, errors, and cancellation in BlindDL's Downloads tab.

Soulseek downloads use the ordinary BlindDL download folder. That Library folder is shared publicly by default, including files completed while BlindDL is running. The Settings page can turn Library sharing off or add any number of other shared folders. Peers who share nothing themselves are refused your files by default; **Refuse uploads to users who share nothing** on that page turns this off, and friends and free-slot priority users can always download from you whatever they share. It also controls the public and obfuscated listening ports, UPnP forwarding, connection obfuscation, simultaneous upload slots, upload and download limits, the result cap, and the public profile description. Sharing and uploads continue while BlindDL is hidden in the system tray and stop on File, Exit.

Enabling Soulseek also adds **Chat** and **Messages** tabs. Chat lists available and remembered rooms, accepts any typed room name, joins and leaves public rooms, and creates or joins an invited private room when **Private room** is checked. Room and direct-message transcripts, joined rooms, private-room choices, friends, and free-slot priorities are restored on the next run. Messages exposes the friend list with presence status; select a friend to address a message, or type a username directly. Friends can be added and removed from that tab. Browse opens an accessible folder tree and file list with a local filter; folders can be navigated in either view, and both views offer file and recursive-folder downloads. Profile, message, friend, and free-slot-priority actions are available there too.

The **Uploads** tab immediately after Downloads combines live Soulseek uploads from shared folders with torrents that are still seeding. It shows the service, peer, progress, speed, and torrent ratio. Downloads and uploads both accept multi-selection, and their context menus offer the actions that apply to the selected states: start or resume, pause, cancel or stop, restart, open or show data, remove history, delete with data after confirmation, clear finished items, select all, and clear selection. Both tabs are split in two: what is still running is the whole of the first list, and **Finished downloads** or **Finished uploads** below it holds what is over, so a long history is never arrowed through to reach a live transfer. **Clear finished downloads and uploads automatically** in Settings, Downloads empties those sections as transfers complete; downloads that failed or were cancelled are kept either way, so their error can still be read, and a torrent that is still seeding keeps its row until seeding stops. The download queue is saved atomically: active downloads resume as queued work after a restart, paused downloads stay paused, Soulseek keeps its transfer cache, and completed torrents that were still uploading are reattached to libtorrent's resume data. Re-adding a known completed download skips it, while re-adding a failed or cancelled partial resumes its existing queue row. Stopped seeds stay stopped.

## Torrents

The Torrents source searches Knaben, The Pirate Bay, EZTV, Nyaa, Torrents-CSV, LimeTorrents, BitSearch and the Internet Archive at once. Archive torrents are the dependable ones: every item is seeded by the Archive itself, so they download at full speed even with no other peers, and one torrent brings a whole item rather than a single file. Tools, My torrent indexers adds your own Prowlarr or Jackett instance, which is how private trackers are reached — that tool already holds the login and the passkey, so BlindDL never stores a tracker password.

A chosen torrent opens in whatever BitTorrent client you already use. Tick **Download torrents in BlindDL** in Settings, Torrents and BlindDL downloads it itself instead: progress, speed and the swarm's seed and peer counts appear in the Downloads tab, and finished files land in the Library with everything else. It offers to install libtorrent the first time you switch it on.

That page also holds a separate folder for torrents, download and upload speed limits, how many run at once, the peer connection limit, seeding limits by ratio and by time, the incoming port and whether the router is asked to forward it, encryption, sequential download for playing a file before it finishes, and a SOCKS5 or HTTP proxy for swarm traffic. Seeding carries on after a download finishes, under those limits; Stop seeding on the Downloads or Uploads tab ends it early.

BlindDL joins swarms as the current qBittorrent release, which is what trackers that check the client expect to see. The version is looked up from qBittorrent's own releases once a day, and Settings can pin a particular one.

## Subscriptions

The Subscriptions tab follows a source and queues whatever appears there next. The Add subscription field takes a playlist, a channel, a hashtag page, or a search results page — a `watch?v=...&list=...` link subscribes to the playlist rather than the one video — plus the equivalents on the other sites yt-dlp supports. Shorthand works too: `@handle`, `#hashtag`, a bare playlist id, or a channel id.

Subscribing to a channel by its plain address follows every tab it publishes, so nothing is missed. Hashtag and search feeds can follow Best match, Most recent, or Most popular. Most recent is the default for new subscriptions so a changing trend list does not hide new uploads; existing subscriptions retain their previous best-match behavior until changed. BlindDL reads only the top 100 entries of these ranked feeds, and already-seen items are never queued twice. Channels and playlists keep their natural published or owner-defined order because YouTube does not expose the same feed sort for them.

The Subscriptions tab's **Sort by** control changes only how subscriptions are displayed. It can group by title or site, show recently checked or stale feeds first, rank by tracked-item count, or put enabled feeds first; background checks still use the saved subscription order. Use a subscription's context menu to change its feed order later.

**Download existing items** in the Add dialog queues the newest 100 items currently listed — leave it clear to start from now on. Channel and playlist subscriptions inspect the newest 100 entries on each refresh, so even channels with years of uploads subscribe promptly without repeatedly crawling their complete history. Set **Update interval (hours)** directly on the Subscriptions tab and press **Apply interval**; Ctrl+Shift+C checks everything immediately.

## Building

The default release path runs the credential audit, complete tests, frozen self-test, and native packaging on each maintained build machine. On Windows, install the requirements plus PyInstaller and pytest, then run `build.bat`. On `root@serrebiradio.com`, use a fresh checkout and run `tools/build_linux_release.sh`; it creates a clean release virtual environment, links only Debian's native wxPython package into it, verifies dependency consistency, and writes the x64 tarball and Debian package to `release/`. macOS continues to run `./build.sh` in GitHub Actions. Ordinary source builds can still call `python tools/build_release.py` directly.

Complete builds verify that libtorrent imports before PyInstaller starts. The
Windows `build.bat` release path always reinstalls the newest compatible wheel
from `~/libtorrent-build/wheels`, even if a different libtorrent build is
already importable. Set `BLINDDL_LIBTORRENT_WHEELHOUSE` to use a different
wheelhouse. The Windows `Libtorrent Weekly Update` task and Linux's
`/etc/cron.d/libtorrent-wheel` keep these wheels on the latest stable
libtorrent release and test each one in a clean virtual environment before it
can be used by a release build. Their source-controlled entry points are
`tools/update_libtorrent_windows.ps1` and
`tools/update_libtorrent_linux.sh`.

Release artifacts are built and self-tested on their target operating systems. Windows x64 is built on the maintainer's Windows machine, Linux x64 is built on `serrebiradio.com`, and GitHub Actions continues to build the Intel and Apple-silicon macOS DMGs when a version tag such as `v0.1.0` is published. The Windows and Linux frozen self-tests remove every developer Python path; Linux additionally runs with an empty executable search path. A release is rejected unless its packaged executable identifies its embedded Python and imports its bundled libtorrent.

Because three machines build one release, nobody publishes it by hand.
`scripts/publish_release.py` owns that last step: it checks the draft carries
all ten artifacts, verifies each one against the checksum its builder
published, and only then takes the release out of draft and marks it latest.
The tag build runs it after uploading the macOS DMGs, waiting up to two hours
for the other two hosts. The `Release guard` workflow sweeps every six hours
for a complete release still sitting in draft and publishes it, and fails
loudly once a draft has been incomplete for a day. A draft is invisible on the
Releases page and skipped by the `/releases/latest` endpoint the in-app
updater reads, so one left behind hides a finished release from everybody —
which is what happened to v0.24.24. To hold a draft back deliberately, put
`<!-- no-autopublish -->` in its body. To publish or check one by hand:

```
python scripts/publish_release.py v0.24.25 --check-only
python scripts/publish_release.py v0.24.25
```

## Keyboard shortcuts

- Ctrl+1 / 2 / 3 / 4 / 5 / 6 / 7 — URL / Search / Download queue / Downloads / Uploads / Library / Subscriptions tabs
- Ctrl+8 / 9 — Soulseek Chat / Messages tabs when Soulseek is enabled
- Ctrl+L — jump to the URL field
- Ctrl+F — jump to search
- Ctrl+O — open the download folder
- Ctrl+, — open Settings
- Ctrl+U — check for updates (Help menu)
- Ctrl+Shift+C — check all subscriptions now
- Ctrl+Shift+Q — keep the selected results on the Download queue tab
- Ctrl+Shift+S — choose which sites are searched
- Ctrl+Q — exit for real, even when closing is set to hide in the tray

## The system tray

Closing the window and minimizing it both put BlindDL in the system tray. The high-contrast blue **B** icon shows a notification when the window hides; click it once, press Windows+B, or launch BlindDL again to restore the existing window. Windows may place new notification icons in its tray-overflow menu until you pin them. BlindDL never hides if Windows has not confirmed that its tray icon was installed. Queued downloads, uploads, seeding torrents, chat, and subscription checks keep running while it is there. Closing means every way Windows closes a window, including Alt+F4 and the system menu. File, Exit and Ctrl+Q always exit for real, as does Exit on the tray menu, and the two hide behaviours can be switched off in Settings, Window.

Only one BlindDL instance runs per user. Starting it again does not create a duplicate download queue or duplicate Soulseek connection; it brings the already-running window back from the tray.

## Config and downloads

Settings and subscriptions live in `%APPDATA%\blindDL` on Windows, `~/Library/Application Support/blindDL` on macOS, and `${XDG_CONFIG_HOME:-~/.config}/blindDL` on Linux.

Temporary search files stay under the platform configuration directory and are cleared at startup, so only finished downloads land in your chosen download folder.

A link that holds more than one thing — a playlist, a channel, an artist page, an album, or an Internet Archive item — downloads into a subfolder of that folder named after it, and a subscription files what it publishes under the feed's own name. Anything asked for on its own goes straight into the download folder as before.

## Contributing

Pull requests are welcome. If BlindDL has been useful to you, open a PR with a fix or feature and I'll review it.

## License

BlindDL is under the [MIT license](LICENSE) — use it, change it, redistribute it, or package it for a distro repository, no permission needed. Every source file carries an `SPDX-License-Identifier: MIT` header so packaging tools can pick the license up automatically.

Bundled dependencies keep their own licenses; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## Community and support

Report bugs and request features in [Issues](https://github.com/serrebidev/blindDL/issues). For questions, feedback, and release news, join the [SerrebiProjects Telegram group](https://t.me/SerrebiProjects).
