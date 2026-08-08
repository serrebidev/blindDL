# Side B, vendored

This directory is a copy of Side B 0.1.0, taken from the released wheel.

It used to be a dependency: `sideb @ git+https://github.com/mosaddiqdev/sideb`.
That repository was deleted in August 2026 — the URL now returns 404 and the
project was never published to PyPI — which broke every blindDL build. The
code is kept here so Side B search and downloads keep working.

Nothing in here is modified. Fixes go upstream if the project ever returns;
until then blindDL maintains it. Its dependencies (`httpx`, `pydantic`,
`pydantic-settings`, `questionary`, `rich`, `ytmusicapi`, `mutagen`, `yt-dlp`)
are declared in blindDL's own `requirements.txt` and `pyproject.toml`.

Side B is MIT licensed; the licence follows, and `THIRD_PARTY_NOTICES.md`
records it alongside blindDL's other dependencies.

---

MIT License

Copyright (c) Side B contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
