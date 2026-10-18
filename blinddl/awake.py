# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Keep the computer awake while something is downloading.

A playlist left running overnight is much of the point of a downloader,
and a machine that fell asleep at eleven downloaded none of it. The hold
is released the moment the queue runs dry, and it is tied to this
process: Windows clears it when the thread that asked for it ends, and
the child processes on the other systems wait on blindDL's own process
id. A crash therefore cannot leave a computer that will not sleep, which
would be a worse bug than the one this fixes.
"""

import ctypes
import os
import shutil
import subprocess
import sys
import threading
import time

# How often the Windows hold is renewed, and how often the child processes
# on the other systems look at whether blindDL is still running.
RENEW_SECONDS = 30

# SetThreadExecutionState flags: this thread is asking continuously, and
# what it is asking for is the system, not the display.
_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001

_lock = threading.Lock()
_held = False
_keeper = None
_child = None


def supported():
    """Whether this system has a way of being asked not to sleep."""
    if sys.platform == "win32":
        return True
    return bool(_child_command())


def held():
    """True while the computer is being kept awake."""
    return _held


def hold():
    """Ask the system not to sleep until release(). True when it agreed."""
    global _held, _keeper, _child
    with _lock:
        if _held:
            return True
        if sys.platform == "win32":
            _held = True
            _keeper = threading.Thread(
                target=_windows_keeper, daemon=True, name="blinddl-awake")
            _keeper.start()
            return True
        child = _start_child()
        if child is None:
            return False
        _child = child
        _held = True
        return True


def release():
    """Let the computer sleep again. Safe to call when nothing is held."""
    global _held, _child
    with _lock:
        if not _held:
            return
        _held = False
        child, _child = _child, None
    if child is not None:
        _stop_child(child)


def _windows_keeper():
    """Hold the awake state on one thread for as long as it is wanted.

    SetThreadExecutionState belongs to the thread that called it, so the
    hold has to live on a thread of its own: a download worker that took
    it and then went back to the queue would drop it the moment that call
    ended. When this thread stops, Windows clears the state by itself.
    """
    try:
        while _held:
            _set_windows_state(True)
            time.sleep(RENEW_SECONDS)
    finally:
        _set_windows_state(False)


def _set_windows_state(active):
    """Ask Windows for ES_CONTINUOUS, with or without ES_SYSTEM_REQUIRED."""
    ctypes.windll.kernel32.SetThreadExecutionState(
        _ES_CONTINUOUS | (_ES_SYSTEM_REQUIRED if active else 0))
    return True


def _child_command():
    """The command that asks this system not to sleep, or None.

    Both of them watch blindDL's process id and end when it does, so a
    killed blindDL cannot leave an inhibitor running behind it.
    """
    if sys.platform == "darwin":
        caffeinate = shutil.which("caffeinate")
        if not caffeinate:
            return None
        # -i keeps the system awake; -w waits for that process to exit.
        return [caffeinate, "-i", "-w", str(os.getpid())]
    systemd_inhibit = shutil.which("systemd-inhibit")
    if not systemd_inhibit:
        return None
    loop = (f"while kill -0 {os.getpid()} 2>/dev/null; "
            f"do sleep {RENEW_SECONDS}; done")
    return [
        systemd_inhibit, "--what=idle:sleep", "--mode=block",
        "--why=blindDL is downloading", "sh", "-c", loop,
    ]


def _start_child():
    command = _child_command()
    if command is None:
        return None
    try:
        return subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
    except OSError:
        return None


def _stop_child(child):
    try:
        child.terminate()
        child.wait(timeout=5)
    except (OSError, subprocess.SubprocessError):
        pass
