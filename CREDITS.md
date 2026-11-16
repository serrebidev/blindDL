# Credits

BlindDL exists because downloaders are built for mice and windows you have to watch. This one is made for fast music searches, dependable downloads, and full keyboard access, screen reader first.

Third-party projects it uses:

- yt-dlp (any link yt-dlp supports downloads).
- libtorrent for torrent downloads.
- FFmpeg, FFprobe, Node.js LTS, VLC, and Deno (downloaded by the app's updater on first run).
- Music metadata and lookup sources: MusicBrainz and TheAudioDB.
- SponsorBlock's crowd-sourced markers for cutting sponsor reads and intros from YouTube downloads.
- Internet Archive and Soulseek as media sources.

Vendored code, kept in the repository:

- musicdl from [CharlesPikachu/musicdl](https://github.com/CharlesPikachu/musicdl) (version 2.13.6, PolyForm Noncommercial License 1.0.0), vendored in `musicdl/`; see `musicdl/VENDORED.md`.
- Side B 0.1.0 from [mosaddiqdev/sideb](https://github.com/mosaddiqdev/sideb) (MIT), vendored in `sideb/`; the upstream repository was deleted, so `sideb/VENDORED.md` keeps its license.
- Three music clients ported from [MusicGrabber](https://gitlab.com/g33kphr33k/musicgrabber) (The Unlicense).

The full runtime dependency and license list is in `THIRD_PARTY_NOTICES.md`, including wxPython, yt-dlp, Requests, Mutagen, aioslsk, PyCryptodome, Deno, FFmpeg, python-vlc/libVLC, and feedparser.

musicdl's PolyForm Noncommercial license restricts use of that component; review `musicdl/LICENSE` before using a blindDL binary commercially.

Those services and data sources belong to their own projects and communities. BlindDL only searches them and files the downloads.

Questions and bug reports are welcome in the [SerrebiProjects Telegram group](https://t.me/SerrebiProjects). More serrebi projects: [projects.serrebiradio.com](https://projects.serrebiradio.com).


## About this project

- GitHub profile: [serrebidev](https://github.com/serrebidev)
- Repository (GitHub): [https://github.com/serrebidev/blindDL](https://github.com/serrebidev/blindDL)
- Repository (Forgejo): [https://git.serrebiradio.com/serrebi/blindDL](https://git.serrebiradio.com/serrebi/blindDL)
- Projects site: [projects.serrebiradio.com](https://projects.serrebiradio.com)
- Questions and bug reports: [SerrebiProjects Telegram group](https://t.me/SerrebiProjects)
