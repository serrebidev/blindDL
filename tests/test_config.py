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

    def test_old_default_search_timeout_migrates_to_thirty_seconds(self):
        config = self._load({"config_version": 1, "search_timeout_s": 5})

        self.assertEqual(config["search_timeout_s"], 30)
        self.assertEqual(config["config_version"], config_module.CONFIG_VERSION)

    def test_custom_search_timeout_survives_migration(self):
        config = self._load({"config_version": 1, "search_timeout_s": 12})

        self.assertEqual(config["search_timeout_s"], 12)

    def test_daily_update_check_migrates_to_twice_a_day(self):
        config = self._load({"config_version": 2, "update_check_hours": 24})

        self.assertEqual(config["update_check_hours"], 12)
        self.assertEqual(config["config_version"], config_module.CONFIG_VERSION)

    def test_a_chosen_update_interval_survives_migration(self):
        config = self._load({"config_version": 2, "update_check_hours": 48})

        self.assertEqual(config["update_check_hours"], 48)

    def test_automatic_updates_are_enabled_by_default(self):
        # auto_install_update is a legacy compatibility key; auto_update now
        # controls the complete scheduled download/install/restart workflow.
        config = self._load({})

        self.assertTrue(config["auto_update"])
        self.assertFalse(config["auto_install_update"])

    def test_environment_can_isolate_application_state(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        isolated = Path(temporary.name) / "managed-state"
        with mock.patch.dict(
            config_module.os.environ,
            {"BLINDDL_APP_DATA_DIR": str(isolated)},
        ):
            self.assertEqual(
                Path(config_module.app_data_dir()).resolve(), isolated.resolve()
            )
        self.assertTrue(isolated.is_dir())

    def test_deezer_format_defaults_to_flac(self):
        config = self._load({})

        self.assertEqual(config["deezer_format"], "flac")


if __name__ == "__main__":
    unittest.main()
