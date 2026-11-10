# Releasing blindDL

Releases live on Forgejo at `serrebi/blindDL`, and the in-app updater reads
them from there. GitHub is a secondary mirror: while that account is
reachable its `release.yml` leg builds the Apple Silicon macOS DMG, but a
release never waits on it.

Every release carries six artifacts: Windows x64 (installer, zip,
checksums) and Linux x64 (tarball, .deb, checksums), plus the macOS DMG
with its checksums when the secondary leg ran. Intel Macs are not built.
`scripts/publish_release.py` publishes a draft only when everything it was
told to expect is in place. Tags are `vX.Y.Z` and the release title is the
tag. Release notes are house prose: an opening one-line summary, no bullet
lists, no version numbers in headings.

Start every release by bumping `__version__` in `blinddl/__init__.py` and
committing `chore: release X.Y.Z` to `main`. Every command below needs
`FORGEJO_TOKEN` in the environment (host: the user variable; VPS:
`/root/forgejo-muse-token.txt`), including `--check-only`: drafts are
invisible to unauthenticated reads.

## On the Windows release host

1. Run `build.bat` (no argument). It tests and builds `release/` with the
   weekly libtorrent wheel.
2. Push `main`, then tag and push the tag:
   `git tag vX.Y.Z` and `git push forgejo main vX.Y.Z`. The tag must point
   at the pushed release commit.
3. Create the draft **after** pushing the tag:
   `python scripts/publish_release.py vX.Y.Z --create --notes-file notes.md --assets "release/*"`.
   Pushing a tag auto-publishes a matching Forgejo draft, so a draft made
   first would go out with only the Windows artifacts on it. The macOS
   leg's upload polls for the draft rather than failing without it.
4. Build Linux in local WSL: `ssh wsl-build bash -s -- vX.Y.Z < tools/build_linux_remote.sh`
   (debian:trixie container; prints the release dir), `scp wsl-build:<dir>/* release/`, then run
   `python scripts/publish_release.py vX.Y.Z --upload --assets "release/*"`.
5. Publish once every platform has landed:
   `python scripts/publish_release.py vX.Y.Z [--skip-macos]`. Without the
   macOS leg, `--skip-macos` is required or the missing DMG blocks the
   publish. The script verifies the checksums and confirms
   `/releases/latest` afterwards.
6. `python scripts/publish_release.py --sweep` catches a complete release
   left in draft and fails loudly on one incomplete for a day. To hold a
   draft back deliberately, put `<!-- no-autopublish -->` in its body.

To publish or check one by hand:

```
python scripts/publish_release.py vX.Y.Z --check-only
python scripts/publish_release.py vX.Y.Z --skip-macos
```

## Cloud agents

There is no cloud release path: the old `cloud-release.yml` built every
platform on hosted runners and died with the GitHub account. A release
needs the Windows host (and the Linux VPS); agents without them stop after
the `chore: release X.Y.Z` commit and say so.
