# Third-party notices

blindDL is licensed under the MIT License. Its runtime dependencies remain
under their respective licenses. Release packages may include these projects
and their transitive dependencies:

- wxPython — wxWindows Library Licence
- yt-dlp — The Unlicense
- musicdl — PolyForm Noncommercial License 1.0.0
- Side B — MIT License
- Requests — Apache License 2.0
- Mutagen — GNU General Public License v2 or later
- PyCryptodome — BSD/Public Domain
- Deno — MIT License
- FFmpeg — LGPL v2.1 or later, or GPL when built with GPL components

blindDL distributions include the EchterAlsFake adult API provider set. As of
2026-08-02, `eaf_base_api` and the active video API packages are AGPL-3.0,
while the archived Porngo and Sex.com packages are LGPL-3.0. Their source is
available from the upstream repositories identified in `requirements-adult.txt`.
These packages retain their upstream licenses; review those terms before
redistributing blindDL or a combined build.

The dependency metadata and license texts shipped by each project are retained
inside packaged application directories when provided by the dependency.
Nothing in blindDL's MIT License changes or overrides a third party's terms.

In particular, musicdl's PolyForm Noncommercial license restricts use of that
component. Review its license before using a blindDL binary commercially.
