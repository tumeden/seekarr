import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


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
    # Selection order when choosing what to search this cycle.
    # - newest: newest air/release date first (default)
    # - random: random order
    # - oldest: oldest air/release date first
    search_order: str
    # Per-instance overrides (if None, fall back to app-level defaults).
    quiet_hours_start: str | None
    quiet_hours_end: str | None
    min_hours_after_release: int | None
    min_seconds_between_actions: int | None
    max_missing_actions_per_instance_per_sync: int | None
    max_cutoff_actions_per_instance_per_sync: int | None
    # Sonarr-only: how missing episode searches are triggered.
    # - "smart": season packs for mostly-empty seasons, episodes otherwise (default)
    # - "season_packs": SeasonSearch per season (best for torrent season packs)
    # - "shows": EpisodeSearch for all missing episodes in a show (batch)
    # - "episodes": EpisodeSearch per episode (least efficient)
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


def _expand_env(value: Any) -> Any:
    if isinstance(value, str):

        def repl(match: re.Match[str]) -> str:
            env_name = match.group(1)
            return os.getenv(env_name, "")

        return ENV_PATTERN.sub(repl, value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


def _require_str(data: dict[str, Any], key: str, default: str = "") -> str:
    value = data.get(key, default)
    if value is None:
        return default
    return str(value).strip()


def _load_dotenv_if_present(config_path: Path) -> None:
    # Prefer .env beside config file, then current working directory.
    candidates = [
        config_path.parent / ".env",
        Path.cwd() / ".env",
    ]
    for dotenv_path in candidates:
        if not dotenv_path.exists():
            continue
        try:
            for raw in dotenv_path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
            return
        except OSError:
            return


def _is_docker_data_path(config_path: Path) -> bool:
    # Heuristic: when running in Docker, we default to /data/config.yaml.
    try:
        s = config_path.as_posix()
    except Exception:
        s = str(config_path).replace("\\", "/")
    return s.startswith("/data/") or s.endswith("/data/config.yaml")


def _ensure_config_exists(config_path: Path) -> None:
    if config_path.exists():
        return
    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
    except PermissionError as exc:
        raise PermissionError(
            f"Cannot create config directory {str(config_path.parent)!r}. "
            "If you're running in Docker, ensure your /data (or /config) volume is writable by the container user "
            "(UID 10001 by default), or run the container as root."
        ) from exc

    # Prefer the repo's config.example.yaml when available (Docker image includes it).
    template_path = Path(__file__).resolve().parents[1] / "config.example.yaml"
    raw: dict[str, Any] = {}
    if template_path.exists():
        try:
            raw_loaded = yaml.safe_load(template_path.read_text(encoding="utf-8")) or {}
            raw = raw_loaded if isinstance(raw_loaded, dict) else {}
        except OSError:
            raw = {}

    raw.setdefault("app", {})
    if isinstance(raw.get("app"), dict):
        raw["app"]["db_path"] = "/data/seekarr.db" if _is_docker_data_path(config_path) else "./state/seekarr.db"

    # Write a usable default config even if the template couldn't be loaded.
    if not raw:
        raw = {
            "app": {
                "db_path": "/data/seekarr.db" if _is_docker_data_path(config_path) else "./state/seekarr.db",
                "request_timeout_seconds": 30,
                "verify_ssl": True,
                "log_level": "INFO",
                "quiet_hours_timezone": "",
            },
            "radarr": {
                "instances": [
                    {
                        "instance_id": 1,
                        "instance_name": "Radarr Main",
                        "enabled": True,
                        "interval_minutes": 15,
                        "search_missing": True,
                        "search_cutoff_unmet": True,
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
                        "radarr": {"url": "", "api_key": ""},
                    }
                ]
            },
            "sonarr": {
                "instances": [
                    {
                        "instance_id": 1,
                        "instance_name": "Sonarr Main",
                        "enabled": True,
                        "interval_minutes": 15,
                        "search_missing": True,
                        "search_cutoff_unmet": True,
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
                        "sonarr": {"url": "", "api_key": ""},
                    }
                ]
            },
        }

    try:
        config_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    except PermissionError as exc:
        raise PermissionError(
            f"Cannot write config file {str(config_path)!r}. "
            "If you're running in Docker, ensure your /data (or /config) volume is writable by the container user "
            "(UID 10001 by default), or run the container as root."
        ) from exc


def load_config(path: str) -> RuntimeConfig:
    config_path = Path(path).resolve()
    _ensure_config_exists(config_path)
    _load_dotenv_if_present(config_path)
    with config_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}

    raw = _expand_env(raw)

    app_raw = raw.get("app", {})
    app = AppConfig(
        db_path=_require_str(app_raw, "db_path", "./state/seekarr.db"),
        item_retry_hours=max(1, int(app_raw.get("item_retry_hours", 12))),
        min_hours_after_release=max(0, int(app_raw.get("min_hours_after_release", 8))),
        quiet_hours_start=_require_str(app_raw, "quiet_hours_start", "23:00"),
        quiet_hours_end=_require_str(app_raw, "quiet_hours_end", "06:00"),
        quiet_hours_timezone=_require_str(app_raw, "quiet_hours_timezone", ""),
        # Per-run caps per instance, split by wanted kind.
        # This prevents "upgrade spam" when cutoff-unmet lists are huge.
        max_missing_actions_per_instance_per_sync=max(
            0, int(app_raw.get("max_missing_actions_per_instance_per_sync", 5))
        ),
        max_cutoff_actions_per_instance_per_sync=max(
            0, int(app_raw.get("max_cutoff_actions_per_instance_per_sync", 1))
        ),
        min_seconds_between_actions=max(0, int(app_raw.get("min_seconds_between_actions", 2))),
        rate_window_minutes=max(1, int(app_raw.get("rate_window_minutes", 30))),
        rate_cap_per_instance=max(1, int(app_raw.get("rate_cap_per_instance", 10))),
        request_timeout_seconds=max(5, int(app_raw.get("request_timeout_seconds", 30))),
        verify_ssl=bool(app_raw.get("verify_ssl", True)),
        log_level=_require_str(app_raw, "log_level", "INFO").upper(),
    )

    def parse_instances(section_key: str, arr_key: str) -> list[ArrSyncInstanceConfig]:
        out: list[ArrSyncInstanceConfig] = []
        for row in raw.get(section_key, {}).get("instances", []) or []:
            if not isinstance(row, dict):
                continue
            enabled = bool(row.get("enabled", True))
            interval_minutes = row.get("interval_minutes")
            try:
                interval_minutes = int(interval_minutes) if interval_minutes is not None else 15
            except (TypeError, ValueError):
                interval_minutes = 15
            arr_raw = row.get(arr_key) if isinstance(row.get(arr_key), dict) else {}
            arr = ArrConfig(
                # Consolidated behavior: Arr connectivity is enabled whenever the instance is enabled.
                enabled=enabled,
                url=_require_str(arr_raw, "url"),
                api_key=_require_str(arr_raw, "api_key"),
            )
            out.append(
                ArrSyncInstanceConfig(
                    instance_id=max(1, int(row.get("instance_id", 1))),
                    instance_name=_require_str(row, "instance_name", f"{arr_key.title()} Default"),
                    enabled=enabled,
                    interval_minutes=max(15, min(60, int(interval_minutes))),
                    # "Missing" = new content not yet grabbed, "cutoff unmet" = upgrades until cutoff.
                    search_missing=bool(row.get("search_missing", True)),
                    search_cutoff_unmet=bool(row.get("search_cutoff_unmet", True)),
                    search_order=_require_str(row, "search_order", "smart").lower(),
                    quiet_hours_start=(
                        _require_str(row, "quiet_hours_start", "") if row.get("quiet_hours_start") is not None else None
                    ),
                    quiet_hours_end=(
                        _require_str(row, "quiet_hours_end", "") if row.get("quiet_hours_end") is not None else None
                    ),
                    min_hours_after_release=(
                        int(row.get("min_hours_after_release"))
                        if row.get("min_hours_after_release") is not None
                        else None
                    ),
                    min_seconds_between_actions=(
                        int(row.get("min_seconds_between_actions"))
                        if row.get("min_seconds_between_actions") is not None
                        else None
                    ),
                    max_missing_actions_per_instance_per_sync=(
                        int(row.get("max_missing_actions_per_instance_per_sync"))
                        if row.get("max_missing_actions_per_instance_per_sync") is not None
                        else None
                    ),
                    max_cutoff_actions_per_instance_per_sync=(
                        int(row.get("max_cutoff_actions_per_instance_per_sync"))
                        if row.get("max_cutoff_actions_per_instance_per_sync") is not None
                        else None
                    ),
                    sonarr_missing_mode=_require_str(row, "sonarr_missing_mode", "smart").lower(),
                    item_retry_hours=(
                        int(row.get("item_retry_hours"))
                        if row.get("item_retry_hours") is not None
                        else (
                            int(row.get("state_management_hours"))
                            if row.get("state_management_hours") is not None
                            else None
                        )
                    ),
                    rate_window_minutes=(
                        int(row.get("rate_window_minutes"))
                        if row.get("rate_window_minutes") is not None
                        else (60 if row.get("hourly_cap") is not None else None)
                    ),
                    rate_cap=(
                        int(row.get("rate_cap"))
                        if row.get("rate_cap") is not None
                        else (int(row.get("hourly_cap")) if row.get("hourly_cap") is not None else None)
                    ),
                    arr=arr,
                )
            )
        return out

    # Compatibility mode for the first simplified config shape.
    radarr_instances = parse_instances("radarr", "radarr")
    sonarr_instances = parse_instances("sonarr", "sonarr")

    # Backward-compat: allow old movie_hunt/tv_hunt sections.
    if not radarr_instances:
        for inst in parse_instances("movie_hunt", "radarr"):
            radarr_instances.append(inst)
    if not sonarr_instances:
        for inst in parse_instances("tv_hunt", "sonarr"):
            sonarr_instances.append(inst)

    if not radarr_instances and not sonarr_instances:
        radarr_raw = raw.get("radarr", {})
        sonarr_raw = raw.get("sonarr", {})
        if radarr_raw.get("enabled", True):
            radarr_instances.append(
                ArrSyncInstanceConfig(
                    instance_id=1,
                    instance_name="Radarr Default",
                    enabled=True,
                    interval_minutes=15,
                    search_missing=True,
                    search_cutoff_unmet=True,
                    search_order="smart",
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
                    arr=ArrConfig(
                        enabled=bool(radarr_raw.get("enabled", True)),
                        url=_require_str(radarr_raw, "url"),
                        api_key=_require_str(radarr_raw, "api_key"),
                    ),
                )
            )
        if sonarr_raw.get("enabled", True):
            sonarr_instances.append(
                ArrSyncInstanceConfig(
                    instance_id=1,
                    instance_name="Sonarr Default",
                    enabled=True,
                    interval_minutes=15,
                    search_missing=True,
                    search_cutoff_unmet=True,
                    search_order="smart",
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
                    arr=ArrConfig(
                        enabled=bool(sonarr_raw.get("enabled", True)),
                        url=_require_str(sonarr_raw, "url"),
                        api_key=_require_str(sonarr_raw, "api_key"),
                    ),
                )
            )

    return RuntimeConfig(app=app, radarr_instances=radarr_instances, sonarr_instances=sonarr_instances)
