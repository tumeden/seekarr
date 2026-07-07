import sqlite3

from seekarr.state import StateStore


def test_count_search_actions_for_item(tmp_path) -> None:
    store = StateStore(str(tmp_path / "seekarr.db"))
    assert store.count_search_actions_for_item("sonarr", 1, "season:10:1") == 0

    store.record_search_action("sonarr", 1, "Sonarr", "season:10:1", "missing", "Show Season 01 (Pack)")
    store.record_search_action("sonarr", 1, "Sonarr", "season:10:1", "missing", "Show Season 01 (Pack)")
    store.record_search_action("sonarr", 1, "Sonarr", "episode:55", "cutoff", "Show S01E01")

    assert store.count_search_actions_for_item("sonarr", 1, "season:10:1") == 2


def test_record_search_events_bulk_counts_toward_rate_window(tmp_path) -> None:
    store = StateStore(str(tmp_path / "seekarr.db"))
    store.record_search_event("sonarr", 1)
    store.record_search_events("sonarr", 1, 4)

    assert store.count_search_events_since("sonarr", 1, "2000-01-01T00:00:00+00:00") == 5


def test_search_action_media_roundtrip_and_backfill(tmp_path) -> None:
    store = StateStore(str(tmp_path / "seekarr.db"))
    store.record_search_action(
        "sonarr",
        1,
        "Sonarr",
        "series:10",
        "missing",
        "Show Batch",
        item_url="https://arr.example/series/show",
        cover_url="https://arr.example/media/poster.jpg",
    )
    row = store.get_recent_search_actions("sonarr", 1, limit=1)[0]
    assert row["item_url"] == "https://arr.example/series/show"
    assert row["cover_url"] == "https://arr.example/media/poster.jpg"

    store.record_search_action("sonarr", 1, "Sonarr", "series:11", "missing", "Other Show")
    store.set_search_action_media(
        "sonarr",
        1,
        "series:11",
        item_url="https://arr.example/series/other-show",
        cover_url="https://arr.example/media/other-poster.jpg",
    )
    backfilled = store.get_recent_search_actions("sonarr", 1, limit=2)[0]
    assert backfilled["item_url"] == "https://arr.example/series/other-show"
    assert backfilled["cover_url"] == "https://arr.example/media/other-poster.jpg"


def test_search_action_media_backfill_selection_and_checked_marker(tmp_path) -> None:
    store = StateStore(str(tmp_path / "seekarr.db"))
    store.record_search_action("radarr", 1, "Radarr", "movie:1", "missing", "Missing Media")
    store.record_search_action(
        "radarr",
        1,
        "Radarr",
        "movie:2",
        "missing",
        "Remote Cover",
        item_url="https://arr.example/movie/2",
        cover_url="https://img.example/movie-2.jpg",
    )
    with store._connect() as conn:
        conn.execute("UPDATE search_action SET media_checked_at = '' WHERE item_key = 'movie:2'")
    store.record_search_action(
        "radarr",
        1,
        "Radarr",
        "movie:3",
        "missing",
        "Local Cover",
        item_url="https://arr.example/movie/3",
        cover_url="/media_cache/" + ("a" * 64) + ".jpg",
    )

    rows = store.get_search_actions_needing_media_backfill(limit=10)
    assert [row["item_key"] for row in rows] == ["movie:2", "movie:1"]

    store.mark_search_action_media_checked("radarr", 1, "movie:1")
    rows = store.get_search_actions_needing_media_backfill(limit=10)
    assert [row["item_key"] for row in rows] == ["movie:2"]

    store.mark_search_action_media_checked("radarr", 1, "movie:2")
    rows = store.get_search_actions_needing_media_backfill(limit=10)
    assert [row["item_key"] for row in rows] == []


def test_clear_local_search_action_cover_urls(tmp_path) -> None:
    store = StateStore(str(tmp_path / "seekarr.db"))
    local_cover = "/media_cache/" + ("a" * 64) + ".jpg"
    remote_cover = "https://img.example/poster.jpg"
    store.record_search_action("radarr", 1, "Radarr", "movie:1", "missing", "Local", cover_url=local_cover)
    store.record_search_action("radarr", 1, "Radarr", "movie:2", "missing", "Remote", cover_url=remote_cover)

    assert store.clear_local_search_action_cover_urls() == 1
    rows = store.get_recent_search_actions("radarr", 1, limit=2)
    by_key = {row["item_key"]: row for row in rows}
    assert by_key["movie:1"]["cover_url"] == ""
    assert by_key["movie:2"]["cover_url"] == remote_cover


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


def test_ui_app_settings_roundtrip_history_limit(tmp_path) -> None:
    store = StateStore(str(tmp_path / "seekarr.db"))

    assert store.get_ui_app_settings() == {}

    store.set_ui_app_settings(history_limit=123)
    assert store.get_ui_app_settings()["history_limit"] == 123

    store.set_ui_app_settings(history_limit=1)
    assert store.get_ui_app_settings()["history_limit"] == 30

    store.set_ui_app_settings(history_limit=999999)
    assert store.get_ui_app_settings()["history_limit"] == 5000


def test_search_action_history_prunes_to_history_limit(tmp_path) -> None:
    store = StateStore(str(tmp_path / "seekarr.db"))
    store.set_ui_app_settings(history_limit=30)

    for idx in range(35):
        store.record_search_action("radarr", 1, "Radarr", f"movie:{idx}", "missing", f"Movie {idx}")

    rows = store.get_recent_search_actions("radarr", 1, limit=100)
    assert len(rows) == 30
    assert rows[0]["title"] == "Movie 34"
    assert rows[-1]["title"] == "Movie 5"


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


def test_search_action_migrates_media_columns(tmp_path) -> None:
    db_path = tmp_path / "seekarr.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE search_action (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                hunt_type TEXT NOT NULL,
                instance_id INTEGER NOT NULL,
                instance_name TEXT,
                item_key TEXT,
                action_kind TEXT,
                title TEXT NOT NULL,
                occurred_at TEXT NOT NULL
            )
            """
        )

    store = StateStore(str(db_path))
    with store._connect() as conn:
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(search_action)")}
    assert "item_url" in cols
    assert "cover_url" in cols


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
