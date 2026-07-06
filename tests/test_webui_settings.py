from pathlib import Path

from seekarr.config import ArrConfig
from seekarr.state import StateStore
from seekarr.webui import create_app


def _bootstrap_password(client) -> dict[str, str]:  # noqa: ANN001
    bootstrap = client.post("/api/auth/bootstrap", json={"password": "password123"})
    assert bootstrap.status_code == 200
    return {"X-Seekarr-Password": "password123"}


def test_settings_can_create_multiple_radarr_instances_from_empty_db(tmp_path: Path) -> None:
    db_path = tmp_path / "seekarr.db"

    app = create_app(str(db_path))
    client = app.test_client()
    headers = _bootstrap_password(client)

    initial = client.get("/api/settings", headers=headers)
    assert initial.status_code == 200
    assert initial.get_json()["instances"] == []

    payload = {
        "app": {"quiet_hours_timezone": "America/Halifax", "history_limit": 123},
        "instances": [
            {
                "app": "radarr",
                "instance_id": 1,
                "instance_name": "Radarr Main",
                "enabled": True,
                "interval_minutes": 15,
                "search_missing": True,
                "search_cutoff_unmet": True,
                "upgrade_scope": "wanted",
                "search_order": "smart",
                "quiet_hours_enabled": True,
                "quiet_hours_start": "23:00",
                "quiet_hours_end": "06:00",
                "min_hours_after_release": 8,
                "min_seconds_between_actions": 2,
                "max_missing_actions_per_instance_per_sync": 5,
                "max_cutoff_actions_per_instance_per_sync": 1,
                "item_retry_hours": 72,
                "rate_window_minutes": 60,
                "rate_cap": 25,
                "cleanup_enabled": True,
                "cleanup_dry_run": True,
                "cleanup_stuck_hours": 36,
                "cleanup_require_issue": True,
                "cleanup_remove_from_client": True,
                "cleanup_blocklist": True,
                "cleanup_skip_redownload": False,
                "arr_url": "http://radarr-a:7878",
                "arr_api_key": "abc123",
            },
            {
                "app": "radarr",
                "instance_id": 2,
                "instance_name": "Radarr 4K",
                "enabled": True,
                "interval_minutes": 20,
                "search_missing": True,
                "search_cutoff_unmet": False,
                "upgrade_scope": "monitored",
                "search_order": "newest",
                "quiet_hours_enabled": False,
                "quiet_hours_start": "22:00",
                "quiet_hours_end": "07:00",
                "min_hours_after_release": 12,
                "min_seconds_between_actions": 3,
                "max_missing_actions_per_instance_per_sync": 4,
                "max_cutoff_actions_per_instance_per_sync": 2,
                "item_retry_hours": 48,
                "rate_window_minutes": 30,
                "rate_cap": 10,
                "cleanup_enabled": False,
                "cleanup_dry_run": True,
                "cleanup_stuck_hours": 12,
                "cleanup_require_issue": False,
                "cleanup_remove_from_client": True,
                "cleanup_blocklist": True,
                "cleanup_skip_redownload": True,
                "arr_url": "http://radarr-b:7878",
                "arr_api_key": "def456",
            },
        ],
    }
    saved = client.post("/api/settings", headers=headers, json=payload)
    assert saved.status_code == 200
    assert saved.get_json()["ok"] is True

    refreshed = client.get("/api/settings", headers=headers)
    assert refreshed.status_code == 200
    body = refreshed.get_json()
    assert body["app"]["quiet_hours_timezone"] == "America/Halifax"
    assert body["app"]["history_limit"] == 123
    assert [(row["app"], row["instance_id"], row["instance_name"]) for row in body["instances"]] == [
        ("radarr", 1, "Radarr Main"),
        ("radarr", 2, "Radarr 4K"),
    ]
    assert [row["arr_url"] for row in body["instances"]] == [
        "http://radarr-a:7878",
        "http://radarr-b:7878",
    ]
    assert [row["quiet_hours_enabled"] for row in body["instances"]] == [True, False]
    assert [row["cleanup_enabled"] for row in body["instances"]] == [True, False]
    assert [row["cleanup_stuck_hours"] for row in body["instances"]] == [36, 12]
    assert [row["cleanup_skip_redownload"] for row in body["instances"]] == [False, True]


def test_settings_defaults_history_limit_to_240_for_new_db(tmp_path: Path) -> None:
    db_path = tmp_path / "seekarr.db"

    app = create_app(str(db_path))
    client = app.test_client()
    headers = _bootstrap_password(client)

    resp = client.get("/api/settings", headers=headers)
    assert resp.status_code == 200
    assert resp.get_json()["app"]["history_limit"] == 240


def test_status_uses_configured_history_limit(tmp_path: Path) -> None:
    db_path = tmp_path / "seekarr.db"
    store = StateStore(str(db_path))
    store.set_ui_app_settings(history_limit=30)
    store.upsert_ui_instance_settings(
        "radarr",
        1,
        {
            "instance_name": "Radarr Main",
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
            "arr_url": "http://radarr-main:7878",
        },
    )
    for idx in range(35):
        store.record_search_action("radarr", 1, "Radarr Main", f"movie:{idx}", "missing", f"Movie {idx}")

    app = create_app(str(db_path))
    client = app.test_client()
    headers = _bootstrap_password(client)

    resp = client.get("/api/status", headers=headers)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["config"]["app"]["history_limit"] == 30
    assert len(body["search_history"]["radarr:1"]) == 30
    assert body["search_history"]["radarr:1"][0]["title"] == "Movie 34"
    assert body["search_history"]["radarr:1"][-1]["title"] == "Movie 5"


def test_saving_lower_history_limit_prunes_unreferenced_media_cache(tmp_path: Path) -> None:
    db_path = tmp_path / "seekarr.db"
    cache_dir = tmp_path / "media_cache"
    cache_dir.mkdir()
    store = StateStore(str(db_path))
    store.set_ui_app_settings(history_limit=240, cache_images=True)

    for idx in range(31):
        name = f"{idx:064x}.jpg"
        (cache_dir / name).write_bytes(f"image {idx}".encode("utf-8"))
        store.record_search_action(
            "radarr",
            1,
            "Radarr Main",
            f"movie:{idx}",
            "missing",
            f"Movie {idx}",
            cover_url=f"/media_cache/{name}",
        )

    app = create_app(str(db_path))
    client = app.test_client()
    headers = _bootstrap_password(client)

    resp = client.post(
        "/api/settings",
        headers=headers,
        json={
            "app": {
                "history_limit": 30,
                "cache_images": True,
                "image_cache_retention_days": 30,
            },
            "instances": [],
        },
    )
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True

    assert not (cache_dir / f"{0:064x}.jpg").exists()
    assert (cache_dir / f"{30:064x}.jpg").exists()
    rows = store.get_recent_search_actions("radarr", 1, limit=100)
    assert len(rows) == 30
    assert rows[-1]["title"] == "Movie 1"


def test_delete_instance_endpoint_removes_instance_and_credentials(tmp_path: Path) -> None:
    db_path = tmp_path / "seekarr.db"

    app = create_app(str(db_path))
    client = app.test_client()
    headers = _bootstrap_password(client)

    seed = client.post(
        "/api/settings",
        headers=headers,
        json={
            "app": {},
            "instances": [
                {
                    "app": "sonarr",
                    "instance_id": 1,
                    "instance_name": "Sonarr Main",
                    "enabled": True,
                    "interval_minutes": 15,
                    "search_missing": True,
                    "search_cutoff_unmet": True,
                    "upgrade_scope": "wanted",
                    "search_order": "smart",
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
                    "arr_url": "http://sonarr-main:8989",
                    "arr_api_key": "abc123",
                }
            ],
        },
    )
    assert seed.status_code == 200

    missing_confirmation = client.post(
        "/api/instances/delete",
        headers=headers,
        json={"app": "sonarr", "instance_id": 1},
    )
    assert missing_confirmation.status_code == 403
    assert missing_confirmation.get_json()["error"] == "Password confirmation failed"

    wrong_confirmation = client.post(
        "/api/instances/delete",
        headers=headers,
        json={"app": "sonarr", "instance_id": 1, "confirm_password": "wrong-password"},
    )
    assert wrong_confirmation.status_code == 403
    assert wrong_confirmation.get_json()["error"] == "Password confirmation failed"

    deleted = client.post(
        "/api/instances/delete",
        headers=headers,
        json={"app": "sonarr", "instance_id": 1, "confirm_password": "password123"},
    )
    assert deleted.status_code == 200
    assert deleted.get_json()["ok"] is True

    refreshed = client.get("/api/settings", headers=headers)
    assert refreshed.status_code == 200
    assert refreshed.get_json()["instances"] == []


def test_settings_reject_invalid_arr_url(tmp_path: Path) -> None:
    db_path = tmp_path / "seekarr.db"

    app = create_app(str(db_path))
    client = app.test_client()
    headers = _bootstrap_password(client)

    saved = client.post(
        "/api/settings",
        headers=headers,
        json={
            "app": {},
            "instances": [
                {
                    "app": "radarr",
                    "instance_id": 1,
                    "instance_name": "Radarr Main",
                    "enabled": True,
                    "interval_minutes": 15,
                    "search_missing": True,
                    "search_cutoff_unmet": True,
                    "upgrade_scope": "wanted",
                    "search_order": "smart",
                    "quiet_hours_start": "23:00",
                    "quiet_hours_end": "06:00",
                    "min_hours_after_release": 8,
                    "min_seconds_between_actions": 2,
                    "max_missing_actions_per_instance_per_sync": 5,
                    "max_cutoff_actions_per_instance_per_sync": 1,
                    "item_retry_hours": 72,
                    "rate_window_minutes": 60,
                    "rate_cap": 25,
                    "arr_url": "<script>alert(1)</script>",
                    "arr_api_key": "abc123",
                }
            ],
        },
    )
    assert saved.status_code == 400
    assert "URL" in saved.get_json()["error"]

    refreshed = client.get("/api/settings", headers=headers)
    assert refreshed.status_code == 200
    assert refreshed.get_json()["instances"] == []


def test_settings_normalize_arr_url_and_reject_htmlish_instance_name(tmp_path: Path) -> None:
    db_path = tmp_path / "seekarr.db"

    app = create_app(str(db_path))
    client = app.test_client()
    headers = _bootstrap_password(client)

    bad_name = client.post(
        "/api/settings",
        headers=headers,
        json={
            "app": {},
            "instances": [
                {
                    "app": "sonarr",
                    "instance_id": 1,
                    "instance_name": "<b>Sonarr Main</b>",
                    "enabled": True,
                    "interval_minutes": 15,
                    "search_missing": True,
                    "search_cutoff_unmet": True,
                    "upgrade_scope": "wanted",
                    "search_order": "smart",
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
                    "arr_url": "HTTP://sonarr-main:8989/",
                    "arr_api_key": "abc123",
                }
            ],
        },
    )
    assert bad_name.status_code == 400
    assert "letters, numbers, spaces, dots, dashes, and underscores" in bad_name.get_json()["error"]

    saved = client.post(
        "/api/settings",
        headers=headers,
        json={
            "app": {},
            "instances": [
                {
                    "app": "sonarr",
                    "instance_id": 1,
                    "instance_name": "  Sonarr   Main  ",
                    "enabled": True,
                    "interval_minutes": 15,
                    "search_missing": True,
                    "search_cutoff_unmet": True,
                    "upgrade_scope": "wanted",
                    "search_order": "smart",
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
                    "arr_url": "HTTP://sonarr-main:8989/",
                    "arr_api_key": "abc123",
                }
            ],
        },
    )
    assert saved.status_code == 200

    refreshed = client.get("/api/settings", headers=headers)
    assert refreshed.status_code == 200
    instance = refreshed.get_json()["instances"][0]
    assert instance["instance_name"] == "Sonarr Main"
    assert instance["arr_url"] == "http://sonarr-main:8989"


def test_settings_reject_weird_instance_name_characters(tmp_path: Path) -> None:
    db_path = tmp_path / "seekarr.db"

    app = create_app(str(db_path))
    client = app.test_client()
    headers = _bootstrap_password(client)

    saved = client.post(
        "/api/settings",
        headers=headers,
        json={
            "app": {},
            "instances": [
                {
                    "app": "radarr",
                    "instance_id": 1,
                    "instance_name": "Radarr Main!!!",
                    "enabled": True,
                    "interval_minutes": 15,
                    "search_missing": True,
                    "search_cutoff_unmet": True,
                    "upgrade_scope": "wanted",
                    "search_order": "smart",
                    "quiet_hours_start": "23:00",
                    "quiet_hours_end": "06:00",
                    "min_hours_after_release": 8,
                    "min_seconds_between_actions": 2,
                    "max_missing_actions_per_instance_per_sync": 5,
                    "max_cutoff_actions_per_instance_per_sync": 1,
                    "item_retry_hours": 72,
                    "rate_window_minutes": 60,
                    "rate_cap": 25,
                    "arr_url": "http://radarr-main:7878",
                    "arr_api_key": "abc123",
                }
            ],
        },
    )
    assert saved.status_code == 400
    assert "letters, numbers, spaces, dots, dashes, and underscores" in saved.get_json()["error"]


def test_instance_connection_test_uses_supplied_url_and_key(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "seekarr.db"
    calls: list[tuple[str, ArrConfig]] = []

    class FakeArrClient:
        def __init__(self, name, config, timeout_seconds, verify_ssl, logger):  # noqa: ANN001
            calls.append((name, config))

        def fetch_system_status(self) -> dict[str, str]:
            return {"appName": "Radarr", "instanceName": "Movies", "version": "5.1.2"}

    monkeypatch.setattr("seekarr.webui.ArrClient", FakeArrClient)

    app = create_app(str(db_path))
    client = app.test_client()
    headers = _bootstrap_password(client)

    tested = client.post(
        "/api/instances/test_connection",
        headers=headers,
        json={
            "app": "radarr",
            "instance_id": 1,
            "arr_url": "HTTP://radarr-main:7878/",
            "arr_api_key": "abc123",
        },
    )

    assert tested.status_code == 200
    body = tested.get_json()
    assert body["ok"] is True
    assert body["message"] == "Connected to Movies 5.1.2"
    assert calls[0][0] == "radarr"
    assert calls[0][1].url == "http://radarr-main:7878"
    assert calls[0][1].api_key == "abc123"


def test_instance_connection_test_uses_stored_key_when_input_key_is_blank(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "seekarr.db"
    calls: list[ArrConfig] = []

    class FakeArrClient:
        def __init__(self, name, config, timeout_seconds, verify_ssl, logger):  # noqa: ANN001
            calls.append(config)

        def fetch_system_status(self) -> dict[str, str]:
            return {"appName": "Sonarr", "version": "4.0.0"}

    monkeypatch.setattr("seekarr.webui.ArrClient", FakeArrClient)

    app = create_app(str(db_path))
    client = app.test_client()
    headers = _bootstrap_password(client)

    saved = client.post(
        "/api/settings",
        headers=headers,
        json={
            "app": {},
            "instances": [
                {
                    "app": "sonarr",
                    "instance_id": 1,
                    "instance_name": "Sonarr Main",
                    "enabled": True,
                    "interval_minutes": 15,
                    "search_missing": True,
                    "search_cutoff_unmet": True,
                    "upgrade_scope": "wanted",
                    "search_order": "smart",
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
                    "arr_url": "http://sonarr-main:8989",
                    "arr_api_key": "stored-key",
                }
            ],
        },
    )
    assert saved.status_code == 200

    tested = client.post(
        "/api/instances/test_connection",
        headers=headers,
        json={
            "app": "sonarr",
            "instance_id": 1,
            "arr_url": "http://sonarr-main:8989",
            "arr_api_key": "",
        },
    )

    assert tested.status_code == 200
    assert tested.get_json()["ok"] is True
    assert calls[0].api_key == "stored-key"


def test_instance_connection_test_requires_api_key_without_stored_key(tmp_path: Path) -> None:
    db_path = tmp_path / "seekarr.db"

    app = create_app(str(db_path))
    client = app.test_client()
    headers = _bootstrap_password(client)

    tested = client.post(
        "/api/instances/test_connection",
        headers=headers,
        json={
            "app": "radarr",
            "instance_id": 1,
            "arr_url": "http://radarr-main:7878",
            "arr_api_key": "",
        },
    )

    assert tested.status_code == 400
    assert tested.get_json()["error"] == "API key is required to test this connection"


def test_instance_connection_test_rejects_wrong_arr_app(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "seekarr.db"

    class FakeArrClient:
        def __init__(self, name, config, timeout_seconds, verify_ssl, logger):  # noqa: ANN001
            pass

        def fetch_system_status(self) -> dict[str, str]:
            return {"appName": "Radarr", "version": "5.1.2"}

    monkeypatch.setattr("seekarr.webui.ArrClient", FakeArrClient)

    app = create_app(str(db_path))
    client = app.test_client()
    headers = _bootstrap_password(client)

    tested = client.post(
        "/api/instances/test_connection",
        headers=headers,
        json={
            "app": "sonarr",
            "instance_id": 1,
            "arr_url": "http://radarr-main:7878",
            "arr_api_key": "abc123",
        },
    )

    assert tested.status_code == 400
    assert tested.get_json()["error"] == "Connected, but this looks like Radarr instead of Sonarr"
