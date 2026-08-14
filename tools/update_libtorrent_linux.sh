#!/usr/bin/env bash
# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

# Build a relocatable CPython 3.14 libtorrent wheel from the latest stable tag.
# Intended deployment: /usr/local/sbin/update-libtorrent-wheel on Linux.

set -Eeuo pipefail

BASE=${LIBTORRENT_BUILD_ROOT:-/root/libtorrent-build}
REPO="$BASE/libtorrent"
BOOST_VERSION=${BOOST_VERSION:-1.90.0}
BOOST_DIR="$BASE/boost_${BOOST_VERSION//./_}"
BOOST_CONFIG="$BASE/user-config.jam"
B2="$BOOST_DIR/b2"
BUILD="$BASE/build"
STAGE="$BASE/stage"
WHEELS="$BASE/wheels"
MARKER="$BASE/installed_version_linux.txt"
PACKAGER="$BASE/package_libtorrent_wheel.py"
TOOLS_VENV="$BASE/tools-venv"
UV=${UV:-/root/.local/bin/uv}

mkdir -p "$BASE" "$WHEELS"
exec 9>"$BASE/update.lock"
if ! flock -n 9; then
  echo "Another libtorrent wheel update is already running."
  exit 0
fi

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Required command is missing: $1" >&2
    exit 1
  fi
}

for command in cmake curl g++ git patchelf tar; do
  require_command "$command"
done
if [[ ! -x "$UV" ]]; then
  echo "uv is missing: $UV" >&2
  exit 1
fi
if [[ ! -f "$PACKAGER" ]]; then
  echo "Wheel packager is missing: $PACKAGER" >&2
  exit 1
fi

"$UV" python install 3.14
PYTHON=$("$UV" python find 3.14)
PYTHON_INCLUDE=$(
  "$PYTHON" -c 'import sysconfig; print(sysconfig.get_path("include"))'
)

if [[ ! -d "$REPO/.git" ]]; then
  git clone --recurse-submodules https://github.com/arvidn/libtorrent.git "$REPO"
fi
git -C "$REPO" fetch --tags --force
LATEST_TAG=$(
  git -C "$REPO" tag --list 'v*' --sort=-version:refname |
    awk '/^v[0-9]+\.[0-9]+\.[0-9]+$/ && !found { print; found=1 }'
)
if [[ -z "$LATEST_TAG" ]]; then
  echo "No stable libtorrent release tag was found." >&2
  exit 1
fi
LATEST_COMMIT=$(git -C "$REPO" rev-list -n 1 "$LATEST_TAG")
RUNTIME_LABEL=$(dpkg-query -W \
  -f='${binary:Package}=${Version};' \
  libssl3t64 libzstd1 zlib1g | sort | tr -d '\n')
LATEST_LABEL="$LATEST_TAG@$LATEST_COMMIT|runtime=$RUNTIME_LABEL"
VERSION=${LATEST_TAG#v}

wheel_for_version() {
  compgen -G "$WHEELS/libtorrent-${VERSION}+*-cp314-cp314-*.whl" >/dev/null
}

if [[ -f "$MARKER" ]] && [[ $(<"$MARKER") == "$LATEST_LABEL" ]] && wheel_for_version; then
  echo "Already up to date: $LATEST_LABEL"
  exit 0
fi

BOOST_ARCHIVE="$BASE/boost_${BOOST_VERSION//./_}.tar.bz2"
if [[ ! -x "$B2" ]] && [[ -x "$BOOST_DIR/tools/build/src/engine/b2" ]]; then
  B2="$BOOST_DIR/tools/build/src/engine/b2"
fi
if [[ ! -x "$B2" ]]; then
  if [[ ! -f "$BOOST_ARCHIVE" ]]; then
    curl --fail --location --retry 3 \
      "https://archives.boost.io/release/$BOOST_VERSION/source/$(basename "$BOOST_ARCHIVE")" \
      --output "$BOOST_ARCHIVE"
  fi
  tar -xjf "$BOOST_ARCHIVE" -C "$BASE"
  "$BOOST_DIR/bootstrap.sh" --with-python="$PYTHON"
  if [[ ! -x "$B2" ]] && [[ -x "$BOOST_DIR/tools/build/src/engine/b2" ]]; then
    B2="$BOOST_DIR/tools/build/src/engine/b2"
  fi
  if [[ ! -x "$B2" ]]; then
    echo "Boost b2 was not produced under $BOOST_DIR." >&2
    exit 1
  fi
fi

printf 'using python : 3.14 : %s : %s : ;\n' \
  "$PYTHON" "$PYTHON_INCLUDE" >"$BOOST_CONFIG"

(
  cd "$BOOST_DIR"
  nice -n 10 ionice -c 2 -n 7 "$B2" \
    --user-config="$BOOST_CONFIG" \
    --with-python \
    variant=release threading=multi address-model=64 \
    link=static runtime-link=shared \
    "cxxflags=-fPIC -Wno-deprecated-declarations" python=3.14 stage
)

git -c advice.detachedHead=false -C "$REPO" checkout --force "$LATEST_TAG"
git -C "$REPO" submodule update --init --recursive

cmake -S "$REPO" -B "$BUILD" --fresh \
  -Wno-dev \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_INSTALL_PREFIX="$STAGE" \
  -DCMAKE_POSITION_INDEPENDENT_CODE=ON \
  -DBUILD_SHARED_LIBS=OFF \
  -Dpython-bindings=ON \
  -Dpython-install-system-dir=OFF \
  -DPython3_EXECUTABLE="$PYTHON" \
  -DBoost_ROOT="$BOOST_DIR" \
  -DBoost_NO_SYSTEM_PATHS=ON \
  -DBoost_NO_WARN_NEW_VERSIONS=ON \
  -DBoost_USE_STATIC_LIBS=ON \
  -Dboost-python-module-name=python314
nice -n 10 ionice -c 2 -n 7 cmake --build "$BUILD" --config Release --parallel "$(nproc)"
cmake --install "$BUILD" --config Release

EXTENSION=$(find "$STAGE" -type f -name 'libtorrent*.so' -print -quit)
if [[ -z "$EXTENSION" ]]; then
  echo "The libtorrent Python extension was not installed under $STAGE." >&2
  exit 1
fi

"$UV" venv --clear --python "$PYTHON" "$TOOLS_VENV"
"$UV" pip install --python "$TOOLS_VENV/bin/python" --upgrade auditwheel
"$TOOLS_VENV/bin/python" "$PACKAGER" \
  --extension "$EXTENSION" \
  --repo "$REPO" \
  --outdir "$WHEELS" \
  --stamp "$(date +%Y%m%d)"

WHEEL=$(find "$WHEELS" -maxdepth 1 -type f \
  -name "libtorrent-${VERSION}+*-cp314-cp314-*.whl" \
  -printf '%T@ %p\n' | sort -nr | head -1 | cut -d' ' -f2-)
if [[ -z "$WHEEL" ]]; then
  echo "The repaired libtorrent wheel was not found." >&2
  exit 1
fi

TEST_VENV="$BASE/wheel-test"
"$UV" venv --clear --python "$PYTHON" "$TEST_VENV"
"$UV" pip install --python "$TEST_VENV/bin/python" "$WHEEL"
"$TEST_VENV/bin/python" -c \
  'import libtorrent, pathlib; print(libtorrent.__version__, pathlib.Path(libtorrent.__file__).resolve())'
"$TOOLS_VENV/bin/python" -m auditwheel show "$WHEEL"

printf '%s\n' "$LATEST_LABEL" >"$MARKER"
echo "Updated to $LATEST_LABEL"
