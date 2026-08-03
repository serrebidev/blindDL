# Optional sites

Adult sites are off by default. Turn on **Enable adult sites** in Settings to
add them, then pick which ones are searched with Ctrl+Shift+S. Some sites take
a link rather than a search, and a few need an account through **Use cookies
from browser** or an authentication file selected in Settings.

BlindDL does not implement these sites itself. It drives these projects:

- [EchterAlsFake's unofficial API set](https://github.com/EchterAlsFake) and
  [`eaf_base_api`](https://github.com/EchterAlsFake/eaf_base_api) — the provider
  packages listed in [`requirements-adult.txt`](../requirements-adult.txt)
- [`aebn-vod-downloader`](https://github.com/hyper440/aebn-vod-downloader)
- [yt-dlp](https://github.com/yt-dlp/yt-dlp), for everything with a built-in
  extractor

Licenses are in [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md). The
authentication files are read only from the path you choose; BlindDL never
copies their contents, does not accept DRM device keys, and does not decrypt
protected media.
