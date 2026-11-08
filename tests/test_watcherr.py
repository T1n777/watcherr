#!/usr/bin/env python3
"""
Unit and Integration Tests for Watcherr.
Zero dependencies: Uses standard library unittest and unittest.mock.
"""

import sys
import os
import unittest
from io import BytesIO
from urllib.error import HTTPError
from unittest.mock import MagicMock, patch

# Ensure watcherr module is on python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from watcherr import WatcherrConfig, WatcherrClient, parse_args, __version__


class TestWatcherrConfig(unittest.TestCase):
    def test_default_config(self):
        with patch.dict(os.environ, {}, clear=True):
            cfg = WatcherrConfig()
            self.assertEqual(cfg.qbit_url, "http://127.0.0.1:8080")
            self.assertEqual(cfg.sonarr_url, "http://127.0.0.1:8989")
            self.assertEqual(cfg.radarr_url, "http://127.0.0.1:7878")
            self.assertEqual(cfg.meta_stall_seconds, 180)
            self.assertEqual(cfg.zero_seed_stall_seconds, 300)
            self.assertEqual(cfg.check_interval_seconds, 45)
            self.assertFalse(cfg.dry_run)
            self.assertFalse(cfg.once)

    def test_env_var_overrides(self):
        env = {
            "QBIT_URL": "http://192.168.1.50:8080/",
            "SONARR_URL": "http://192.168.1.50:8989/",
            "RADARR_URL": "http://192.168.1.50:7878/",
            "SONARR_API_KEY": "sonarr123",
            "RADARR_API_KEY": "radarr123",
            "METADATA_STALL_SECONDS": "120",
            "ZERO_SEED_STALL_SECONDS": "240",
            "CHECK_INTERVAL_SECONDS": "30",
            "DRY_RUN": "true",
        }
        with patch.dict(os.environ, env, clear=True):
            cfg = WatcherrConfig()
            self.assertEqual(cfg.qbit_url, "http://192.168.1.50:8080")
            self.assertEqual(cfg.sonarr_url, "http://192.168.1.50:8989")
            self.assertEqual(cfg.radarr_url, "http://192.168.1.50:7878")
            self.assertEqual(cfg.sonarr_key, "sonarr123")
            self.assertEqual(cfg.radarr_key, "radarr123")
            self.assertEqual(cfg.meta_stall_seconds, 120)
            self.assertEqual(cfg.zero_seed_stall_seconds, 240)
            self.assertEqual(cfg.check_interval_seconds, 30)
            self.assertTrue(cfg.dry_run)

    def test_xml_discovery(self):
        xml_content = "<Config><ApiKey>auto_discovered_key_456</ApiKey></Config>"
        with patch("os.path.isfile", return_value=True):
            with patch("xml.etree.ElementTree.parse") as mock_parse:
                import xml.etree.ElementTree as ET
                mock_parse.return_value = ET.ElementTree(ET.fromstring(xml_content))
                key = WatcherrConfig._discover_api_key("/fake/path/config.xml")
                self.assertEqual(key, "auto_discovered_key_456")

    def test_xml_discovery_missing_file(self):
        key = WatcherrConfig._discover_api_key("/nonexistent/file/config.xml")
        self.assertIsNone(key)


class TestStallDetection(unittest.TestCase):
    def setUp(self):
        self.cfg = WatcherrConfig()
        self.cfg.sonarr_key = "test_sonarr_key"
        self.cfg.radarr_key = "test_radarr_key"
        self.client = WatcherrClient(self.cfg)

    @patch.object(WatcherrClient, "heal_sonarr_torrent")
    @patch.object(WatcherrClient, "heal_radarr_torrent")
    def test_metadata_stall_detected(self, mock_radarr, mock_sonarr):
        torrents = [{
            "hash": "abc12345",
            "name": "Stuck.Meta.Series.S01E01",
            "state": "metaDL",
            "num_seeds": 0,
            "progress": 0.0,
            "dlspeed": 0,
            "time_active": 190,  # > 180s threshold
            "category": "tv-sonarr",
        }]
        self.client.get_qbit_torrents = MagicMock(return_value=torrents)
        self.client.inspect_and_heal()

        mock_sonarr.assert_called_once_with(torrents[0])
        mock_radarr.assert_not_called()

    @patch.object(WatcherrClient, "heal_sonarr_torrent")
    @patch.object(WatcherrClient, "heal_radarr_torrent")
    def test_metadata_not_stalled_yet(self, mock_radarr, mock_sonarr):
        torrents = [{
            "hash": "abc12345",
            "name": "Starting.Meta.Series.S01E01",
            "state": "metaDL",
            "num_seeds": 0,
            "progress": 0.0,
            "dlspeed": 0,
            "time_active": 60,  # < 180s threshold
            "category": "tv-sonarr",
        }]
        self.client.get_qbit_torrents = MagicMock(return_value=torrents)
        self.client.inspect_and_heal()

        mock_sonarr.assert_not_called()
        mock_radarr.assert_not_called()

    @patch.object(WatcherrClient, "heal_sonarr_torrent")
    @patch.object(WatcherrClient, "heal_radarr_torrent")
    def test_zero_seed_stall_detected_radarr(self, mock_radarr, mock_sonarr):
        torrents = [{
            "hash": "def67890",
            "name": "Dead.Movie.2024.1080p",
            "state": "stalledDL",
            "num_seeds": 0,
            "progress": 0.01,  # < 5%
            "dlspeed": 0,
            "time_active": 350,  # > 300s threshold
            "category": "radarr",
        }]
        self.client.get_qbit_torrents = MagicMock(return_value=torrents)
        self.client.inspect_and_heal()

        mock_radarr.assert_called_once_with(torrents[0])
        mock_sonarr.assert_not_called()

    @patch.object(WatcherrClient, "heal_sonarr_torrent")
    @patch.object(WatcherrClient, "heal_radarr_torrent")
    def test_uncategorized_falls_back_to_both(self, mock_radarr, mock_sonarr):
        # When category is empty, tries Sonarr first; if false, tries Radarr
        mock_sonarr.return_value = False
        mock_radarr.return_value = True

        torrents = [{
            "hash": "unknown123",
            "name": "Uncategorized.Dead.Release",
            "state": "stalledDL",
            "num_seeds": 0,
            "progress": 0.0,
            "dlspeed": 0,
            "time_active": 400,
            "category": "",
        }]
        self.client.get_qbit_torrents = MagicMock(return_value=torrents)
        self.client.inspect_and_heal()

        mock_sonarr.assert_called_once_with(torrents[0])
        mock_radarr.assert_called_once_with(torrents[0])

    @patch.object(WatcherrClient, "heal_sonarr_torrent")
    @patch.object(WatcherrClient, "heal_radarr_torrent")
    def test_healthy_torrent_ignored(self, mock_radarr, mock_sonarr):
        torrents = [
            # Seeding completed
            {
                "hash": "111",
                "name": "Finished.Show",
                "state": "uploading",
                "num_seeds": 0,
                "progress": 1.0,
                "dlspeed": 0,
                "time_active": 9000,
                "category": "sonarr",
            },
            # Active download with seeders
            {
                "hash": "222",
                "name": "Downloading.Show",
                "state": "downloading",
                "num_seeds": 12,
                "progress": 0.35,
                "dlspeed": 4500000,
                "time_active": 400,
                "category": "sonarr",
            },
            # Stalled but already at 50% progress (e.g. temporary pause)
            {
                "hash": "333",
                "name": "Partially.Downloaded.Show",
                "state": "stalledDL",
                "num_seeds": 0,
                "progress": 0.50,
                "dlspeed": 0,
                "time_active": 600,
                "category": "sonarr",
            },
        ]
        self.client.get_qbit_torrents = MagicMock(return_value=torrents)
        self.client.inspect_and_heal()

        mock_sonarr.assert_not_called()
        mock_radarr.assert_not_called()


class TestHealExecution(unittest.TestCase):
    def setUp(self):
        self.cfg = WatcherrConfig()
        self.cfg.sonarr_key = "mock_sonarr_key"
        self.cfg.radarr_key = "mock_radarr_key"
        self.client = WatcherrClient(self.cfg)

    def test_heal_sonarr_by_hash_triggers_episode_search(self):
        torrent = {
            "hash": "deadbeef123",
            "name": "Dead.Show.S01E05",
            "num_seeds": 0,
            "state": "stalledDL",
        }
        mock_queue = {
            "records": [
                {
                    "id": 42,
                    "downloadId": "deadbeef123",
                    "title": "Dead.Show.S01E05",
                    "episodeId": 999,
                    "seriesId": 12,
                }
            ]
        }

        call_log = []

        def mock_request(url, method="GET", headers=None, data=None, timeout=10):
            call_log.append({"url": url, "method": method, "data": data})
            if "/api/v3/queue" in url:
                return mock_queue
            return {}

        self.client.request = MagicMock(side_effect=mock_request)
        result = self.client.heal_sonarr_torrent(torrent)

        self.assertTrue(result)
        self.assertEqual(len(call_log), 3)
        self.assertEqual(call_log[0]["method"], "GET")

        # Check DELETE call
        self.assertEqual(call_log[1]["method"], "DELETE")
        self.assertIn("/api/v3/queue/42?removeFromClient=true&blocklist=true", call_log[1]["url"])

        # Check POST command call
        self.assertEqual(call_log[2]["method"], "POST")
        self.assertIn("/api/v3/command", call_log[2]["url"])
        self.assertEqual(call_log[2]["data"], {"name": "EpisodeSearch", "episodeIds": [999]})

    def test_heal_sonarr_season_pack_triggers_series_search(self):
        torrent = {
            "hash": "seasonpack777",
            "name": "Full.Season.Pack",
            "num_seeds": 0,
            "state": "stalledDL",
        }
        mock_queue = {
            "records": [
                {
                    "id": 55,
                    "downloadId": "seasonpack777",
                    "title": "Full.Season.Pack",
                    "episodeId": None,
                    "seriesId": 88,
                }
            ]
        }

        call_log = []

        def mock_request(url, method="GET", headers=None, data=None, timeout=10):
            call_log.append({"url": url, "method": method, "data": data})
            if "/api/v3/queue" in url:
                return mock_queue
            return {}

        self.client.request = MagicMock(side_effect=mock_request)
        result = self.client.heal_sonarr_torrent(torrent)

        self.assertTrue(result)
        self.assertEqual(call_log[2]["method"], "POST")
        self.assertEqual(call_log[2]["data"], {"name": "SeriesSearch", "seriesId": 88})

    def test_heal_sonarr_matches_by_title_fallback(self):
        torrent = {
            "hash": "mismatched_hash_999",
            "name": "Dead.Episode.Title",
            "num_seeds": 0,
            "state": "stalledDL",
        }
        mock_queue = {
            "records": [
                {
                    "id": 73,
                    "downloadId": "different_hash_000",
                    "title": "Dead.Episode.Title",
                    "episodeId": 1234,
                }
            ]
        }

        self.client.request = MagicMock(return_value=mock_queue)
        result = self.client.heal_sonarr_torrent(torrent)
        self.assertTrue(result)

    def test_heal_radarr_by_hash_triggers_movie_search(self):
        torrent = {
            "hash": "cafebabe456",
            "name": "Dead.Movie.2024",
            "num_seeds": 0,
            "state": "stalledDL",
        }
        mock_queue = {
            "records": [
                {
                    "id": 88,
                    "downloadId": "cafebabe456",
                    "title": "Dead.Movie.2024",
                    "movieId": 777,
                }
            ]
        }

        call_log = []

        def mock_request(url, method="GET", headers=None, data=None, timeout=10):
            call_log.append({"url": url, "method": method, "data": data})
            if "/api/v3/queue" in url:
                return mock_queue
            return {}

        self.client.request = MagicMock(side_effect=mock_request)
        result = self.client.heal_radarr_torrent(torrent)

        self.assertTrue(result)
        self.assertEqual(len(call_log), 3)

        # Check DELETE call
        self.assertEqual(call_log[1]["method"], "DELETE")
        self.assertIn("/api/v3/queue/88?removeFromClient=true&blocklist=true", call_log[1]["url"])

        # Check POST MoviesSearch call
        self.assertEqual(call_log[2]["method"], "POST")
        self.assertIn("/api/v3/command", call_log[2]["url"])
        self.assertEqual(call_log[2]["data"], {"name": "MoviesSearch", "movieIds": [777]})

    def test_dry_run_does_not_mutate_state(self):
        self.cfg.dry_run = True
        torrent = {
            "hash": "feedbeef999",
            "name": "DryRun.Show.S01E01",
            "num_seeds": 0,
            "state": "stalledDL",
        }
        mock_queue = {
            "records": [
                {
                    "id": 101,
                    "downloadId": "feedbeef999",
                    "title": "DryRun.Show.S01E01",
                    "episodeId": 500,
                }
            ]
        }

        call_log = []

        def mock_request(url, method="GET", headers=None, data=None, timeout=10):
            call_log.append({"url": url, "method": method})
            if "/api/v3/queue" in url:
                return mock_queue
            return {}

        self.client.request = MagicMock(side_effect=mock_request)
        result = self.client.heal_sonarr_torrent(torrent)

        self.assertTrue(result)
        # In dry run, only GET queue was called, NO DELETE and NO POST command
        self.assertEqual(len(call_log), 1)
        self.assertEqual(call_log[0]["method"], "GET")


class TestQbitAuth(unittest.TestCase):
    def test_qbit_auth_success(self):
        cfg = WatcherrConfig()
        cfg.qbit_user = "admin"
        cfg.qbit_pass = "secret"
        client = WatcherrClient(cfg)

        mock_resp = MagicMock()
        mock_resp.read.return_value = b"Ok."
        mock_resp.__enter__.return_value = mock_resp

        with patch.object(client.opener, "open", return_value=mock_resp):
            auth_ok = client.qbit_auth()
            self.assertTrue(auth_ok)
            self.assertTrue(client._qbit_logged_in)

    def test_qbit_auth_failure(self):
        cfg = WatcherrConfig()
        cfg.qbit_user = "admin"
        cfg.qbit_pass = "wrong"
        client = WatcherrClient(cfg)

        mock_resp = MagicMock()
        mock_resp.read.return_value = b"Fails."
        mock_resp.__enter__.return_value = mock_resp

        with patch.object(client.opener, "open", return_value=mock_resp):
            auth_ok = client.qbit_auth()
            self.assertFalse(auth_ok)
            self.assertFalse(client._qbit_logged_in)


class TestCliArgs(unittest.TestCase):
    def test_parse_args_all_flags(self):
        test_args = [
            "--qbit-url", "http://qbit.local:8080",
            "--qbit-user", "myuser",
            "--qbit-pass", "mypass",
            "--sonarr-url", "http://sonarr.local:8989",
            "--sonarr-api-key", "my_sonarr_key",
            "--radarr-url", "http://radarr.local:7878",
            "--radarr-api-key", "my_radarr_key",
            "--meta-stall-seconds", "150",
            "--zero-seed-stall-seconds", "400",
            "--check-interval", "20",
            "--dry-run",
            "--once",
            "-v",
        ]
        with patch.object(sys, "argv", ["watcherr"] + test_args):
            args = parse_args()
            self.assertEqual(args.qbit_url, "http://qbit.local:8080")
            self.assertEqual(args.qbit_user, "myuser")
            self.assertEqual(args.qbit_pass, "mypass")
            self.assertEqual(args.sonarr_url, "http://sonarr.local:8989")
            self.assertEqual(args.sonarr_api_key, "my_sonarr_key")
            self.assertEqual(args.radarr_url, "http://radarr.local:7878")
            self.assertEqual(args.radarr_api_key, "my_radarr_key")
            self.assertEqual(args.meta_stall_seconds, 150)
            self.assertEqual(args.zero_seed_stall_seconds, 400)
            self.assertEqual(args.check_interval, 20)
            self.assertTrue(args.dry_run)
            self.assertTrue(args.once)
            self.assertTrue(args.verbose)


if __name__ == "__main__":
    unittest.main()
