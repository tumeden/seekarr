import sqlite3

from seekarr.state import StateStore


def test_count_search_actions_for_item(tmp_path) -> None:
    store = StateStore(str(tmp_path / "seekarr.db"))
    assert store.count_search_actions_for_item("sonarr", 1, "season:10:1") == 0

    store.record_search_action("sonarr", 1, "Sonarr", "season:10:1", "missing", "Show Season 01 (Pack)")
    store.record_search_action("sonarr", 1, "Sonarr", "season:10:1", "missing", "Show Season 01 (Pack)")
    store.record_search_action("sonarr", 1, "Sonarr", "episode:55", "cutoff", "Show S01E01")

    assert store.count_search_actions_for_item("sonarr", 1, "season:10:1") == 2


def test_ui_instance_settings_roundtrip_upgrade_scope(tmp_path) -> None:
    store = StateStore(str(tmp_path / "seekarr.db"))

    store.upsert_ui_instance_settings(
        "radarr",
        1,
        {
            "instance_name": "Radarr Main",
            "enabled": 1,
            "interval_minutes": 15,
            "search_missing": 1,
            "search_cutoff_unmet": 1,
            "upgrade_scope": "both",
            "search_order": "smart",
            "quiet_hours_enabled": 0,
            "quiet_hours_start": "23:00",
            "quiet_hours_end": "06:00",
            "min_hours_after_release": 8,
            "min_seconds_between_actions": 2,
            "max_missing_actions_per_instance_per_sync": 5,
            "max_cutoff_actions_per_instance_per_sync": 1,
            "sonarr_missing_mode": "smart",
            "item_retry_hours": 72,
            "rate_window_minutes": 60,
            "rate_cap": 25,
            "arr_url": "http://localhost:7878",
        },
    )

    values = store.get_all_ui_instance_settings()[("radarr", 1)]
    assert values["instance_name"] == "Radarr Main"
    assert values["upgrade_scope"] == "both"
    assert values["quiet_hours_enabled"] == 0


def test_ui_instance_settings_migrates_upgrade_scope_column(tmp_path) -> None:
    db_path = tmp_path / "seekarr.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE ui_instance_settings (
                app_type TEXT NOT NULL,
                instance_id INTEGER NOT NULL,
                enabled INTEGER,
                interval_minutes INTEGER,
                search_missing INTEGER,
                search_cutoff_unmet INTEGER,
                search_order TEXT,
                quiet_hours_enabled INTEGER,
                quiet_hours_start TEXT,
                quiet_hours_end TEXT,
                min_hours_after_release INTEGER,
                min_seconds_between_actions INTEGER,
                max_missing_actions_per_instance_per_sync INTEGER,
                max_cutoff_actions_per_instance_per_sync INTEGER,
                sonarr_missing_mode TEXT,
                item_retry_hours INTEGER,
                rate_window_minutes INTEGER,
                rate_cap INTEGER,
                arr_url TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (app_type, instance_id)
            )
            """
        )

    store = StateStore(str(db_path))
    with store._connect() as conn:
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(ui_instance_settings)")}
    assert "instance_name" in cols
    assert "upgrade_scope" in cols
    assert "quiet_hours_enabled" in cols


def test_delete_instance_removes_instance_state_and_credentials(tmp_path) -> None:
    store = StateStore(str(tmp_path / "seekarr.db"))
    store.upsert_ui_instance_settings(
        "sonarr",
        2,
        {
            "instance_name": "Sonarr Anime",
            "enabled": 1,
            "interval_minutes": 15,
            "search_missing": 1,
            "search_cutoff_unmet": 1,
            "upgrade_scope": "wanted",
            "search_order": "smart",
            "quiet_hours_enabled": 1,
            "quiet_hours_start": "23:00",
            "quiet_hours_end": "06:00",
            "min_hours_after_release": 8,
            "min_seconds_between_actions": 2,
            "max_missing_actions_per_instance_per_sync": 5,
            "max_cutoff_actions_per_instance_per_sync": 1,
            "sonarr_missing_mode": "smart",
            "item_retry_hours": 72,
            "rate_window_minutes": 60,
            "rate_cap": 25,
            "arr_url": "http://localhost:8989",
        },
    )
    store.set_arr_api_key("sonarr", 2, "secret")
    store.mark_guid_processed("sonarr", 2, "guid-1")
    store.mark_item_action("sonarr", 2, "episode:10", "guid-1", "Episode 10")
    store.set_next_sync_time("sonarr", 2, "2026-04-10T00:00:00+00:00")
    store.record_search_event("sonarr", 2)
    store.record_search_action("sonarr", 2, "Sonarr Anime", "episode:10", "missing", "Episode 10")
    store.record_instance_run(
        cycle_run_id=1,
        hunt_type="sonarr",
        instance_id=2,
        instance_name="Sonarr Anime",
        started_at="2026-04-10T00:00:00+00:00",
        finished_at="2026-04-10T00:01:00+00:00",
        status="success",
        stats={"actions_triggered": 1},
    )

    store.delete_instance("sonarr", 2)

    assert ("sonarr", 2) not in store.get_all_ui_instance_settings()
    assert store.get_arr_api_key("sonarr", 2) is None
    assert store.get_next_sync_time("sonarr", 2) is None
    assert store.get_recent_search_actions("sonarr", 2) == []
    assert store.get_recent_instance_runs("sonarr", 2) == []
    with store._connect() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) AS c FROM processed_guid WHERE hunt_type = ? AND instance_id = ?",
                ("sonarr", 2),
            ).fetchone()["c"]
            == 0
        )
