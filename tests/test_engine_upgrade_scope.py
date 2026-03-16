import logging

from seekarr.config import AppConfig, ArrConfig, ArrSyncInstanceConfig, RuntimeConfig
from seekarr.engine import Engine


def _base_app_config(db_path: str) -> AppConfig:
    return AppConfig(
        db_path=db_path,
        item_retry_hours=12,
        min_hours_after_release=0,
        quiet_hours_start="",
        quiet_hours_end="",
        quiet_hours_timezone="",
        max_missing_actions_per_instance_per_sync=0,
        max_cutoff_actions_per_instance_per_sync=1,
        min_seconds_between_actions=0,
        rate_window_minutes=60,
        rate_cap_per_instance=10,
        request_timeout_seconds=5,
        verify_ssl=True,
        log_level="INFO",
    )


def _radarr_instance(upgrade_scope: str) -> ArrSyncInstanceConfig:
    return ArrSyncInstanceConfig(
        instance_id=1,
        instance_name="Radarr Main",
        enabled=True,
        interval_minutes=15,
        search_missing=False,
        search_cutoff_unmet=True,
        upgrade_scope=upgrade_scope,
        search_order="newest",
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


def test_engine_passes_monitored_upgrade_scope(monkeypatch, tmp_path) -> None:
    seen: list[tuple[bool, bool]] = []

    class FakeArrClient:
        def __init__(self, name, config, timeout_seconds, verify_ssl, logger):  # noqa: ANN001
            self.name = name

        def fetch_wanted_movies(self, search_missing=True, search_cutoff_unmet=True, search_all_monitored=False):  # noqa: ANN001
            seen.append((bool(search_cutoff_unmet), bool(search_all_monitored)))
            return []

    cfg = RuntimeConfig(
        app=_base_app_config(str(tmp_path / "seekarr.db")),
        radarr_instances=[_radarr_instance("monitored")],
        sonarr_instances=[],
    )
    monkeypatch.setattr("seekarr.engine.ArrClient", FakeArrClient)

    engine = Engine(config=cfg, logger=logging.getLogger("test"))
    engine.run_instance("radarr", 1, force=True)

    assert seen == [(False, True)]


def test_engine_keeps_wanted_upgrade_scope_default(monkeypatch, tmp_path) -> None:
    seen: list[tuple[bool, bool]] = []

    class FakeArrClient:
        def __init__(self, name, config, timeout_seconds, verify_ssl, logger):  # noqa: ANN001
            self.name = name

        def fetch_wanted_movies(self, search_missing=True, search_cutoff_unmet=True, search_all_monitored=False):  # noqa: ANN001
            seen.append((bool(search_cutoff_unmet), bool(search_all_monitored)))
            return []

    cfg = RuntimeConfig(
        app=_base_app_config(str(tmp_path / "seekarr.db")),
        radarr_instances=[_radarr_instance("wanted")],
        sonarr_instances=[],
    )
    monkeypatch.setattr("seekarr.engine.ArrClient", FakeArrClient)

    engine = Engine(config=cfg, logger=logging.getLogger("test"))
    engine.run_instance("radarr", 1, force=True)

    assert seen == [(True, False)]


def test_engine_passes_both_upgrade_scope(monkeypatch, tmp_path) -> None:
    seen: list[tuple[bool, bool]] = []

    class FakeArrClient:
        def __init__(self, name, config, timeout_seconds, verify_ssl, logger):  # noqa: ANN001
            self.name = name

        def fetch_wanted_movies(self, search_missing=True, search_cutoff_unmet=True, search_all_monitored=False):  # noqa: ANN001
            seen.append((bool(search_cutoff_unmet), bool(search_all_monitored)))
            return []

    cfg = RuntimeConfig(
        app=_base_app_config(str(tmp_path / "seekarr.db")),
        radarr_instances=[_radarr_instance("both")],
        sonarr_instances=[],
    )
    monkeypatch.setattr("seekarr.engine.ArrClient", FakeArrClient)

    engine = Engine(config=cfg, logger=logging.getLogger("test"))
    engine.run_instance("radarr", 1, force=True)

    assert seen == [(True, True)]
