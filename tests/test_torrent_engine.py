# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""The parts of the torrent engine that run without libtorrent installed.

libtorrent is optional, so everything here either avoids it or stands a stub
in for it. What is worth pinning down is the shape of what blindDL tells a
swarm about itself, and that the settings a user types survive the trip into
libtorrent's own units.
"""

import unittest
import threading
from unittest import mock

from blinddl import torrent_engine
from blinddl.config import CONFIG_VERSION, DEFAULTS


class _Config(dict):
    """A Config stand-in: the real one is a dict with a save()."""

    def __init__(self, **overrides):
        super().__init__(DEFAULTS)
        self.update(overrides)
        self.saves = 0

    def save(self):
        self.saves += 1


class _StubLibtorrent:
    """Just enough libtorrent for the identity and settings helpers."""

    __version__ = "2.0.13.0"

    @staticmethod
    def generate_fingerprint(name, major, minor, revision, tag):
        return f"-{name}{major}{minor}{revision}{tag}-"


class ClientIdentityTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(torrent_engine, "_lt", _StubLibtorrent())
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_pinned_version_wins_and_never_asks_the_network(self):
        config = _Config(torrent_client_version="4.6.7")
        with mock.patch.object(torrent_engine, "_fetch_latest_qbittorrent",
                               side_effect=AssertionError("looked up")):
            fingerprint, agent = torrent_engine.client_identity(config)
        self.assertEqual(fingerprint, "-qB4670-")
        self.assertEqual(agent, "qBittorrent/4.6.7")

    def test_looked_up_version_is_cached_in_the_config(self):
        config = _Config()
        with mock.patch.object(torrent_engine, "_fetch_latest_qbittorrent",
                               return_value=(5, 2, 3)) as lookup:
            first = torrent_engine.client_version(config)
            second = torrent_engine.client_version(config)
        self.assertEqual(first, (5, 2, 3))
        self.assertEqual(second, (5, 2, 3))
        # The second call is answered from the config, a day's worth of
        # sessions off one lookup.
        self.assertEqual(lookup.call_count, 1)
        self.assertEqual(config["torrent_client_version_cache"], "5.2.3")

    def test_offline_start_falls_back_without_calling_out(self):
        config = _Config()
        with mock.patch.object(torrent_engine, "_fetch_latest_qbittorrent",
                               side_effect=AssertionError("looked up")):
            version = torrent_engine.client_version(
                config, allow_network=False)
        self.assertEqual(
            version, torrent_engine._parse_version(
                torrent_engine.QBITTORRENT_FALLBACK_VERSION))

    def test_a_swarm_is_never_told_this_is_libtorrent(self):
        config = _Config(torrent_client_version="5.2.3")
        settings = torrent_engine.session_settings(config)
        self.assertNotIn("libtorrent", settings["user_agent"].lower())
        self.assertTrue(settings["peer_fingerprint"].startswith("-qB"))


class SessionSettingsTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(torrent_engine, "_lt", _StubLibtorrent())
        patcher.start()
        self.addCleanup(patcher.stop)

    def _settings(self, **overrides):
        config = _Config(torrent_client_version="5.2.3", **overrides)
        return torrent_engine.session_settings(config)

    def test_speed_limits_are_converted_from_kib_to_bytes(self):
        settings = self._settings(torrent_max_down_kib=1500,
                                  torrent_max_up_kib=250)
        self.assertEqual(settings["download_rate_limit"], 1500 * 1024)
        self.assertEqual(settings["upload_rate_limit"], 250 * 1024)

    def test_zero_stays_unlimited(self):
        settings = self._settings()
        self.assertEqual(settings["download_rate_limit"], 0)
        self.assertEqual(settings["upload_rate_limit"], 0)

    def test_encryption_choices_map_onto_libtorrent_policies(self):
        for choice, expected in (("prefer", 1), ("require", 0), ("off", 2)):
            with self.subTest(choice=choice):
                settings = self._settings(torrent_encryption=choice)
                self.assertEqual(settings["out_enc_policy"], expected)
                self.assertEqual(settings["in_enc_policy"], expected)

    def test_turning_the_public_swarm_off_disables_dht_and_lsd(self):
        settings = self._settings(torrent_dht=False)
        self.assertFalse(settings["enable_dht"])
        self.assertFalse(settings["enable_lsd"])

    def test_random_port_is_asked_for_as_port_zero(self):
        self.assertEqual(self._settings()["listen_interfaces"],
                         "0.0.0.0:0,[::]:0")
        self.assertEqual(self._settings(torrent_port=51413)["listen_interfaces"],
                         "0.0.0.0:51413,[::]:51413")

    def test_libtorrents_own_seed_limits_stay_out_of_the_way(self):
        # blindDL applies the user's ratio and time limits itself, including
        # "0 means keep seeding", which libtorrent has no value for.
        settings = self._settings(torrent_seed_ratio=0,
                                  torrent_seed_minutes=0)
        self.assertGreater(settings["share_ratio_limit"], 1000)
        self.assertGreater(settings["seed_time_limit"], 86400)


class ProxyTests(unittest.TestCase):
    def test_blank_means_direct(self):
        self.assertEqual(torrent_engine.parse_proxy(""), {})
        self.assertEqual(torrent_engine.parse_proxy("   "), {})

    def test_host_and_port_default_to_socks5(self):
        settings = torrent_engine.parse_proxy("10.0.0.2:1080")
        self.assertEqual(settings["proxy_type"], torrent_engine._PROXY_SOCKS5)
        self.assertEqual(settings["proxy_hostname"], "10.0.0.2")
        self.assertEqual(settings["proxy_port"], 1080)

    def test_credentials_select_the_authenticated_proxy_type(self):
        settings = torrent_engine.parse_proxy(
            "socks5://me:secret@vpn.example:1080")
        self.assertEqual(settings["proxy_type"],
                         torrent_engine._PROXY_SOCKS5_PW)
        self.assertEqual(settings["proxy_username"], "me")
        self.assertEqual(settings["proxy_password"], "secret")

    def test_peer_traffic_and_dns_go_through_the_proxy_too(self):
        # A proxy that only covered tracker announces would still hand the
        # real address to every peer in the swarm.
        settings = torrent_engine.parse_proxy("socks5://vpn.example:1080")
        self.assertTrue(settings["proxy_peer_connections"])
        self.assertTrue(settings["proxy_hostnames"])

    def test_nonsense_is_refused_rather_than_silently_ignored(self):
        with self.assertRaises(torrent_engine.TorrentEngineError):
            torrent_engine.parse_proxy("socks5://:@:")


class _Status:
    """A torrent_status stand-in for the progress formatter."""

    class _State:
        def __init__(self, name):
            self.name = name

    def __init__(self, state="downloading", progress=0.5, rate=1024,
                 wanted=1000, done=500, seeds=3, peers=7):
        self.state = self._State(state)
        self.progress = progress
        self.download_rate = rate
        self.total_wanted = wanted
        self.total_wanted_done = done
        self.num_seeds = seeds
        self.num_peers = peers


class ProgressTests(unittest.TestCase):
    def test_eta_comes_from_what_is_left_and_the_current_rate(self):
        info = torrent_engine._progress(
            _Status(rate=100, wanted=1000, done=400))
        self.assertEqual(info["percent"], 50.0)
        self.assertEqual(info["eta"], 6.0)

    def test_a_stalled_torrent_reports_no_eta_rather_than_infinity(self):
        info = torrent_engine._progress(_Status(rate=0))
        self.assertEqual(info["eta"], 0)

    def test_peers_excludes_the_seeds_already_counted(self):
        info = torrent_engine._progress(_Status(seeds=3, peers=7))
        self.assertEqual(info["seeds"], 3)
        self.assertEqual(info["peers"], 4)

    def test_the_metadata_wait_says_so_instead_of_showing_zero_bytes(self):
        info = torrent_engine._progress(
            _Status(state="downloading_metadata", rate=0))
        self.assertEqual(info["state"], "Fetching torrent details")


class RatioTests(unittest.TestCase):
    def test_ratio_is_measured_against_the_torrent_not_this_session(self):
        # A resumed torrent has downloaded nothing this session; dividing by
        # that would report an enormous ratio and stop seeding at once.
        status = mock.Mock(all_time_upload=500, all_time_download=0,
                           total_wanted=1000)
        self.assertEqual(torrent_engine._ratio(status), 0.5)

    def test_a_fresh_torrent_uses_what_it_actually_downloaded(self):
        status = mock.Mock(all_time_upload=2000, all_time_download=1000,
                           total_wanted=1000)
        self.assertEqual(torrent_engine._ratio(status), 2.0)


class UploadSnapshotTests(unittest.TestCase):
    def test_uploads_uses_the_maintenance_cache_without_calling_libtorrent(self):
        engine = object.__new__(torrent_engine.TorrentEngine)
        engine._lock = threading.RLock()
        engine._uploads_cache = [{"key": "abc", "title": "Release"}]

        rows = engine.uploads()

        self.assertEqual(rows, [{"key": "abc", "title": "Release"}])
        self.assertIsNot(rows[0], engine._uploads_cache[0])


class AvailabilityTests(unittest.TestCase):
    def test_a_missing_libtorrent_is_reported_not_raised(self):
        with mock.patch.object(torrent_engine, "_lt", None), \
                mock.patch.dict("sys.modules", {"libtorrent": None}):
            with mock.patch("builtins.__import__",
                            side_effect=ImportError("no libtorrent")):
                self.assertFalse(torrent_engine.available())

    def test_the_hint_names_the_running_python(self):
        import sys
        hint = torrent_engine.install_hint()
        self.assertIn(
            f"{sys.version_info.major}.{sys.version_info.minor}", hint)


class ConfigMigrationTests(unittest.TestCase):
    def test_tray_defaults_reach_a_config_written_before_they_existed(self):
        from blinddl.config import Config
        with mock.patch.object(Config, "load", lambda self: None):
            config = Config()
        config.data["minimize_to_tray"] = False
        config.data["tray_on_minimize"] = False
        with mock.patch.object(Config, "save", lambda self: None):
            config._migrate(0)
        self.assertTrue(config["minimize_to_tray"])
        self.assertTrue(config["tray_on_minimize"])
        self.assertEqual(config["config_version"], CONFIG_VERSION)

    def test_a_current_config_is_left_exactly_as_the_user_set_it(self):
        from blinddl.config import Config
        with mock.patch.object(Config, "load", lambda self: None):
            config = Config()
        config.data["minimize_to_tray"] = False
        with mock.patch.object(Config, "save", lambda self: None):
            config._migrate(CONFIG_VERSION)
        self.assertFalse(config["minimize_to_tray"])


if __name__ == "__main__":
    unittest.main()
