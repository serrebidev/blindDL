# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from blinddl import config as config_module


class ConfigLoadTests(unittest.TestCase):
    def _load(self, value):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "config.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        with mock.patch.object(
            config_module, "app_data_dir", return_value=temporary.name
        ):
            return config_module.Config()

    def test_wrong_shaped_json_falls_back_to_defaults(self):
        config = self._load(["not", "a", "mapping"])

        self.assertEqual(config["max_concurrent"], 4)
        self.assertEqual(config["disabled_music_sources"], [])

    def test_wrong_value_types_do_not_poison_controls(self):
        config = self._load(
            {
                "config_version": "invalid",
                "max_concurrent": "many",
                "adult_sites_enabled": "yes",
                "disabled_music_sources": {},
            }
        )

        self.assertEqual(config["max_concurrent"], 4)
        self.assertFalse(config["adult_sites_enabled"])
        self.assertEqual(config["disabled_music_sources"], [])


if __name__ == "__main__":
    unittest.main()
