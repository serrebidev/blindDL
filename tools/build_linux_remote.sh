#!/bin/bash
# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT
#
# Build the Linux release for one tag on the Linux release host (local WSL
# Ubuntu, ssh alias wsl-build) in a throwaway debian:trixie container, with the
# same system packages as the Linux CI job. Run from the Windows host:
#
#   ssh wsl-build bash -s -- vX.Y.Z < tools/build_linux_remote.sh
#
# The last line of output is the host directory holding the release assets;
# copy them back with scp and upload with scripts/publish_release.py --upload.
# The container (glibc 2.41) keeps the binaries runnable on Debian 13 and
# older distros; the WSL host itself is newer.
set -euo pipefail

# Host caches mounted into the container: apt .debs, pip wheels.
tag="$1"
work="$(mktemp -d /tmp/blinddl-linux-XXXXXX)"
git clone --quiet --depth 1 --branch "$tag" https://git.serrebiradio.com/serrebi/blindDL.git "$work/src" >&2

docker run --rm --cpu-shares 26214 \
  -v /var/cache/release-build/blinddl-apt:/var/cache/apt/archives \
  -v /var/cache/release-build/pip:/root/.cache/pip \
  -v "$work/src:/src" -w /src debian:trixie bash -c '
  set -e
  export DEBIAN_FRONTEND=noninteractive
  rm -f /etc/apt/apt.conf.d/docker-clean  # keep .debs in the mounted host cache
  apt-get update -qq
  apt-get install -y -qq --no-install-recommends \
    ca-certificates git python3 python3-venv python3-dev python3-pip \
    python3-wxgtk4.0 python3-wxgtk-media4.0 xvfb xauth dbus \
    build-essential binutils dpkg-dev ffmpeg nodejs \
    gstreamer1.0-plugins-base gstreamer1.0-plugins-good >/dev/null
  git config --global --add safe.directory /src
  sh tools/build_linux_release.sh
' >&2

echo "$work/src/release"
