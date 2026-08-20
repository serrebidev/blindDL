#!/bin/sh
# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

set -eu

SOURCE_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
TARGET_DIR=${XDG_DATA_HOME:-"$HOME/.local/share"}/blinddl
BIN_DIR="$HOME/.local/bin"
APPS_DIR=${XDG_DATA_HOME:-"$HOME/.local/share"}/applications

install_native_tools() {
    if command -v ffmpeg >/dev/null 2>&1 && \
       command -v node >/dev/null 2>&1 && \
       ldconfig -p 2>/dev/null | grep -q 'libvlc\.so'; then
        return
    fi
    echo "Installing required FFmpeg, Node.js, and VLC packages..."
    if command -v apt-get >/dev/null 2>&1; then
        sudo apt-get update
        sudo apt-get install -y --no-install-recommends curl unzip ffmpeg nodejs vlc
    elif command -v dnf >/dev/null 2>&1; then
        sudo dnf install -y curl unzip ffmpeg nodejs vlc
    elif command -v pacman >/dev/null 2>&1; then
        sudo pacman -S --needed --noconfirm curl unzip ffmpeg nodejs vlc
    elif command -v zypper >/dev/null 2>&1; then
        sudo zypper --non-interactive install curl unzip ffmpeg nodejs vlc
    else
        echo "No supported package manager was found. Install FFmpeg, Node.js, and VLC, then rerun this installer." >&2
        exit 1
    fi
}

install_deno() {
    if command -v deno >/dev/null 2>&1 || [ -x "$HOME/.deno/bin/deno" ]; then
        return
    fi
    case "$(uname -m)" in
        x86_64|amd64) DENO_TARGET=deno-x86_64-unknown-linux-gnu ;;
        aarch64|arm64) DENO_TARGET=deno-aarch64-unknown-linux-gnu ;;
        *) echo "No automatic Deno package for $(uname -m)." >&2; exit 1 ;;
    esac
    DENO_VERSION=$(curl --fail --silent --show-error \
        https://dl.deno.land/release-latest.txt)
    case "$DENO_VERSION" in
        v2.*) ;;
        *) echo "Unsupported Deno release: $DENO_VERSION" >&2; exit 1 ;;
    esac
    echo "Installing Deno $DENO_VERSION for the current user..."
    DENO_DIR="$HOME/.deno/bin"
    mkdir -p "$DENO_DIR"
    DENO_ARCHIVE="$DENO_DIR/deno-download.zip"
    if ! curl --fail --location --silent --show-error \
        "https://dl.deno.land/release/$DENO_VERSION/$DENO_TARGET.zip" \
        --output "$DENO_ARCHIVE"; then
        rm -f "$DENO_ARCHIVE"
        echo "Could not download Deno." >&2
        exit 1
    fi
    unzip -o "$DENO_ARCHIVE" -d "$DENO_DIR"
    rm -f "$DENO_ARCHIVE"
    chmod +x "$DENO_DIR/deno"
}

install_native_tools
install_deno
mkdir -p "$BIN_DIR" "$APPS_DIR"

# Copying straight over an installed blindDL writes into the file a running
# one is executing from -- "Text file busy" -- and it leaves behind every
# file the previous release shipped that this one does not. The new tree is
# built beside the old one and takes its place in a single rename instead,
# which is also what makes this safe to run as a self-update.
STAGE_DIR="$TARGET_DIR.new"
PREVIOUS_DIR="$TARGET_DIR.previous"
rm -rf "$STAGE_DIR" "$PREVIOUS_DIR"
mkdir -p "$STAGE_DIR"
cp -R "$SOURCE_DIR"/. "$STAGE_DIR"/
chmod +x "$STAGE_DIR/blindDL"
cp "$SOURCE_DIR/install.sh" "$STAGE_DIR/install.sh"
if [ -d "$TARGET_DIR" ]; then
    mv "$TARGET_DIR" "$PREVIOUS_DIR"
fi
if ! mv "$STAGE_DIR" "$TARGET_DIR"; then
    if [ -d "$PREVIOUS_DIR" ]; then
        mv "$PREVIOUS_DIR" "$TARGET_DIR"
    fi
    echo "blindDL could not be put in place." >&2
    exit 1
fi
rm -rf "$PREVIOUS_DIR"
ln -sf "$TARGET_DIR/blindDL" "$BIN_DIR/blinddl"
cat > "$APPS_DIR/blinddl.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=blindDL
Comment=Accessible media downloader
Exec=$BIN_DIR/blinddl
Terminal=false
Categories=AudioVideo;Audio;Network;
StartupNotify=true
EOF

echo "blindDL installed. Run: $BIN_DIR/blinddl"

# blindDL closes before handing the install over, so a self-update would
# otherwise end with the application gone. Only its own updater sets this;
# a person running the installer from a terminal gets the line above.
if [ "${BLINDDL_RESTART:-}" = "1" ]; then
    "$BIN_DIR/blinddl" >/dev/null 2>&1 &
fi
