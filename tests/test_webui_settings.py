from pathlib import Path

from seekarr.webui import create_app


def test_settings_can_create_multiple_radarr_instances_from_empty_db(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "seekarr.db"

    monkeypatch.setenv("SEEKARR_WEBUI_PASSWORD", "password123")
    app = create_app(str(db_path))
    client = app.test_client()
    headers = {"X-Seekarr-Password": "password123"}

    initial = client.get("/api/settings", headers=headers)
    assert initial.status_code == 200
    assert initial.get_json()["instances"] == []

    payload = {
        "app": {"quiet_hours_timezone": "America/Halifax"},
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
                "quiet_hours_start": "22:00",
                "quiet_hours_end": "07:00",
                "min_hours_after_release": 12,
                "min_seconds_between_actions": 3,
                "max_missing_actions_per_instance_per_sync": 4,
                "max_cutoff_actions_per_instance_per_sync": 2,
                "item_retry_hours": 48,
                "rate_window_minutes": 30,
                "rate_cap": 10,
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
    assert [(row["app"], row["instance_id"], row["instance_name"]) for row in body["instances"]] == [
        ("radarr", 1, "Radarr Main"),
        ("radarr", 2, "Radarr 4K"),
    ]
    assert [row["arr_url"] for row in body["instances"]] == [
        "http://radarr-a:7878",
        "http://radarr-b:7878",
    ]


def test_delete_instance_endpoint_removes_instance_and_credentials(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "seekarr.db"

    monkeypatch.setenv("SEEKARR_WEBUI_PASSWORD", "password123")
    app = create_app(str(db_path))
    client = app.test_client()
    headers = {"X-Seekarr-Password": "password123"}

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
