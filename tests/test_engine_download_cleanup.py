import logging
from datetime import datetime, timedelta, timezone

from seekarr.config import AppConfig, ArrConfig, ArrSyncInstanceConfig, RuntimeConfig
from seekarr.engine import Engine
from seekarr.state import StateStore


def _base_app_config(db_path: str) -> AppConfig:
    return AppConfig(
        db_path=db_path,
        item_retry_hours=12,
        min_hours_after_release=0,
        quiet_hours_start="",
        quiet_hours_end="",
        quiet_hours_timezone="",
        max_missing_actions_per_instance_per_sync=0,
        max_cutoff_actions_per_instance_per_sync=0,
        min_seconds_between_actions=0,
        rate_window_minutes=60,
        rate_cap_per_instance=10,
        request_timeout_seconds=5,
        verify_ssl=True,
        log_level="INFO",
    )


def _radarr_instance(**overrides) -> ArrSyncInstanceConfig:  # noqa: ANN003
    values = {
        "instance_id": 1,
        "instance_name": "Radarr Main",
        "enabled": True,
        "interval_minutes": 15,
        "search_missing": False,
        "search_cutoff_unmet": False,
        "upgrade_scope": "wanted",
        "search_order": "newest",
        "quiet_hours_enabled": None,
        "quiet_hours_start": None,
        "quiet_hours_end": None,
        "min_hours_after_release": None,
        "min_seconds_between_actions": None,
        "max_missing_actions_per_instance_per_sync": None,
        "max_cutoff_actions_per_instance_per_sync": None,
        "sonarr_missing_mode": "smart",
        "item_retry_hours": None,
        "rate_window_minutes": None,
        "rate_cap": None,
        "arr": ArrConfig(enabled=True, url="http://example", api_key="abc"),
        "cleanup_enabled": True,
        "cleanup_dry_run": True,
        "cleanup_stuck_hours": 24,
        "cleanup_require_issue": True,
        "cleanup_remove_from_client": True,
        "cleanup_blocklist": True,
        "cleanup_skip_redownload": False,
    }
    values.update(overrides)
    return ArrSyncInstanceConfig(**values)


def test_download_cleanup_dry_run_records_candidate_without_removing(monkeypatch, tmp_path) -> None:
    removed: list[int] = []
    old_added = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()

    class FakeArrClient:
        def __init__(self, name, config, timeout_seconds, verify_ssl, logger):  # noqa: ANN001
            self.name = name

        def fetch_queue(self):  # noqa: ANN201
            return [
                {
                    "id": 42,
                    "movieId": 7,
                    "title": "Stuck Movie",
                    "status": "warning",
                    "trackedDownloadStatus": "warning",
                    "added": old_added,
                }
            ]

        def remove_queue_item(self, queue_id, **kwargs):  # noqa: ANN001, ANN003, ANN201
            removed.append(int(queue_id))
            return True

        def fetch_wanted_movies(self, **kwargs):  # noqa: ANN003, ANN201
            return []

    cfg = RuntimeConfig(
        app=_base_app_config(str(tmp_path / "seekarr.db")),
        radarr_instances=[_radarr_instance(cleanup_dry_run=True)],
        sonarr_instances=[],
    )
    monkeypatch.setattr("seekarr.engine.ArrClient", FakeArrClient)

    engine = Engine(config=cfg, logger=logging.getLogger("test"))
    engine.run_instance("radarr", 1, force=True)

    assert removed == []
    rows = StateStore(str(tmp_path / "seekarr.db")).get_recent_search_actions("radarr", 1, limit=5)
    assert rows[0]["action_kind"] == "cleanup_dry_run"
    assert rows[0]["item_key"] == "movie:7"
    assert rows[0]["title"] == "Stuck Movie"


def test_download_cleanup_removes_old_problem_queue_item(monkeypatch, tmp_path) -> None:
    removed: list[tuple[int, dict]] = []
    old_added = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()

    class FakeArrClient:
        def __init__(self, name, config, timeout_seconds, verify_ssl, logger):  # noqa: ANN001
            self.name = name

        def fetch_queue(self):  # noqa: ANN201
            return [
                {
                    "id": 43,
                    "movieId": 8,
                    "title": "Failed Movie",
                    "status": "error",
                    "trackedDownloadStatus": "error",
                    "added": old_added,
                }
            ]

        def remove_queue_item(self, queue_id, **kwargs):  # noqa: ANN001, ANN003, ANN201
            removed.append((int(queue_id), kwargs))
            return True

        def fetch_wanted_movies(self, **kwargs):  # noqa: ANN003, ANN201
            return []

    cfg = RuntimeConfig(
        app=_base_app_config(str(tmp_path / "seekarr.db")),
        radarr_instances=[_radarr_instance(cleanup_dry_run=False, cleanup_skip_redownload=True)],
        sonarr_instances=[],
    )
    monkeypatch.setattr("seekarr.engine.ArrClient", FakeArrClient)

    engine = Engine(config=cfg, logger=logging.getLogger("test"))
    engine.run_instance("radarr", 1, force=True)

    assert removed == [
        (
            43,
            {
                "remove_from_client": True,
                "blocklist": True,
                "skip_redownload": True,
            },
        )
    ]
    rows = StateStore(str(tmp_path / "seekarr.db")).get_recent_search_actions("radarr", 1, limit=5)
    assert rows[0]["action_kind"] == "cleanup"
    assert rows[0]["item_key"] == "movie:8"
