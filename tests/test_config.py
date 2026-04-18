from seekarr.config import default_db_path, load_app_config, load_runtime_config


def test_load_app_config_uses_explicit_db_path_and_env(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "seekarr.db"
    monkeypatch.setenv("SEEKARR_REQUEST_TIMEOUT_SECONDS", "3")
    monkeypatch.setenv("SEEKARR_VERIFY_SSL", "false")
    monkeypatch.setenv("SEEKARR_LOG_LEVEL", "debug")
    monkeypatch.setenv("SEEKARR_RATE_CAP_PER_INSTANCE", "25")

    cfg = load_app_config(str(db_path))

    assert cfg.db_path == str(db_path)
    assert cfg.request_timeout_seconds == 5
    assert cfg.verify_ssl is False
    assert cfg.log_level == "DEBUG"
    assert cfg.rate_cap_per_instance == 25


def test_default_db_path_prefers_env(monkeypatch) -> None:
    monkeypatch.setenv("SEEKARR_DB_PATH", "/tmp/custom-seekarr.db")
    assert default_db_path() == "/tmp/custom-seekarr.db"


def test_load_runtime_config_starts_with_empty_instances(tmp_path) -> None:
    cfg = load_runtime_config(str(tmp_path / "seekarr.db"))
    assert cfg.radarr_instances == []
    assert cfg.sonarr_instances == []
