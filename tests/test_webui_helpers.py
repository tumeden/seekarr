import seekarr.webui as webui_module
from seekarr.state import StateStore
from seekarr.webui import _hash_password, _is_newer_version, _parse_semver_tuple, _verify_password, create_app


def test_password_hash_roundtrip() -> None:
    password = "correct horse battery staple"
    hashed = _hash_password(password)
    assert hashed.startswith("pbkdf2_sha256$")
    assert _verify_password(password, hashed) is True
    assert _verify_password("wrong-password", hashed) is False


def test_parse_semver_tuple() -> None:
    assert _parse_semver_tuple("v1.2.3") == (1, 2, 3)
    assert _parse_semver_tuple("1.2.3") == (1, 2, 3)
    assert _parse_semver_tuple("1.2") is None
    assert _parse_semver_tuple("invalid") is None


def test_is_newer_version() -> None:
    assert _is_newer_version("v1.2.3", "v1.2.4") is True
    assert _is_newer_version("1.2.3", "1.2.3") is False
    assert _is_newer_version("1.3.0", "1.2.9") is False


def test_first_run_can_disable_password_and_remembers_choice(tmp_path) -> None:
    app = create_app(str(tmp_path / "seekarr.db"))
    client = app.test_client()

    status = client.get("/api/auth/status")
    assert status.get_json() == {"password_set": False, "auth_configured": False, "password_enabled": False}

    bootstrap = client.post("/api/auth/bootstrap", json={"password_enabled": False})
    assert bootstrap.status_code == 200
    assert client.get("/api/auth/status").get_json() == {
        "password_set": False,
        "auth_configured": True,
        "password_enabled": False,
    }
    assert client.get("/api/status").status_code == 200


def test_password_can_be_enabled_changed_and_removed(tmp_path) -> None:
    app = create_app(str(tmp_path / "seekarr.db"))
    client = app.test_client()
    assert client.post("/api/auth/bootstrap", json={"password_enabled": False}).status_code == 200

    enabled = client.post(
        "/api/auth/password",
        json={"enabled": True, "new_password": "password123"},
    )
    assert enabled.status_code == 200
    assert client.get("/api/status").status_code == 401
    assert client.get("/api/status", headers={"X-Seekarr-Password": "password123"}).status_code == 200

    changed = client.post(
        "/api/auth/password",
        headers={"X-Seekarr-Password": "password123"},
        json={"enabled": True, "current_password": "password123", "new_password": "password456"},
    )
    assert changed.status_code == 200
    assert client.get("/api/status", headers={"X-Seekarr-Password": "password123"}).status_code == 401
    assert client.get("/api/status", headers={"X-Seekarr-Password": "password456"}).status_code == 200

    removed = client.post(
        "/api/auth/password",
        headers={"X-Seekarr-Password": "password456"},
        json={"enabled": False, "current_password": "password456"},
    )
    assert removed.status_code == 200
    assert client.get("/api/status").status_code == 200


def test_webui_shell_and_assets_are_served(tmp_path) -> None:
    app = create_app(str(tmp_path / "seekarr.db"))
    client = app.test_client()

    page = client.get("/")
    assert page.status_code == 200
    assert b'/assets/css/styles.css?v=' in page.data
    assert b'/assets/js/state.js?v=' in page.data
    assert b'/assets/js/init.js?v=' in page.data
    assert b"__ASSET_CACHE_KEY__" not in page.data
    assert "no-cache" in page.headers.get("Cache-Control", "")

    stylesheet = client.get("/assets/css/styles.css")
    assert stylesheet.status_code == 200
    assert stylesheet.mimetype == "text/css"

    logo = client.get("/assets/logo.svg")
    assert logo.status_code == 200
    assert logo.mimetype == "image/svg+xml"

    script = client.get("/assets/js/init.js")
    assert script.status_code == 200
    assert script.mimetype in ("text/javascript", "application/javascript")

    missing = client.get("/assets/not-a-ui-file.js")
    assert missing.status_code == 404


def test_item_meta_endpoint_backfills_search_action_media(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "seekarr.db"
    store = StateStore(str(db_path))
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
            "quiet_hours_enabled": 0,
            "quiet_hours_start": "",
            "quiet_hours_end": "",
            "min_hours_after_release": 0,
            "min_seconds_between_actions": 0,
            "max_missing_actions_per_instance_per_sync": 0,
            "max_cutoff_actions_per_instance_per_sync": 0,
            "sonarr_missing_mode": "smart",
            "item_retry_hours": 24,
            "rate_window_minutes": 60,
            "rate_cap": 10,
            "arr_url": "http://radarr.local:7878",
        },
    )
    store.set_arr_api_key("radarr", 1, "secret")
    store.set_ui_app_settings(cache_images=True)
    store.record_search_action("radarr", 1, "Radarr Main", "movie:5", "missing", "Movie Five")

    monkeypatch.setattr(
        webui_module,
        "resolve_item_meta_by_key",
        lambda *args, **kwargs: {
            "cover_url": "https://img.example/movie-five.jpg",
            "item_url": "http://radarr.local:7878/movie/movie-five",
        },
    )
    monkeypatch.setattr(
        webui_module,
        "cache_cover_image",
        lambda *args, **kwargs: "/media_cache/" + ("a" * 64) + ".jpg",
    )

    app = create_app(str(db_path))
    client = app.test_client()

    bootstrap = client.post("/api/auth/bootstrap", json={"password": "password123"})
    assert bootstrap.status_code == 200
    headers = {"X-Seekarr-Password": "password123"}

    resp = client.get("/api/item_meta?app=radarr&instance_id=1&item_key=movie:5", headers=headers)
    assert resp.status_code == 200
    assert resp.get_json()["cover_url"] == "/media_cache/" + ("a" * 64) + ".jpg"
    assert resp.get_json()["item_url"] == "http://radarr.local:7878/movie/movie-five"

    row = store.get_recent_search_actions("radarr", 1, limit=1)[0]
    assert row["cover_url"] == "/media_cache/" + ("a" * 64) + ".jpg"
    assert row["item_url"] == "http://radarr.local:7878/movie/movie-five"


def test_item_meta_endpoint_retries_cached_remote_cover_until_localized(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "seekarr.db"
    store = StateStore(str(db_path))
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
            "quiet_hours_enabled": 0,
            "quiet_hours_start": "",
            "quiet_hours_end": "",
            "min_hours_after_release": 0,
            "min_seconds_between_actions": 0,
            "max_missing_actions_per_instance_per_sync": 0,
            "max_cutoff_actions_per_instance_per_sync": 0,
            "sonarr_missing_mode": "smart",
            "item_retry_hours": 24,
            "rate_window_minutes": 60,
            "rate_cap": 10,
            "arr_url": "http://radarr.local:7878",
        },
    )
    store.set_arr_api_key("radarr", 1, "secret")
    store.set_ui_app_settings(cache_images=True)
    store.record_search_action("radarr", 1, "Radarr Main", "movie:5", "missing", "Movie Five")
    store.mark_search_action_media_checked("radarr", 1, "movie:5")

    resolver_calls = {"count": 0}

    def resolve_meta(*args, **kwargs):
        resolver_calls["count"] += 1
        return {
            "cover_url": "https://img.example/movie-five.jpg",
            "item_url": "http://radarr.local:7878/movie/movie-five",
        }

    cache_results = iter(["https://img.example/movie-five.jpg", "/media_cache/" + ("c" * 64) + ".jpg"])

    monkeypatch.setattr(webui_module, "resolve_item_meta_by_key", resolve_meta)
    monkeypatch.setattr(webui_module, "cache_cover_image", lambda *args, **kwargs: next(cache_results))

    app = create_app(str(db_path))
    client = app.test_client()

    bootstrap = client.post("/api/auth/bootstrap", json={"password": "password123"})
    assert bootstrap.status_code == 200
    headers = {"X-Seekarr-Password": "password123"}

    first = client.get("/api/item_meta?app=radarr&instance_id=1&item_key=movie:5", headers=headers)
    assert first.status_code == 200
    assert first.get_json()["cover_url"] == "https://img.example/movie-five.jpg"

    second = client.get("/api/item_meta?app=radarr&instance_id=1&item_key=movie:5", headers=headers)
    assert second.status_code == 200
    assert second.get_json()["cover_url"] == "/media_cache/" + ("c" * 64) + ".jpg"
    assert resolver_calls["count"] == 1

    row = store.get_recent_search_actions("radarr", 1, limit=1)[0]
    assert row["cover_url"] == "/media_cache/" + ("c" * 64) + ".jpg"


def test_status_preserves_remote_cover_urls_and_strips_missing_local_cache(tmp_path) -> None:
    db_path = tmp_path / "seekarr.db"
    store = StateStore(str(db_path))
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
            "quiet_hours_enabled": 0,
            "quiet_hours_start": "",
            "quiet_hours_end": "",
            "min_hours_after_release": 0,
            "min_seconds_between_actions": 0,
            "max_missing_actions_per_instance_per_sync": 0,
            "max_cutoff_actions_per_instance_per_sync": 0,
            "sonarr_missing_mode": "smart",
            "item_retry_hours": 24,
            "rate_window_minutes": 60,
            "rate_cap": 10,
            "arr_url": "http://radarr.local:7878",
        },
    )
    store.set_arr_api_key("radarr", 1, "secret")
    store.record_search_action(
        "radarr",
        1,
        "Radarr Main",
        "movie:5",
        "missing",
        "Movie Five",
        item_url="http://radarr.local:7878/movie/movie-five",
        cover_url="https://img.example/movie-five.jpg",
    )
    store.record_search_action(
        "radarr",
        1,
        "Radarr Main",
        "movie:6",
        "missing",
        "Movie Six",
        item_url="http://radarr.local:7878/movie/movie-six",
        cover_url="/media_cache/" + ("d" * 64) + ".jpg",
    )

    app = create_app(str(db_path))
    client = app.test_client()

    bootstrap = client.post("/api/auth/bootstrap", json={"password": "password123"})
    assert bootstrap.status_code == 200
    resp = client.get("/api/status", headers={"X-Seekarr-Password": "password123"})
    assert resp.status_code == 200
    data = resp.get_json()

    assert data["recent_actions"][0]["cover_url"] == ""
    assert data["recent_actions"][1]["cover_url"] == "https://img.example/movie-five.jpg"
    assert data["search_history"]["radarr:1"][0]["cover_url"] == ""
    assert data["search_history"]["radarr:1"][1]["cover_url"] == "https://img.example/movie-five.jpg"


def test_media_cache_route_serves_only_cache_files(tmp_path) -> None:
    db_path = tmp_path / "seekarr.db"
    cache_dir = tmp_path / "media_cache"
    cache_dir.mkdir()
    name = ("b" * 64) + ".jpg"
    (cache_dir / name).write_bytes(b"fake image bytes")

    app = create_app(str(db_path))
    client = app.test_client()

    ok = client.get(f"/media_cache/{name}")
    assert ok.status_code == 200
    assert ok.mimetype == "image/jpeg"
    assert "max-age=604800" in ok.headers.get("Cache-Control", "")

    cached = client.get(f"/media_cache/{name}", headers={"If-None-Match": ok.headers.get("ETag", "")})
    assert cached.status_code in (200, 304)

    bad = client.get("/media_cache/../../seekarr.db")
    assert bad.status_code == 404


def test_media_cache_clear_endpoint_removes_files_and_local_refs(tmp_path) -> None:
    db_path = tmp_path / "seekarr.db"
    store = StateStore(str(db_path))
    cache_dir = tmp_path / "media_cache"
    cache_dir.mkdir()
    name = ("e" * 64) + ".jpg"
    (cache_dir / name).write_bytes(b"fake image bytes")
    store.record_search_action(
        "radarr",
        1,
        "Radarr",
        "movie:5",
        "missing",
        "Movie Five",
        cover_url=f"/media_cache/{name}",
    )

    app = create_app(str(db_path))
    client = app.test_client()
    bootstrap = client.post("/api/auth/bootstrap", json={"password": "password123"})
    assert bootstrap.status_code == 200

    resp = client.post("/api/media_cache/clear", headers={"X-Seekarr-Password": "password123"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["files_removed"] == 1
    assert data["rows_updated"] == 1
    assert not (cache_dir / name).exists()
    row = store.get_recent_search_actions("radarr", 1, limit=1)[0]
    assert row["cover_url"] == ""
