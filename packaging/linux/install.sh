#!/bin/sh
# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

set -eu

SOURCE_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
TARGET_DIR=${XDG_DATA_HOME:-"$HOME/.local/share"}/blinddl
BIN_DIR="$HOME/.local/bin"
APPS_DIR=${XDG_DATA_HOME:-"$HOME/.local/share"}/applications

install_ffmpeg() {
    if command -v ffmpeg >/dev/null 2>&1; then
        return
    fi
    echo "Installing required FFmpeg package..."
    if command -v apt-get >/dev/null 2>&1; then
        sudo apt-get update
        sudo apt-get install -y ffmpeg
    elif command -v dnf >/dev/null 2>&1; then
        sudo dnf install -y ffmpeg
    elif command -v pacman >/dev/null 2>&1; then
        sudo pacman -S --needed --noconfirm ffmpeg
    elif command -v zypper >/dev/null 2>&1; then
        sudo zypper --non-interactive install ffmpeg
    else
        echo "No supported package manager was found. Install FFmpeg, then rerun this installer." >&2
        exit 1
    fi
}

install_vlc() {
    if ldconfig -p 2>/dev/null | grep -q 'libvlc\.so'; then
        return
    fi
    echo "Installing required VLC playback library..."
    if command -v apt-get >/dev/null 2>&1; then
        sudo apt-get update
        sudo apt-get install -y libvlc5 vlc-plugin-base python3-wxgtk-media4.0
    elif command -v dnf >/dev/null 2>&1; then
        sudo dnf install -y vlc-core
    elif command -v pacman >/dev/null 2>&1; then
        sudo pacman -S --needed --noconfirm vlc
    elif command -v zypper >/dev/null 2>&1; then
        sudo zypper --non-interactive install vlc
    else
        echo "No supported package manager was found. Install VLC, then rerun this installer." >&2
        exit 1
    fi
}

install_ffmpeg
install_vlc
mkdir -p "$TARGET_DIR" "$BIN_DIR" "$APPS_DIR"
cp -R "$SOURCE_DIR"/. "$TARGET_DIR"/
chmod +x "$TARGET_DIR/blindDL"
ln -sf "$TARGET_DIR/blindDL" "$BIN_DIR/blinddl"
cp "$SOURCE_DIR/install.sh" "$TARGET_DIR/install.sh"
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
