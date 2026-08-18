# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Making blindDL the thing that opens a torrent file or a magnet link.

Registering is deliberately per-user. Writing the machine-wide entries
needs administrator rights, which would mean a consent prompt every time
somebody changed their mind, and blindDL is a program one person runs
rather than something an administrator deploys. The Windows installer
writes the machine-wide half once, with the rights it already has; this is
what the Settings button and the first-run question use afterwards.

Windows keeps the user's own answer to "what opens this?" in a UserChoice
key that only the Settings app is allowed to write -- deliberately, so that
a program cannot quietly make itself the default. So registering here makes
blindDL an available handler and the default where nothing has claimed one;
where Windows has already recorded a choice it may still ask the user to
confirm the change. The wording in the dialog says so rather than promising
something the system will not allow.
"""

import os
import shutil
import subprocess
import sys

# The names Windows files blindDL's handlers under. They are ours, so they
# are prefixed; a bare "torrent" would collide with whatever else is
# installed.
TORRENT_PROG_ID = "blindDL.torrent"
MAGNET_PROG_ID = "blindDL.magnet"
TORRENT_EXTENSION = ".torrent"
MAGNET_SCHEME = "magnet"

# What Linux calls the same two things.
TORRENT_MIME = "application/x-bittorrent"
MAGNET_MIME = "x-scheme-handler/magnet"
DESKTOP_FILE = "blinddl.desktop"


def supported():
    """Whether blindDL can register itself as a handler on this system."""
    return sys.platform in ("win32", "linux")


def launcher_command():
    """The command line that opens one link, with %1 where the link goes.

    A frozen build is its own executable. Running from a checkout there is
    no executable to point at, so the interpreter is asked for the module,
    which is what a developer testing this actually wants to launch.
    """
    if getattr(sys, "frozen", False):
        return f'"{os.path.abspath(sys.executable)}" "%1"'
    return f'"{os.path.abspath(sys.executable)}" -m blinddl "%1"'


# -- Windows ---------------------------------------------------------------


def _winreg():
    import winreg  # noqa: PLC0415 - Windows only

    return winreg


def _classes_root():
    """HKCU\\Software\\Classes: the per-user half of the association store."""
    winreg = _winreg()
    return winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, r"Software\Classes",
                              0, winreg.KEY_READ | winreg.KEY_WRITE)


def _write_prog_id(prog_id, label, scheme=False):
    """Describe one handler and what to run for it."""
    winreg = _winreg()
    command = launcher_command()
    with _classes_root() as classes:
        with winreg.CreateKey(classes, prog_id) as key:
            winreg.SetValueEx(key, None, 0, winreg.REG_SZ, label)
            if scheme:
                # What marks a key as a protocol rather than a file type.
                # Its presence is the signal; the value is ignored.
                winreg.SetValueEx(key, "URL Protocol", 0, winreg.REG_SZ, "")
        with winreg.CreateKey(classes,
                              rf"{prog_id}\shell\open\command") as key:
            winreg.SetValueEx(key, None, 0, winreg.REG_SZ, command)
        with winreg.CreateKey(classes, rf"{prog_id}\DefaultIcon") as key:
            winreg.SetValueEx(key, None, 0, winreg.REG_SZ,
                              f"{os.path.abspath(sys.executable)},0")


def _read_default(path):
    """The default value of one Classes key, or "" when it is not there."""
    winreg = _winreg()
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            rf"Software\Classes\{path}") as key:
            value, _kind = winreg.QueryValueEx(key, None)
            return str(value or "")
    except OSError:
        return ""


def _register_windows():
    _write_prog_id(TORRENT_PROG_ID, "BitTorrent file")
    _write_prog_id(MAGNET_PROG_ID, "URL:Magnet Link", scheme=True)
    winreg = _winreg()
    with _classes_root() as classes:
        # The extension points at our handler, and is also listed as one of
        # the programs offered under "Open with" whatever the default is.
        with winreg.CreateKey(classes, TORRENT_EXTENSION) as key:
            winreg.SetValueEx(key, None, 0, winreg.REG_SZ, TORRENT_PROG_ID)
        with winreg.CreateKey(
                classes,
                rf"{TORRENT_EXTENSION}\OpenWithProgids") as key:
            winreg.SetValueEx(key, TORRENT_PROG_ID, 0, winreg.REG_NONE, b"")
        # A scheme has no extension to point at: the scheme key is the
        # handler, so the same command is written straight onto it.
        with winreg.CreateKey(classes, MAGNET_SCHEME) as key:
            winreg.SetValueEx(key, None, 0, winreg.REG_SZ, "URL:Magnet Link")
            winreg.SetValueEx(key, "URL Protocol", 0, winreg.REG_SZ, "")
        with winreg.CreateKey(classes,
                              rf"{MAGNET_SCHEME}\shell\open\command") as key:
            winreg.SetValueEx(key, None, 0, winreg.REG_SZ, launcher_command())
    return True


def _registered_windows():
    if _read_default(TORRENT_EXTENSION) != TORRENT_PROG_ID:
        return False
    command = _read_default(rf"{MAGNET_SCHEME}\shell\open\command")
    return os.path.abspath(sys.executable).casefold() in command.casefold()


# -- Linux -----------------------------------------------------------------


def _desktop_path():
    data_home = (os.environ.get("XDG_DATA_HOME")
                 or os.path.expanduser("~/.local/share"))
    return os.path.join(data_home, "applications", DESKTOP_FILE)


def _register_linux():
    """Write a desktop entry claiming both types, then make it the default.

    The entry is rewritten rather than assumed: a distribution package may
    have installed one without the MimeType line, and the user asking for
    this is asking for the line to be there.
    """
    path = _desktop_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    executable = (os.path.abspath(sys.executable)
                  if getattr(sys, "frozen", False)
                  else f"{os.path.abspath(sys.executable)} -m blinddl")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Name=blindDL\n"
            "Comment=Accessible media downloader\n"
            f"Exec={executable} %u\n"
            "Terminal=false\n"
            "Categories=AudioVideo;Audio;Network;\n"
            "StartupNotify=true\n"
            f"MimeType={TORRENT_MIME};{MAGNET_MIME};\n"
        )
    if shutil.which("update-desktop-database"):
        _run(["update-desktop-database", os.path.dirname(path)])
    if not shutil.which("xdg-mime"):
        # The entry is written and will be picked up; only the "make it the
        # default" step needs the tool.
        return False
    ok = _run(["xdg-mime", "default", DESKTOP_FILE, TORRENT_MIME])
    return _run(["xdg-mime", "default", DESKTOP_FILE, MAGNET_MIME]) and ok


def _run(command):
    try:
        return subprocess.run(command, capture_output=True,
                              timeout=20).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _registered_linux():
    if not os.path.exists(_desktop_path()):
        return False
    if not shutil.which("xdg-mime"):
        return False
    for mime in (TORRENT_MIME, MAGNET_MIME):
        try:
            result = subprocess.run(["xdg-mime", "query", "default", mime],
                                    capture_output=True, text=True,
                                    timeout=20)
        except (OSError, subprocess.SubprocessError):
            return False
        if DESKTOP_FILE not in (result.stdout or ""):
            return False
    return True


# -- what the rest of blindDL calls ---------------------------------------


def is_registered():
    """Whether blindDL currently opens torrent files and magnet links.

    Never raises: a registry or desktop-database that cannot be read is
    reported as "not registered", which at worst offers the user a button
    that turns out to be unnecessary.
    """
    try:
        if sys.platform == "win32":
            return _registered_windows()
        if sys.platform == "linux":
            return _registered_linux()
    except Exception:  # noqa: BLE001 - a broken lookup is not an error here
        return False
    return False


def register():
    """Claim both types for blindDL. Returns whether it took effect.

    False means the entries were written but the system did not confirm
    blindDL as the default -- on Windows because the user has already
    chosen something else and only they can change it, on Linux because
    xdg-mime is not installed.
    """
    try:
        if sys.platform == "win32":
            _register_windows()
            return _registered_windows()
        if sys.platform == "linux":
            return _register_linux()
    except Exception:  # noqa: BLE001 - reported to the user as a failure
        return False
    return False
