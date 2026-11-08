#!/usr/bin/env python3
"""
Watcherr - Automated Stalled Torrent Auto-Healing & Indexer Failover Daemon

Monitors qBittorrent downloads managed by Sonarr and Radarr.
When a torrent is stalled with 0 connected seeders (either stuck in metadata
download or 0% progress for a configurable duration), Watcherr automatically:
  1. Removes the stalled torrent from qBittorrent.
  2. Blocklists the dead torrent release in Sonarr/Radarr so it is never grabbed again.
  3. Triggers an automated search in Sonarr/Radarr to find and grab
     a healthy alternative release from other indexers via Prowlarr/Jackett.
"""

import os
import sys
import time
import json
import logging
import argparse
import urllib.request
import urllib.error
import urllib.parse
import http.cookiejar
import xml.etree.ElementTree as ET

__version__ = "1.0.0"

log = logging.getLogger("watcherr")


class WatcherrConfig:
    def __init__(self, args=None):
        args = args or argparse.Namespace()

        self.qbit_url = (
            getattr(args, "qbit_url", None)
            or os.environ.get("QBIT_URL", "http://127.0.0.1:8080")
        ).rstrip("/")
        self.qbit_user = getattr(args, "qbit_user", None) or os.environ.get("QBIT_USER", "")
        self.qbit_pass = getattr(args, "qbit_pass", None) or os.environ.get("QBIT_PASS", "")

        self.sonarr_url = (
            getattr(args, "sonarr_url", None)
            or os.environ.get("SONARR_URL", "http://127.0.0.1:8989")
        ).rstrip("/")
        self.radarr_url = (
            getattr(args, "radarr_url", None)
            or os.environ.get("RADARR_URL", "http://127.0.0.1:7878")
        ).rstrip("/")

        self.sonarr_key = (
            getattr(args, "sonarr_api_key", None)
            or os.environ.get("SONARR_API_KEY")
            or self._discover_api_key("/var/lib/sonarr/config.xml")
            or self._discover_api_key(os.path.expanduser("~/.config/Sonarr/config.xml"))
            or ""
        )

        self.radarr_key = (
            getattr(args, "radarr_api_key", None)
            or os.environ.get("RADARR_API_KEY")
            or self._discover_api_key("/var/lib/radarr/config.xml")
            or self._discover_api_key(os.path.expanduser("~/.config/Radarr/config.xml"))
            or ""
        )

        self.meta_stall_seconds = int(
            getattr(args, "meta_stall_seconds", None)
            or os.environ.get("METADATA_STALL_SECONDS", 180)
        )
        self.zero_seed_stall_seconds = int(
            getattr(args, "zero_seed_stall_seconds", None)
            or os.environ.get("ZERO_SEED_STALL_SECONDS", 300)
        )
        self.check_interval_seconds = int(
            getattr(args, "check_interval", None)
            or os.environ.get("CHECK_INTERVAL_SECONDS", 45)
        )
        self.dry_run = getattr(args, "dry_run", False) or (
            os.environ.get("DRY_RUN", "false").lower() in ("true", "1", "yes")
        )
        self.once = getattr(args, "once", False)

    @staticmethod
    def _discover_api_key(config_path):
        if not os.path.isfile(config_path):
            return None
        try:
            tree = ET.parse(config_path)
            root = tree.getroot()
            key_elem = root.find("ApiKey")
            if key_elem is not None and key_elem.text:
                return key_elem.text.strip()
        except Exception as e:
            log.debug("Failed reading config %s: %s", config_path, e)
        return None


class WatcherrClient:
    def __init__(self, config: WatcherrConfig):
        self.cfg = config
        self.cookie_jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookie_jar)
        )
        self._qbit_logged_in = False

    def request(self, url, method="GET", headers=None, data=None, timeout=10):
        req_headers = {"User-Agent": f"watcherr/{__version__}"}
        if headers:
            req_headers.update(headers)

        body = None
        if data is not None:
            if isinstance(data, (dict, list)):
                req_headers["Content-Type"] = "application/json"
                body = json.dumps(data).encode("utf-8")
            elif isinstance(data, str):
                body = data.encode("utf-8")
            elif isinstance(data, bytes):
                body = data

        req = urllib.request.Request(url, data=body, headers=req_headers, method=method)
        try:
            with self.opener.open(req, timeout=timeout) as resp:
                raw = resp.read()
                if not raw:
                    return {}
                try:
                    return json.loads(raw.decode("utf-8"))
                except ValueError:
                    return raw.decode("utf-8", errors="ignore")
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="ignore")
            log.error("HTTP %s for %s %s: %s", e.code, method, url, err_body)
            raise
        except Exception as e:
            log.error("Network error for %s %s: %s", method, url, e)
            raise

    def qbit_auth(self):
        if not self.cfg.qbit_user and not self.cfg.qbit_pass:
            return True
        if self._qbit_logged_in:
            return True

        login_url = f"{self.cfg.qbit_url}/api/v2/auth/login"
        form_data = urllib.parse.urlencode({
            "username": self.cfg.qbit_user,
            "password": self.cfg.qbit_pass,
        }).encode("utf-8")

        req = urllib.request.Request(
            login_url,
            data=form_data,
            headers={
                "User-Agent": f"watcherr/{__version__}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            method="POST",
        )
        try:
            with self.opener.open(req, timeout=8) as resp:
                text = resp.read().decode("utf-8", errors="ignore")
                if text.strip().lower() == "ok.":
                    self._qbit_logged_in = True
                    log.info("Successfully authenticated with qBittorrent WebUI.")
                    return True
                log.warning("qBittorrent login responded: %s", text)
        except Exception as e:
            log.warning("qBittorrent login attempt failed: %s", e)
        return False

    def get_qbit_torrents(self):
        url = f"{self.cfg.qbit_url}/api/v2/torrents/info"
        try:
            return self.request(url, timeout=6) or []
        except urllib.error.HTTPError as e:
            if e.code in (401, 403) and (self.cfg.qbit_user or self.cfg.qbit_pass):
                self._qbit_logged_in = False
                if self.qbit_auth():
                    return self.request(url, timeout=6) or []
            return []
        except Exception as e:
            log.warning("Unable to fetch qBittorrent torrents from %s: %s", url, e)
            return []

    def heal_sonarr_torrent(self, torrent):
        if not self.cfg.sonarr_key:
            log.warning("Sonarr API key not configured. Skipping Sonarr healing.")
            return False

        torrent_hash = (torrent.get("hash") or "").lower()
        torrent_name = torrent.get("name", "Unknown")

        try:
            queue_data = self.request(
                f"{self.cfg.sonarr_url}/api/v3/queue?includeUnknownSeriesItems=true",
                headers={"X-Api-Key": self.cfg.sonarr_key},
                timeout=8,
            )
        except Exception as e:
            log.warning("Failed querying Sonarr queue: %s", e)
            return False

        records = queue_data.get("records", []) if isinstance(queue_data, dict) else []
        matched = None
        for r in records:
            download_id = (r.get("downloadId") or "").lower()
            if download_id == torrent_hash or (r.get("title") or "").strip() == torrent_name.strip():
                matched = r
                break

        if not matched:
            log.info("Torrent '%s' not found in Sonarr queue (external or unmanaged download).", torrent_name)
            return False

        queue_id = matched.get("id")
        episode_id = matched.get("episodeId") or (matched.get("episode") or {}).get("id")
        series_id = matched.get("seriesId") or (matched.get("series") or {}).get("id")

        if self.cfg.dry_run:
            log.info(
                "[DRY RUN] Would remove & blocklist queue item %s (release: '%s', hash: %s) in Sonarr and trigger search.",
                queue_id, torrent_name, torrent_hash,
            )
            return True

        log.warning(
            "Healer action triggered: Removing dead release '%s' (hash: %s) from Sonarr queue %s with blocklist=true...",
            torrent_name, torrent_hash, queue_id,
        )

        try:
            del_url = f"{self.cfg.sonarr_url}/api/v3/queue/{queue_id}?removeFromClient=true&blocklist=true"
            self.request(del_url, method="DELETE", headers={"X-Api-Key": self.cfg.sonarr_key}, timeout=8)
            log.info("Successfully blocklisted and removed '%s' from Sonarr queue.", torrent_name)
        except Exception as e:
            log.error("Failed removing queue item %s from Sonarr: %s", queue_id, e)
            return False

        try:
            if episode_id:
                cmd = {"name": "EpisodeSearch", "episodeIds": [episode_id]}
                self.request(
                    f"{self.cfg.sonarr_url}/api/v3/command",
                    method="POST",
                    headers={"X-Api-Key": self.cfg.sonarr_key},
                    data=cmd,
                    timeout=8,
                )
                log.info("Dispatched automated EpisodeSearch command for episode ID %s.", episode_id)
            elif series_id:
                cmd = {"name": "SeriesSearch", "seriesId": series_id}
                self.request(
                    f"{self.cfg.sonarr_url}/api/v3/command",
                    method="POST",
                    headers={"X-Api-Key": self.cfg.sonarr_key},
                    data=cmd,
                    timeout=8,
                )
                log.info("Dispatched automated SeriesSearch command for series ID %s.", series_id)
        except Exception as e:
            log.error("Failed to trigger automated search in Sonarr: %s", e)

        return True

    def heal_radarr_torrent(self, torrent):
        if not self.cfg.radarr_key:
            log.warning("Radarr API key not configured. Skipping Radarr healing.")
            return False

        torrent_hash = (torrent.get("hash") or "").lower()
        torrent_name = torrent.get("name", "Unknown")

        try:
            queue_data = self.request(
                f"{self.cfg.radarr_url}/api/v3/queue?includeUnknownMovieItems=true",
                headers={"X-Api-Key": self.cfg.radarr_key},
                timeout=8,
            )
        except Exception as e:
            log.warning("Failed querying Radarr queue: %s", e)
            return False

        records = queue_data.get("records", []) if isinstance(queue_data, dict) else []
        matched = None
        for r in records:
            download_id = (r.get("downloadId") or "").lower()
            if download_id == torrent_hash or (r.get("title") or "").strip() == torrent_name.strip():
                matched = r
                break

        if not matched:
            log.info("Torrent '%s' not found in Radarr queue (external or unmanaged download).", torrent_name)
            return False

        queue_id = matched.get("id")
        movie_id = matched.get("movieId") or (matched.get("movie") or {}).get("id")

        if self.cfg.dry_run:
            log.info(
                "[DRY RUN] Would remove & blocklist queue item %s (release: '%s', hash: %s) in Radarr and trigger search.",
                queue_id, torrent_name, torrent_hash,
            )
            return True

        log.warning(
            "Healer action triggered: Removing dead release '%s' (hash: %s) from Radarr queue %s with blocklist=true...",
            torrent_name, torrent_hash, queue_id,
        )

        try:
            del_url = f"{self.cfg.radarr_url}/api/v3/queue/{queue_id}?removeFromClient=true&blocklist=true"
            self.request(del_url, method="DELETE", headers={"X-Api-Key": self.cfg.radarr_key}, timeout=8)
            log.info("Successfully blocklisted and removed '%s' from Radarr queue.", torrent_name)
        except Exception as e:
            log.error("Failed removing queue item %s from Radarr: %s", queue_id, e)
            return False

        try:
            if movie_id:
                cmd = {"name": "MoviesSearch", "movieIds": [movie_id]}
                self.request(
                    f"{self.cfg.radarr_url}/api/v3/command",
                    method="POST",
                    headers={"X-Api-Key": self.cfg.radarr_key},
                    data=cmd,
                    timeout=8,
                )
                log.info("Dispatched automated MoviesSearch command for movie ID %s.", movie_id)
        except Exception as e:
            log.error("Failed to trigger automated search in Radarr: %s", e)

        return True

    def inspect_and_heal(self):
        torrents = self.get_qbit_torrents()
        if not torrents:
            return

        for t in torrents:
            category = (t.get("category") or "").lower()
            state = t.get("state", "")
            num_seeds = int(t.get("num_seeds") or 0)
            progress = float(t.get("progress") or 0.0)
            dlspeed = int(t.get("dlspeed") or 0)
            time_active = int(t.get("time_active") or 0)
            name = t.get("name", "Unknown")

            if progress >= 1.0 or state in ("pausedUP", "uploading", "queuedUP", "checkingUP"):
                continue

            is_stuck_meta = (
                state == "metaDL"
                and num_seeds == 0
                and time_active >= self.cfg.meta_stall_seconds
            )

            is_zero_seeds_stalled = (
                num_seeds == 0
                and progress < 0.05
                and dlspeed == 0
                and time_active >= self.cfg.zero_seed_stall_seconds
                and state in ("stalledDL", "downloading", "queuedDL")
            )

            if is_stuck_meta or is_zero_seeds_stalled:
                reason = "stuck downloading metadata" if is_stuck_meta else "0 connected seeders and 0 B/s speed"
                log.warning(
                    "Detected stalled download: '%s' | Reason: %s | Active: %ds | State: %s | Category: %s",
                    name, reason, time_active, state, category or "none",
                )

                if category == "sonarr" or "tv" in category:
                    self.heal_sonarr_torrent(t)
                elif category == "radarr" or "movie" in category:
                    self.heal_radarr_torrent(t)
                else:
                    if not self.heal_sonarr_torrent(t):
                        self.heal_radarr_torrent(t)


def parse_args():
    parser = argparse.ArgumentParser(
        prog="watcherr",
        description="Watcherr: Automated stalled torrent auto-healing & indexer failover watchdog",
    )
    parser.add_argument("--qbit-url", help="qBittorrent WebUI base URL (default: http://127.0.0.1:8080)")
    parser.add_argument("--qbit-user", help="qBittorrent WebUI username (optional)")
    parser.add_argument("--qbit-pass", help="qBittorrent WebUI password (optional)")
    parser.add_argument("--sonarr-url", help="Sonarr base URL (default: http://127.0.0.1:8989)")
    parser.add_argument("--sonarr-api-key", help="Sonarr API Key")
    parser.add_argument("--radarr-url", help="Radarr base URL (default: http://127.0.0.1:7878)")
    parser.add_argument("--radarr-api-key", help="Radarr API Key")
    parser.add_argument("--meta-stall-seconds", type=int, help="Stall timeout for metaDL in seconds (default: 180)")
    parser.add_argument("--zero-seed-stall-seconds", type=int, help="Stall timeout for 0 seeds in seconds (default: 300)")
    parser.add_argument("--check-interval", type=int, help="Cycle interval in seconds (default: 45)")
    parser.add_argument("--dry-run", action="store_true", help="Inspect without deleting or triggering searches")
    parser.add_argument("--once", action="store_true", help="Run a single inspection pass and exit")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser.parse_args()


def main():
    args = parse_args()
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="[%(asctime)s] [watcherr] [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    config = WatcherrConfig(args)
    client = WatcherrClient(config)

    log.info("Watcherr v%s starting up.", __version__)
    log.info(
        "Config: qBittorrent=%s | Sonarr=%s (key set: %s) | Radarr=%s (key set: %s)",
        config.qbit_url,
        config.sonarr_url,
        bool(config.sonarr_key),
        config.radarr_url,
        bool(config.radarr_key),
    )
    log.info(
        "Thresholds: metaDL=%ds, zero-seeds=%ds, interval=%ds | dry_run=%s",
        config.meta_stall_seconds,
        config.zero_seed_stall_seconds,
        config.check_interval_seconds,
        config.dry_run,
    )

    if config.once:
        log.info("Executing single inspection cycle (--once)...")
        client.inspect_and_heal()
        log.info("Inspection complete.")
        return

    while True:
        try:
            client.inspect_and_heal()
        except Exception as e:
            log.error("Unhandled error during inspection cycle: %s", e)
        time.sleep(config.check_interval_seconds)


if __name__ == "__main__":
    main()
