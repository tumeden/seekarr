import os
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


def _env_str(name: str, default: str) -> str:
    value = os.getenv(name)
    if value is None:
        return default
    return str(value).strip()


def _env_int(name: str, default: int, minimum: int | None = None, maximum: int | None = None) -> int:
    raw = os.getenv(name)
    try:
        value = int(raw) if raw is not None else default
    except (TypeError, ValueError):
        value = default
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def default_db_path() -> str:
    explicit = _env_str("SEEKARR_DB_PATH", "")
    if explicit:
        return explicit
    docker_data_dir = Path("/data")
    if docker_data_dir.exists():
        return str(docker_data_dir / "seekarr.db")
    return str(Path("./state/seekarr.db"))


def load_app_config(db_path: str | None = None) -> AppConfig:
    resolved_db_path = str(db_path or default_db_path()).strip() or default_db_path()
    return AppConfig(
        db_path=resolved_db_path,
        item_retry_hours=_env_int("SEEKARR_ITEM_RETRY_HOURS", 12, minimum=1),
        min_hours_after_release=_env_int("SEEKARR_MIN_HOURS_AFTER_RELEASE", 8, minimum=0),
        quiet_hours_start=_env_str("SEEKARR_QUIET_HOURS_START", "23:00"),
        quiet_hours_end=_env_str("SEEKARR_QUIET_HOURS_END", "06:00"),
        quiet_hours_timezone=_env_str("SEEKARR_QUIET_HOURS_TIMEZONE", ""),
        max_missing_actions_per_instance_per_sync=_env_int(
            "SEEKARR_MAX_MISSING_ACTIONS_PER_INSTANCE_PER_SYNC", 5, minimum=0
        ),
        max_cutoff_actions_per_instance_per_sync=_env_int(
            "SEEKARR_MAX_CUTOFF_ACTIONS_PER_INSTANCE_PER_SYNC", 1, minimum=0
        ),
        min_seconds_between_actions=_env_int("SEEKARR_MIN_SECONDS_BETWEEN_ACTIONS", 2, minimum=0),
        rate_window_minutes=_env_int("SEEKARR_RATE_WINDOW_MINUTES", 30, minimum=1),
        rate_cap_per_instance=_env_int("SEEKARR_RATE_CAP_PER_INSTANCE", 10, minimum=1),
        request_timeout_seconds=_env_int("SEEKARR_REQUEST_TIMEOUT_SECONDS", 30, minimum=5),
        verify_ssl=_env_bool("SEEKARR_VERIFY_SSL", True),
        log_level=_env_str("SEEKARR_LOG_LEVEL", "INFO").upper() or "INFO",
    )


def load_runtime_config(db_path: str | None = None) -> RuntimeConfig:
    app = load_app_config(db_path)
    return RuntimeConfig(app=app, radarr_instances=[], sonarr_instances=[])
