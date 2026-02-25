from pathlib import Path

from seekarr.config import load_config


def test_env_interpolation_and_interval_clamp(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("RADARR_API_KEY_1", "abc123")
    monkeypatch.setenv("SONARR_API_KEY_1", "def456")

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        """
app:
  db_path: "./state/seekarr.db"
  request_timeout_seconds: 30
  verify_ssl: true
  log_level: INFO

radarr:
  instances:
    - instance_id: 1
      instance_name: Radarr Main
      enabled: true
      interval_minutes: 1
      search_missing: true
      search_cutoff_unmet: false
      radarr:
        url: http://localhost:7878
        api_key: "${RADARR_API_KEY_1}"

sonarr:
  instances:
    - instance_id: 1
      instance_name: Sonarr Main
      enabled: true
      interval_minutes: 999
      search_missing: true
      search_cutoff_unmet: false
      sonarr:
        url: http://localhost:8989
        api_key: "${SONARR_API_KEY_1}"
""".lstrip(),
        encoding="utf-8",
    )

    cfg = load_config(str(cfg_path))
    assert cfg.radarr_instances[0].arr.api_key == "abc123"
    assert cfg.sonarr_instances[0].arr.api_key == "def456"
    # Interval minutes are clamped to Huntarr-like bounds (15..60)
    assert cfg.radarr_instances[0].interval_minutes == 15
    assert cfg.sonarr_instances[0].interval_minutes == 60


def test_dotenv_loading(tmp_path: Path, monkeypatch) -> None:
    # Ensure dotenv loads if env var is missing.
    monkeypatch.delenv("RADARR_API_KEY_1", raising=False)
    (tmp_path / ".env").write_text("RADARR_API_KEY_1=fromdotenv\n", encoding="utf-8")

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        """
app:
  db_path: "./state/seekarr.db"
  request_timeout_seconds: 30
  verify_ssl: true
  log_level: INFO

radarr:
  instances:
    - instance_id: 1
      instance_name: Radarr Main
      enabled: true
      interval_minutes: 15
      search_missing: false
      search_cutoff_unmet: false
      radarr:
        url: http://localhost:7878
        api_key: "${RADARR_API_KEY_1}"
""".lstrip(),
        encoding="utf-8",
    )

    cfg = load_config(str(cfg_path))
    assert cfg.radarr_instances[0].arr.api_key == "fromdotenv"


def test_sonarr_smart_missing_mode_parsed(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        """
app:
  db_path: "./state/seekarr.db"
  request_timeout_seconds: 30
  verify_ssl: true
  log_level: INFO

sonarr:
  instances:
    - instance_id: 1
      instance_name: Sonarr Main
      enabled: true
      interval_minutes: 25
      search_missing: true
      search_cutoff_unmet: false
      sonarr_missing_mode: Smart
      sonarr:
        url: http://localhost:8989
        api_key: "abc"
""".lstrip(),
        encoding="utf-8",
    )

    cfg = load_config(str(cfg_path))
    assert cfg.sonarr_instances[0].sonarr_missing_mode == "smart"
