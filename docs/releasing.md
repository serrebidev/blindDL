# Releasing blindDL

Every release carries eight artifacts: Windows x64 (installer, zip, checksums), Linux x64 (tarball, .deb, checksums) and the Apple Silicon macOS DMG with its checksums. Intel Macs are not built. `scripts/publish_release.py` publishes a draft only when all eight are in place. Tags are `vX.Y.Z` and the release title is the tag. Release notes are house prose: an opening one-line summary, no bullet lists, no version numbers in headings.

Start every release by bumping `__version__` in `blinddl/__init__.py` and committing `chore: release X.Y.Z` to `main`.

## On the Windows release host

1. Run `build.bat` (no argument). It tests and builds `release/` with the weekly libtorrent wheel.
2. Create the draft **before** pushing the tag: `gh release create vX.Y.Z --draft --latest=false release/* --notes-file notes.md`. If the tag comes first, release.yml's macOS publish fails with "release not found".
3. Push `main` and the tag. `release.yml` builds the Apple Silicon DMG.
4. Build Linux on `root@serrebiradio.com` from the tag with `tools/build_linux_release.sh`, then upload its three files.
5. `release.yml` (or `python scripts/publish_release.py vX.Y.Z`) publishes once all eight are present.

## Muse agent and cloud agents only

The Muse agent and cloud agents, which have no Windows host, use `.github/workflows/cloud-release.yml`, which builds every platform on GitHub runners. Never use it on the release host, and never run it while a host release is in progress.

- `gh workflow run cloud-release.yml -f dry_run=true` runs `build.bat` on Windows and `tools/build_linux_release.sh` in a `debian:trixie` container, uploading both as workflow artifacts. It tags and publishes nothing.
- `gh workflow run cloud-release.yml -f dry_run=false -f notes="$(cat notes.md)"` does the real release. It builds both platforms, creates the draft with the notes, creates the tag, then dispatches `release.yml` on the tag and waits for it. A tag created with `GITHUB_TOKEN` starts no workflow, so the dispatch is required. `release.yml` builds macOS and publishes, then the workflow checks `/releases/latest`.
- Watch with `gh run watch <id> --exit-status`. If `release.yml` fails, re-run its failed jobs. Do not start a new cloud release, because the tag already exists.
- Windows uses the maintained CPython 3.14 libtorrent wheel from the `libtorrent-wheels` pre-release, which is never Latest and never a `v*` tag. After the weekly wheel build, refresh it with `gh release upload libtorrent-wheels <new .whl> --clobber`; the newest stamp wins.
