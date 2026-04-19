from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AppConfig:
    db_path: str
    item_retry_hours: int
    min_hours_after_release: int
    quiet_hours_start: str
    quiet_hours_end: str
    quiet_hours_timezone: str
    max_missing_actions_per_instance_per_sync: int
    max_cutoff_actions_per_instance_per_sync: int
    min_seconds_between_actions: int
    rate_window_minutes: int
    rate_cap_per_instance: int
    request_timeout_seconds: int
    verify_ssl: bool
    log_level: str


@dataclass(frozen=True)
class ArrConfig:
    enabled: bool
    url: str
    api_key: str


@dataclass(frozen=True)
class ArrSyncInstanceConfig:
    instance_id: int
    instance_name: str
    enabled: bool
    interval_minutes: int
    search_missing: bool
    search_cutoff_unmet: bool
    upgrade_scope: str
    search_order: str
    quiet_hours_start: str | None
    quiet_hours_end: str | None
    min_hours_after_release: int | None
    min_seconds_between_actions: int | None
    max_missing_actions_per_instance_per_sync: int | None
    max_cutoff_actions_per_instance_per_sync: int | None
    sonarr_missing_mode: str
    item_retry_hours: int | None
    rate_window_minutes: int | None
    rate_cap: int | None
    arr: ArrConfig


@dataclass(frozen=True)
class RuntimeConfig:
    app: AppConfig
    radarr_instances: list[ArrSyncInstanceConfig]
    sonarr_instances: list[ArrSyncInstanceConfig]


def default_db_path() -> str:
    docker_data_dir = Path("/data")
    if docker_data_dir.exists():
        return str(docker_data_dir / "seekarr.db")
    return str(Path("./state/seekarr.db"))


def load_app_config(db_path: str | None = None) -> AppConfig:
    resolved_db_path = str(db_path or default_db_path()).strip() or default_db_path()
    return AppConfig(
        db_path=resolved_db_path,
        item_retry_hours=12,
        min_hours_after_release=8,
        quiet_hours_start="23:00",
        quiet_hours_end="06:00",
        quiet_hours_timezone="",
        max_missing_actions_per_instance_per_sync=5,
        max_cutoff_actions_per_instance_per_sync=1,
        min_seconds_between_actions=2,
        rate_window_minutes=30,
        rate_cap_per_instance=10,
        request_timeout_seconds=30,
        verify_ssl=True,
        log_level="INFO",
    )


def load_runtime_config(db_path: str | None = None) -> RuntimeConfig:
    app = load_app_config(db_path)
    return RuntimeConfig(app=app, radarr_instances=[], sonarr_instances=[])
