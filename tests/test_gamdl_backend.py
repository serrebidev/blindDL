# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from blinddl import gamdl_backend


class GamdlBackendTests(unittest.TestCase):
    def test_find_gamdl_accepts_current_version(self):
        with (
            mock.patch.object(gamdl_backend, "_candidate_paths", return_value=["C:/gamdl.exe"]),
            mock.patch.object(os.path, "isfile", return_value=True),
            mock.patch.object(gamdl_backend, "_version", return_value=(3, 9, 1)),
        ):
            self.assertEqual(gamdl_backend.find_gamdl(), os.path.abspath("C:/gamdl.exe"))

    def test_find_gamdl_rejects_old_version(self):
        with (
            mock.patch.object(gamdl_backend, "_candidate_paths", return_value=["C:/gamdl.exe"]),
            mock.patch.object(os.path, "isfile", return_value=True),
            mock.patch.object(gamdl_backend, "_version", return_value=(3, 8, 5)),
        ):
            self.assertIsNone(gamdl_backend.find_gamdl())

    def test_download_passes_existing_apple_cookies_and_output_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            cookies = Path(tmp) / "cookies.txt"
            cookies.write_text("# Netscape", encoding="utf-8")
            process = mock.Mock()
            process.poll.side_effect = [None, 0]
            process.returncode = 0

            with (
                mock.patch.object(gamdl_backend, "find_gamdl", return_value="gamdl"),
                mock.patch.object(subprocess, "Popen", return_value=process) as popen,
                mock.patch.object(gamdl_backend.time, "sleep"),
            ):
                result = gamdl_backend.download(
                    "https://music.apple.com/ca/song/example/123",
                    tmp,
                    {"apple_music_cookies": str(cookies)},
                )

            command = popen.call_args.args[0]
            self.assertEqual(command[0], "gamdl")
            self.assertIn("--cookies-path", command)
            self.assertEqual(command[command.index("--cookies-path") + 1], str(cookies))
            self.assertEqual(command[command.index("--output-path") + 1], os.path.abspath(tmp))
            self.assertEqual(result, os.path.abspath(tmp))

    def test_download_cancels_running_gamdl(self):
        with tempfile.TemporaryDirectory() as tmp:
            cookies = Path(tmp) / "cookies.txt"
            cookies.write_text("# Netscape", encoding="utf-8")
            cancelled = threading.Event()
            cancelled.set()
            process = mock.Mock()
            process.poll.return_value = None

            with (
                mock.patch.object(gamdl_backend, "find_gamdl", return_value="gamdl"),
                mock.patch.object(subprocess, "Popen", return_value=process),
            ):
                with self.assertRaisesRegex(RuntimeError, "cancelled"):
                    gamdl_backend.download(
                        "https://music.apple.com/ca/song/example/123",
                        tmp,
                        {"apple_music_cookies": str(cookies)},
                        cancelled,
                    )

            process.terminate.assert_called_once()


if __name__ == "__main__":
    unittest.main()