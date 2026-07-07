import logging
import threading
import time

from seekarr.arr import WantedEpisode
from seekarr.config import AppConfig, ArrConfig, ArrSyncInstanceConfig, RuntimeConfig
from seekarr.engine import Engine, SmartSeasonMonitorState, _sonarr_smart_pace_seconds
from seekarr.state import StateStore


def _base_app_config(db_path: str) -> AppConfig:
    return AppConfig(
        db_path=db_path,
        item_retry_hours=12,
        min_hours_after_release=0,
        quiet_hours_start="",
        quiet_hours_end="",
        quiet_hours_timezone="",
        max_missing_actions_per_instance_per_sync=5,
        max_cutoff_actions_per_instance_per_sync=0,
        min_seconds_between_actions=0,
        rate_window_minutes=60,
        rate_cap_per_instance=10,
        request_timeout_seconds=5,
        verify_ssl=True,
        log_level="INFO",
    )


def _sonarr_instance() -> ArrSyncInstanceConfig:
    return ArrSyncInstanceConfig(
        instance_id=1,
        instance_name="Sonarr Main",
        enabled=True,
        interval_minutes=15,
        search_missing=True,
        search_cutoff_unmet=False,
        upgrade_scope="wanted",
        search_order="smart",
        quiet_hours_enabled=None,
        quiet_hours_start=None,
        quiet_hours_end=None,
        min_hours_after_release=None,
        min_seconds_between_actions=None,
        max_missing_actions_per_instance_per_sync=None,
        max_cutoff_actions_per_instance_per_sync=None,
        sonarr_missing_mode="smart",
        item_retry_hours=None,
        rate_window_minutes=None,
        rate_cap=None,
        arr=ArrConfig(enabled=True, url="http://example", api_key="abc"),
    )


def test_smart_season_monitor_waits_for_later_episode_queue_items(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("seekarr.engine.SMART_SEASON_MONITOR_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setattr("seekarr.engine.SMART_SEASON_MONITOR_POLL_SECONDS", 0.05)
    monkeypatch.setattr("seekarr.engine.SMART_SEASON_MONITOR_SETTLE_SECONDS", 0.12)
    monkeypatch.setattr("seekarr.engine.SMART_SEASON_EPISODE_EXPLOSION_THRESHOLD", 3)

    rows = [
        [],
        [
            {"id": 1, "seriesId": 10, "seasonNumber": 1, "episodeId": 101},
        ],
        [
            {"id": 1, "seriesId": 10, "seasonNumber": 1, "episodeId": 101, "title": "Example S01E01"},
            {"id": 2, "seriesId": 10, "seasonNumber": 1, "episodeId": 102, "title": "Example S01E02"},
        ],
        [
            {"id": 1, "seriesId": 10, "seasonNumber": 1, "episodeId": 101, "title": "Example S01E01"},
            {"id": 2, "seriesId": 10, "seasonNumber": 1, "episodeId": 102, "title": "Example S01E02"},
            {"id": 3, "seriesId": 10, "seasonNumber": 1, "episodeId": 103, "title": "Example S01E03"},
        ],
    ]

    class FakeArrClient:
        def __init__(self) -> None:
            self.calls = 0

        def fetch_queue(self):  # noqa: ANN201
            idx = min(self.calls, len(rows) - 1)
            self.calls += 1
            return rows[idx]

    cfg = RuntimeConfig(
        app=_base_app_config(str(tmp_path / "seekarr.db")),
        radarr_instances=[],
        sonarr_instances=[_sonarr_instance()],
    )
    engine = Engine(config=cfg, logger=logging.getLogger("test"))
    monitor_state = SmartSeasonMonitorState(
        brake_event=threading.Event(),
        completed_event=threading.Event(),
        lock=threading.Lock(),
    )
    engine._start_smart_season_queue_monitor(
        client=FakeArrClient(),
        instance=_sonarr_instance(),
        series_id=10,
        season_number=1,
        title="Example Season 01",
        baseline_queue_ids=set(),
        monitor_state=monitor_state,
    )

    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and not monitor_state.brake_event.is_set():
        time.sleep(0.02)

    assert monitor_state.brake_event.is_set()
    assert monitor_state.completed_event.is_set()
    assert monitor_state.snapshot_episode_grabs() == 3
    store = StateStore(str(tmp_path / "seekarr.db"))
    assert store.item_on_cooldown("sonarr", 1, "episode:101", retry_hours=12)
    assert store.item_on_cooldown("sonarr", 1, "episode:102", retry_hours=12)
    assert store.item_on_cooldown("sonarr", 1, "episode:103", retry_hours=12)


def test_smart_season_monitor_ignores_single_pack_like_queue_item(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("seekarr.engine.SMART_SEASON_MONITOR_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr("seekarr.engine.SMART_SEASON_MONITOR_POLL_SECONDS", 0.05)
    monkeypatch.setattr("seekarr.engine.SMART_SEASON_MONITOR_SETTLE_SECONDS", 0.05)
    monkeypatch.setattr("seekarr.engine.SMART_SEASON_EPISODE_EXPLOSION_THRESHOLD", 3)

    class FakeArrClient:
        def fetch_queue(self):  # noqa: ANN201
            return [{"id": 1, "seriesId": 10, "seasonNumber": 1, "title": "Example S01"}]

    cfg = RuntimeConfig(
        app=_base_app_config(str(tmp_path / "seekarr.db")),
        radarr_instances=[],
        sonarr_instances=[_sonarr_instance()],
    )
    engine = Engine(config=cfg, logger=logging.getLogger("test"))
    monitor_state = SmartSeasonMonitorState(
        brake_event=threading.Event(),
        completed_event=threading.Event(),
        lock=threading.Lock(),
    )
    engine._start_smart_season_queue_monitor(
        client=FakeArrClient(),
        instance=_sonarr_instance(),
        series_id=10,
        season_number=1,
        title="Example Season 01",
        baseline_queue_ids=set(),
        monitor_state=monitor_state,
    )

    time.sleep(0.35)

    assert monitor_state.completed_event.is_set()
    assert not monitor_state.brake_event.is_set()
    assert monitor_state.snapshot_episode_grabs() == 0


def test_sonarr_smart_mode_uses_dynamic_seconds_between_actions(monkeypatch, tmp_path) -> None:
    pace_values: list[int] = []

    class FakeArrClient:
        def __init__(self, name, config, timeout_seconds, verify_ssl, logger):  # noqa: ANN001
            self.name = name
            self.config = config

        def fetch_wanted_episodes(self, **kwargs):  # noqa: ANN003, ANN201
            return [
                WantedEpisode(
                    episode_id=101,
                    series_id=10,
                    series_title="Example",
                    series_tvdb_id=1000,
                    season_number=1,
                    episode_number=1,
                    air_date_utc="2020-01-01T00:00:00Z",
                )
            ]

        def fetch_calendar(self, **kwargs):  # noqa: ANN003, ANN201
            return []

        def fetch_queue_episode_ids(self):  # noqa: ANN201
            return set()

        def fetch_series_season_inventory(self, series_id):  # noqa: ANN001, ANN201
            return {1: {"aired_total": 1, "aired_downloaded": 1, "unaired_total": 0}}

        def trigger_episode_search(self, episode_id):  # noqa: ANN001, ANN201
            return True

        def fetch_series_meta(self, series_id):  # noqa: ANN001, ANN201
            return {}

    inst = _sonarr_instance()
    inst = ArrSyncInstanceConfig(
        **{
            **inst.__dict__,
            "min_seconds_between_actions": 99,
            "max_missing_actions_per_instance_per_sync": 1,
        }
    )
    cfg = RuntimeConfig(
        app=_base_app_config(str(tmp_path / "seekarr.db")),
        radarr_instances=[],
        sonarr_instances=[inst],
    )
    monkeypatch.setattr("seekarr.engine.ArrClient", FakeArrClient)
    monkeypatch.setattr(Engine, "_wait_pace", lambda self, seconds: pace_values.append(int(seconds)))

    engine = Engine(config=cfg, logger=logging.getLogger("test"))
    engine.run_instance("sonarr", 1, force=True)

    assert pace_values == [5]


def test_sonarr_smart_pace_scales_with_remaining_missing_budget() -> None:
    assert _sonarr_smart_pace_seconds(20, 0) == 20
    assert _sonarr_smart_pace_seconds(20, 19) == 5
    assert 5 < _sonarr_smart_pace_seconds(20, 10) < 20
