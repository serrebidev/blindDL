# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""Speak the status bar out loud through whatever screen reader is running.

blindDL writes what it is doing to the status bar, which a screen reader
only reads when it is asked to (NVDA+End). Everything that matters -- a
search finishing, a download failing -- therefore had to be gone looking
for. This module says those same messages out loud as they happen, so
nothing has to be monitored by hand.

The preferred path is accessible-output2, which reaches NVDA, JAWS,
System Access, ZDSR and Dolphin, and puts the message on a Braille display
as well. Where it is missing, NVDA's own controller client and the JAWS COM
API are used directly, and macOS falls back to the built-in ``say``. Every
path fails closed: speech is an extra, and a screen reader that is not
listening must never break the action that produced the message.

Messages are appended rather than spoken over the top of whatever is being
read. A status update that cut the results list off mid-word would cost more
than it told, and blindDL updates the status bar while a search is running.
"""

import ctypes
import os
import shutil
import subprocess
import sys
import threading
import time

_lock = threading.Lock()
_output = None
_output_attempted = False
_nvda_dll = None
_nvda_attempted = False
_say_process = None
_say_path = None
_say_attempted = False
# The status bar repeats itself -- a tick that finds nothing new to report
# rewrites the same sentence. Saying it again would be noise.
_last_spoken = None
# Which reader answered last, and when it was asked. Finding out costs a
# walk through every installed output, so it is not asked again for every
# sentence of a search that is reporting fifty sites.
_resolved = None
_resolved_at = 0.0
# Short enough that a reader started after blindDL is picked up almost at
# once; long enough that a burst of progress messages costs one look.
RESOLVE_TTL = 2.0
# The same idea for JAWS, whose "are you running" test takes a snapshot of
# every process on the machine. JAWS does not start between two sentences.
_jaws_running = False
_jaws_checked_at = 0.0
JAWS_PROBE_TTL = 1.0


def speak(message, interrupt=False):
    """Say *message* through the screen reader. Returns True if anything did.

    *interrupt* replaces what is currently being read instead of queueing
    behind it. blindDL leaves it off: these are notifications, not answers
    to a keypress, and cutting the user off to deliver one is worse than
    waiting.
    """
    text = str(message or "").strip()
    if not text:
        return False
    if os.environ.get("BLINDDL_NO_SPEECH", "").strip():
        # Set by test runs and headless builds, which have nobody to talk to.
        return False
    spoken = False
    output = _accessible_output()
    if output is not None:
        # Auto.speak() and Auto.braille() each ask every installed output in
        # turn whether it is running, so one message paid for two full
        # sweeps -- and a sweep that has to reach the end of the list costs
        # over twenty milliseconds on the thread trying to talk. Resolve the
        # reader once and address it directly.
        target = _resolved_output(output)
        spoken = _ao2_speak(target, text, interrupt) or spoken
        spoken = _ao2_braille(target, text) or spoken
        if spoken:
            return True
    if sys.platform == "darwin":
        return _speak_macos(text, interrupt)
    if not sys.platform.startswith("win"):
        return False
    if _speak_nvda(text, interrupt):
        return True
    return _speak_jaws(text, interrupt)


def announce(message, interrupt=False):
    """Speak *message* unless it is the one that was just spoken."""
    global _last_spoken
    text = str(message or "").strip()
    if not text:
        return False
    with _lock:
        if text == _last_spoken:
            return False
        _last_spoken = text
    return speak(text, interrupt=interrupt)


def reset():
    """Forget the last message, so it would be spoken again. For tests."""
    global _last_spoken, _output, _output_attempted
    global _nvda_dll, _nvda_attempted, _say_path, _say_attempted
    global _resolved, _resolved_at, _jaws_running, _jaws_checked_at
    with _lock:
        _last_spoken = None
        _output = None
        _output_attempted = False
        _nvda_dll = None
        _nvda_attempted = False
        _say_path = None
        _say_attempted = False
        _resolved = None
        _resolved_at = 0.0
        _jaws_running = False
        _jaws_checked_at = 0.0


# -- accessible-output2 ----------------------------------------------------


def _accessible_output():
    """The accessible-output2 Auto output, looked up once."""
    global _output, _output_attempted
    with _lock:
        if _output_attempted:
            return _output
        _output_attempted = True
        try:
            import accessible_output2.outputs.auto

            _output = accessible_output2.outputs.auto.Auto()
        except Exception:  # noqa: BLE001 - library missing or no reader running
            _output = None
        return _output


def _resolved_output(output):
    """The concrete reader behind the Auto output, looked for now and then.

    Falls back to *output* itself, which finds the reader on every call, so
    nothing is ever left unspoken because this had nothing to hand. A reader
    started after blindDL is picked up within RESOLVE_TTL seconds.
    """
    global _resolved, _resolved_at
    finder = getattr(output, "get_first_available_output", None)
    if finder is None:
        return output
    now = time.monotonic()
    with _lock:
        if _resolved is not None and now - _resolved_at < RESOLVE_TTL:
            return _resolved
    try:
        found = finder()
    except Exception:  # noqa: BLE001 - fall back to the probing Auto output
        return output
    if found is None:
        return output
    with _lock:
        _resolved = found
        _resolved_at = now
    return found


def _ao2_speak(output, text, interrupt):
    try:
        output.speak(text, interrupt=interrupt)
        return True
    except TypeError:
        # Not every output accepts the keyword.
        try:
            output.speak(text)
            return True
        except Exception:  # noqa: BLE001 - fall through to the direct paths
            return False
    except Exception:  # noqa: BLE001 - fall through to the direct paths
        return False


def _ao2_braille(output, text):
    try:
        output.braille(text)
        return True
    except Exception:  # noqa: BLE001 - most outputs have no Braille display
        return False


# -- NVDA ------------------------------------------------------------------


def _speak_nvda(text, interrupt=False):
    dll = _nvda_controller()
    if dll is None:
        return False
    try:
        if int(dll.nvdaController_testIfRunning()) != 0:
            return False
        if interrupt:
            try:
                dll.nvdaController_cancelSpeech()
            except Exception:  # noqa: BLE001 - speaking still works without it
                pass
        spoke = int(dll.nvdaController_speakText(str(text))) == 0
        braille = getattr(dll, "nvdaController_brailleMessage", None)
        if braille is not None:
            try:
                braille(str(text))
            except Exception:  # noqa: BLE001 - Braille is optional
                pass
        return spoke
    except Exception:  # noqa: BLE001 - NVDA went away mid-call
        return False


def _nvda_controller():
    """Load NVDA's controller client from wherever this build keeps it."""
    global _nvda_dll, _nvda_attempted
    with _lock:
        if _nvda_attempted:
            return _nvda_dll
        _nvda_attempted = True
        if not sys.platform.startswith("win"):
            return None
        for path in _nvda_controller_candidates():
            try:
                dll = ctypes.WinDLL(path)
                dll.nvdaController_testIfRunning.argtypes = []
                dll.nvdaController_testIfRunning.restype = ctypes.c_ulong
                dll.nvdaController_speakText.argtypes = [ctypes.c_wchar_p]
                dll.nvdaController_speakText.restype = ctypes.c_ulong
                dll.nvdaController_cancelSpeech.argtypes = []
                dll.nvdaController_cancelSpeech.restype = ctypes.c_ulong
                try:
                    dll.nvdaController_brailleMessage.argtypes = [ctypes.c_wchar_p]
                    dll.nvdaController_brailleMessage.restype = ctypes.c_ulong
                except Exception:  # noqa: BLE001 - older clients lack Braille
                    pass
                _nvda_dll = dll
                return _nvda_dll
            except Exception:  # noqa: BLE001 - try the next candidate
                continue
        return None


def _nvda_controller_candidates():
    """Every place a controller client DLL could be, newest layout first."""
    names = ("nvdaControllerClient64.dll", "nvdaControllerClient.dll",
             "nvdaControllerClient32.dll")
    roots = []
    override = os.environ.get("BLINDDL_NVDA_CONTROLLER_DLL", "").strip()
    if override:
        roots.append(override)
    frozen_dir = getattr(sys, "_MEIPASS", "")
    if frozen_dir:
        roots.append(frozen_dir)
    roots.append(os.path.dirname(os.path.abspath(sys.executable)))
    roots.append(os.path.dirname(os.path.abspath(__file__)))
    try:
        import accessible_output2

        roots.append(os.path.join(
            os.path.dirname(os.path.abspath(accessible_output2.__file__)), "lib"
        ))
    except Exception:  # noqa: BLE001 - the library is optional
        pass

    candidates = []
    for root in roots:
        if root.lower().endswith(".dll"):
            candidates.append(root)
            continue
        for folder in (root, os.path.join(root, "lib"),
                       os.path.join(root, "_internal"),
                       os.path.join(root, "_internal", "lib")):
            for name in names:
                candidates.append(os.path.join(folder, name))
    seen = set()
    found = []
    for candidate in candidates:
        key = os.path.normcase(os.path.abspath(candidate))
        if key in seen:
            continue
        seen.add(key)
        if os.path.isfile(candidate):
            found.append(candidate)
    return found


# -- JAWS ------------------------------------------------------------------


def _speak_jaws(text, interrupt=False):
    """Speak through JAWS, but only when JAWS is actually running.

    The COM object exists on disk whether or not JAWS has been started, and
    creating it would launch nothing useful and report success.
    """
    if not _jaws_is_running():
        return False
    for module_name in ("win32com.client", "comtypes.client"):
        try:
            module = __import__(module_name, fromlist=["client"])
        except Exception:  # noqa: BLE001 - neither binding is required
            continue
        for program_id in ("FreedomSci.JawsApi", "freedomsci.jawsapi"):
            try:
                factory = getattr(module, "Dispatch", None) or module.CreateObject
                jaws = factory(program_id)
                return bool(jaws.SayString(str(text), bool(interrupt)))
            except Exception:  # noqa: BLE001 - try the other spelling/binding
                continue
    return False


def _jaws_is_running():
    """Whether JAWS is up, asked at most once every JAWS_PROBE_TTL seconds.

    The test underneath snapshots every process on the machine, which costs
    milliseconds on the thread that is trying to speak -- and it costs them
    whether or not JAWS is even installed.
    """
    global _jaws_running, _jaws_checked_at
    now = time.monotonic()
    with _lock:
        if _jaws_checked_at and now - _jaws_checked_at < JAWS_PROBE_TTL:
            return _jaws_running
    running = _process_running({"jfw.exe", "jaws.exe", "fusion.exe"})
    with _lock:
        _jaws_running = running
        _jaws_checked_at = now
    return running


def _process_running(names):
    """Whether any of *names* is a running process on this Windows machine."""
    if not sys.platform.startswith("win"):
        return False
    try:
        from ctypes import wintypes
    except (ImportError, ValueError):
        return False
    wanted = {name.lower() for name in names}
    try:
        kernel32 = ctypes.windll.kernel32
        # Without an explicit restype, ctypes truncates the returned 64-bit
        # HANDLE to 32 bits, which fails enumeration on 64-bit Windows.
        kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
        kernel32.CreateToolhelp32Snapshot.argtypes = [
            wintypes.DWORD, wintypes.DWORD]
        snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
        if snapshot in (0, -1, ctypes.c_void_p(-1).value):
            return False

        class ProcessEntry(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_void_p),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", wintypes.WCHAR * 260),
            ]

        try:
            entry = ProcessEntry()
            entry.dwSize = ctypes.sizeof(ProcessEntry)
            if not kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
                return False
            while True:
                if str(entry.szExeFile).lower() in wanted:
                    return True
                if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                    return False
        finally:
            kernel32.CloseHandle(snapshot)
    except Exception:  # noqa: BLE001 - the enumeration is a best effort
        return False


# -- macOS -----------------------------------------------------------------


def _speak_macos(text, interrupt=False):
    """VoiceOver has no announcement API without pyobjc; ``say`` is the path."""
    global _say_process, _say_path, _say_attempted
    with _lock:
        if not _say_attempted:
            _say_attempted = True
            _say_path = shutil.which("say") or (
                "/usr/bin/say" if os.path.isfile("/usr/bin/say") else None
            )
        say = _say_path
        if not say:
            return False
        try:
            previous = _say_process
            if previous is not None and previous.poll() is None:
                if interrupt:
                    previous.terminate()
            # poll() above also reaps a finished `say`, so announcements never
            # accumulate zombie processes.
            _say_process = subprocess.Popen(  # noqa: S603 - fixed system binary
                [say, "--", text],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
        except Exception:  # noqa: BLE001 - speech is an extra
            return False
