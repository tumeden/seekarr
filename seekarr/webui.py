import argparse
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.request
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from flask import Flask, jsonify, request, send_file

from .arr import ArrClient, ArrRequestError
from .config import ArrConfig, ArrSyncInstanceConfig, RuntimeConfig, load_runtime_config
from .engine import Engine, _quiet_hours_end_utc
from .item_meta import (
    cache_cover_image,
    media_cache_dir,
    media_cache_stats,
    prune_media_cache,
    resolve_item_meta_by_key,
)
from .state import StateStore
from .utils.logging import setup_logging


class _QuietAccessFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        noisy_paths = [
            '"GET /api/status ',
            '"GET /favicon.ico ',
        ]
        return not any(path in msg for path in noisy_paths)


def _default_instance_name(app_type: str, instance_id: int) -> str:
    app = str(app_type or "").strip().lower()
    label = "Radarr" if app == "radarr" else "Sonarr" if app == "sonarr" else (app.title() or "Instance")
    return f"{label} {max(1, int(instance_id))}"


def _normalize_upgrade_scope(value: Any) -> str:
    scope = str(value or "").strip().lower()
    if scope in ("both", "all", "all_monitored", "full_library"):
        return "both"
    if scope in ("monitored", "library", "monitored_only"):
        return "monitored"
    return "wanted"


def _normalize_search_order(value: Any) -> str:
    order = str(value or "").strip().lower()
    if order in ("smart", "newest", "random", "oldest"):
        return order
    return "smart"


def _normalize_sonarr_missing_mode(value: Any) -> str:
    mode = str(value or "").strip().lower()
    if mode in ("smart", "season_packs", "shows", "episodes"):
        return mode
    return "smart"


def _normalize_date_format(value: Any) -> str:
    fmt = str(value or "").strip().lower()
    if fmt in ("us", "mdy", "mm/dd/yyyy"):
        return "us"
    if fmt in ("eu", "dmy", "dd/mm/yyyy"):
        return "eu"
    return "iso"


def _normalize_time_format(value: Any) -> str:
    fmt = str(value or "").strip().lower()
    if fmt in ("12h", "12", "12hr", "12-hour"):
        return "12h"
    return "24h"


def _normalize_history_limit(value: Any) -> int:
    try:
        return max(30, min(5000, int(value or 240)))
    except (TypeError, ValueError):
        return 240


def _contains_control_chars(value: str) -> bool:
    return any(ord(ch) < 32 or ord(ch) == 127 for ch in value)


_INSTANCE_NAME_RE = re.compile(r"^[A-Za-z0-9._ -]+$")


def _normalize_instance_name(value: Any, app_type: str, instance_id: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    if not text:
        return _default_instance_name(app_type, instance_id)
    if _contains_control_chars(text):
        raise ValueError(f"{app_type.title()} instance #{instance_id} name contains invalid characters")
    if not _INSTANCE_NAME_RE.fullmatch(text):
        raise ValueError(
            f"{app_type.title()} instance #{instance_id} name may only use letters, numbers, spaces, dots, dashes, and underscores"
        )
    if len(text) > 120:
        raise ValueError(f"{app_type.title()} instance #{instance_id} name is too long")
    return text


def _normalize_arr_url(value: Any, app_type: str, instance_id: int) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if _contains_control_chars(raw) or any(ch.isspace() for ch in raw):
        raise ValueError(f"{app_type.title()} instance #{instance_id} URL contains invalid characters")
    parts = urlsplit(raw)
    if parts.scheme.lower() not in ("http", "https"):
        raise ValueError(f"{app_type.title()} instance #{instance_id} URL must start with http:// or https://")
    if not parts.netloc:
        raise ValueError(f"{app_type.title()} instance #{instance_id} URL must include a hostname")
    if parts.username or parts.password:
        raise ValueError(f"{app_type.title()} instance #{instance_id} URL must not include embedded credentials")
    if parts.query or parts.fragment:
        raise ValueError(f"{app_type.title()} instance #{instance_id} URL must not include query strings or fragments")
    normalized_path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme.lower(), parts.netloc, normalized_path, "", ""))


def _normalize_quiet_hours_enabled(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value or "").strip().lower()
    if text in ("0", "false", "no", "off", ""):
        return False
    return True


def _normalize_hhmm_or_empty(value: Any, field_label: str, app_type: str, instance_id: int) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    parts = text.split(":")
    if len(parts) != 2:
        raise ValueError(f"{app_type.title()} instance #{instance_id} {field_label} must use HH:MM")
    try:
        hh = int(parts[0])
        mm = int(parts[1])
    except ValueError as exc:
        raise ValueError(f"{app_type.title()} instance #{instance_id} {field_label} must use HH:MM") from exc
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise ValueError(f"{app_type.title()} instance #{instance_id} {field_label} must use HH:MM")
    return f"{hh:02d}:{mm:02d}"


def _config_view(config: RuntimeConfig, store: StateStore) -> dict[str, Any]:
    app_overrides = store.get_ui_app_settings()

    def _instance_row(app: str, inst) -> dict[str, Any]:
        return {
            "app": app,
            "instance_id": inst.instance_id,
            "instance_name": inst.instance_name,
            "enabled": inst.enabled,
            "interval_minutes": inst.interval_minutes,
            "search_missing": bool(getattr(inst, "search_missing", True)),
            "search_cutoff_unmet": bool(getattr(inst, "search_cutoff_unmet", True)),
            "upgrade_scope": str(getattr(inst, "upgrade_scope", "wanted") or "wanted"),
            "search_order": str(getattr(inst, "search_order", "smart") or "smart"),
            "quiet_hours_enabled": bool(
                True if getattr(inst, "quiet_hours_enabled", None) is None else inst.quiet_hours_enabled
            ),
            "quiet_hours_start": str(getattr(inst, "quiet_hours_start", None) or config.app.quiet_hours_start or ""),
            "quiet_hours_end": str(getattr(inst, "quiet_hours_end", None) or config.app.quiet_hours_end or ""),
            "min_hours_after_release": int(
                getattr(inst, "min_hours_after_release", None)
                if getattr(inst, "min_hours_after_release", None) is not None
                else config.app.min_hours_after_release
            ),
            "min_seconds_between_actions": int(
                getattr(inst, "min_seconds_between_actions", None)
                if getattr(inst, "min_seconds_between_actions", None) is not None
                else config.app.min_seconds_between_actions
            ),
            "max_missing_actions_per_instance_per_sync": int(
                getattr(inst, "max_missing_actions_per_instance_per_sync", None)
                if getattr(inst, "max_missing_actions_per_instance_per_sync", None) is not None
                else getattr(config.app, "max_missing_actions_per_instance_per_sync", 0)
            ),
            "max_cutoff_actions_per_instance_per_sync": int(
                getattr(inst, "max_cutoff_actions_per_instance_per_sync", None)
                if getattr(inst, "max_cutoff_actions_per_instance_per_sync", None) is not None
                else getattr(config.app, "max_cutoff_actions_per_instance_per_sync", 0)
            ),
            "sonarr_missing_mode": str(getattr(inst, "sonarr_missing_mode", "smart") or "smart"),
            "item_retry_hours": inst.item_retry_hours or config.app.item_retry_hours,
            "rate_window_minutes": inst.rate_window_minutes or config.app.rate_window_minutes,
            "rate_cap": inst.rate_cap or config.app.rate_cap_per_instance,
            "arr_enabled": bool(inst.enabled),
            "arr_url": inst.arr.url,
            "api_key_set": bool(store.has_arr_api_key(app, inst.instance_id) or getattr(inst.arr, "api_key", "")),
        }

    rows = []
    for inst in sorted(config.radarr_instances, key=lambda item: int(item.instance_id)):
        rows.append(_instance_row("radarr", inst))
    for inst in sorted(config.sonarr_instances, key=lambda item: int(item.instance_id)):
        rows.append(_instance_row("sonarr", inst))
    return {
        "app": {
            "quiet_hours_timezone": str(getattr(config.app, "quiet_hours_timezone", "") or ""),
            "date_format": _normalize_date_format(app_overrides.get("date_format")),
            "time_format": _normalize_time_format(app_overrides.get("time_format")),
            "cache_images": bool(getattr(config.app, "cache_images", False)),
            "image_cache_retention_days": int(getattr(config.app, "image_cache_retention_days", 30) or 30),
            "history_limit": _normalize_history_limit(app_overrides.get("history_limit")),
            "media_cache": media_cache_stats(config.app.db_path),
        },
        "instances": rows,
    }


def _hash_password(password: str) -> str:
    pw = str(password or "").encode("utf-8")
    salt = secrets.token_bytes(16)
    iterations = 200_000
    dk = hashlib.pbkdf2_hmac("sha256", pw, salt, iterations, dklen=32)
    return "pbkdf2_sha256$%d$%s$%s" % (
        iterations,
        base64.urlsafe_b64encode(salt).decode("ascii").rstrip("="),
        base64.urlsafe_b64encode(dk).decode("ascii").rstrip("="),
    )


def _verify_password(password: str, password_hash: str) -> bool:
    try:
        algo, it_s, salt_s, dk_s = str(password_hash).split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        iterations = int(it_s)
        salt = base64.urlsafe_b64decode(salt_s + "=" * (-len(salt_s) % 4))
        expected = base64.urlsafe_b64decode(dk_s + "=" * (-len(dk_s) % 4))
        got = hashlib.pbkdf2_hmac("sha256", str(password or "").encode("utf-8"), salt, iterations, dklen=len(expected))
        return hmac.compare_digest(got, expected)
    except Exception:
        return False


def _parse_semver_tuple(value: str) -> tuple[int, int, int] | None:
    s = str(value or "").strip()
    m = re.match(r"^v?(\d+)\.(\d+)\.(\d+)", s)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def _is_newer_version(current: str, latest: str) -> bool:
    cur = _parse_semver_tuple(current)
    latest_tuple = _parse_semver_tuple(latest)
    if not cur or not latest_tuple:
        return False
    return latest_tuple > cur


def create_app(db_path: str | None = None) -> Flask:
    project_dir = Path(__file__).resolve().parent.parent
    resolved_db_path = db_path
    if resolved_db_path and not Path(resolved_db_path).is_absolute():
        resolved_db_path = str(project_dir / resolved_db_path)
    base_config = load_runtime_config(resolved_db_path)
    setup_logging(base_config.app.log_level)
    logger = logging.getLogger("seekarr.webui")
    wz = logging.getLogger("werkzeug")
    wz.addFilter(_QuietAccessFilter())
    store = StateStore(base_config.app.db_path)

    def _materialize_db_instance(
        cfg: RuntimeConfig,
        app_type: str,
        instance_id: int,
        row: dict[str, Any],
    ) -> ArrSyncInstanceConfig:
        def _to_bool(value: Any, fallback: bool) -> bool:
            if value is None:
                return fallback
            try:
                return bool(int(value))
            except (TypeError, ValueError):
                return bool(value)

        def _to_int(value: Any, fallback: int) -> int:
            if value is None:
                return fallback
            try:
                return int(value)
            except (TypeError, ValueError):
                return fallback

        enabled = _to_bool(row.get("enabled"), True)
        interval_minutes = max(15, min(60, _to_int(row.get("interval_minutes"), 15)))
        instance_name = str(row.get("instance_name") or "").strip()
        return ArrSyncInstanceConfig(
            instance_id=max(1, int(instance_id)),
            instance_name=instance_name or _default_instance_name(app_type, instance_id),
            enabled=enabled,
            interval_minutes=interval_minutes,
            search_missing=_to_bool(row.get("search_missing"), True),
            search_cutoff_unmet=_to_bool(row.get("search_cutoff_unmet"), True),
            upgrade_scope=_normalize_upgrade_scope(row.get("upgrade_scope")),
            search_order=_normalize_search_order(row.get("search_order")),
            quiet_hours_enabled=_to_bool(row.get("quiet_hours_enabled"), True),
            quiet_hours_start=str(
                row.get("quiet_hours_start") if row.get("quiet_hours_start") is not None else cfg.app.quiet_hours_start
            ).strip(),
            quiet_hours_end=str(
                row.get("quiet_hours_end") if row.get("quiet_hours_end") is not None else cfg.app.quiet_hours_end
            ).strip(),
            min_hours_after_release=_to_int(
                row.get("min_hours_after_release"),
                cfg.app.min_hours_after_release,
            ),
            min_seconds_between_actions=_to_int(
                row.get("min_seconds_between_actions"),
                cfg.app.min_seconds_between_actions,
            ),
            max_missing_actions_per_instance_per_sync=_to_int(
                row.get("max_missing_actions_per_instance_per_sync"),
                cfg.app.max_missing_actions_per_instance_per_sync,
            ),
            max_cutoff_actions_per_instance_per_sync=_to_int(
                row.get("max_cutoff_actions_per_instance_per_sync"),
                cfg.app.max_cutoff_actions_per_instance_per_sync,
            ),
            sonarr_missing_mode=_normalize_sonarr_missing_mode(row.get("sonarr_missing_mode")),
            item_retry_hours=_to_int(
                row.get("item_retry_hours"),
                cfg.app.item_retry_hours,
            ),
            rate_window_minutes=_to_int(
                row.get("rate_window_minutes"),
                cfg.app.rate_window_minutes,
            ),
            rate_cap=_to_int(row.get("rate_cap"), cfg.app.rate_cap_per_instance),
            arr=ArrConfig(
                enabled=enabled,
                url=str(row.get("arr_url") or "").strip(),
                api_key="",
            ),
        )

    def _with_ui_overrides(cfg: RuntimeConfig) -> RuntimeConfig:
        app_overrides = store.get_ui_app_settings()
        qtz = str(app_overrides.get("quiet_hours_timezone") or "").strip()
        app_cfg = replace(
            cfg.app,
            quiet_hours_timezone=qtz or cfg.app.quiet_hours_timezone,
            cache_images=bool(app_overrides.get("cache_images", False)),
            image_cache_retention_days=max(1, min(3650, int(app_overrides.get("image_cache_retention_days") or 30))),
        )

        raw_overrides = store.get_all_ui_instance_settings()
        radarr_instances = [
            _materialize_db_instance(cfg, "radarr", instance_id, row)
            for (app_type, instance_id), row in sorted(raw_overrides.items())
            if app_type == "radarr"
        ]
        sonarr_instances = [
            _materialize_db_instance(cfg, "sonarr", instance_id, row)
            for (app_type, instance_id), row in sorted(raw_overrides.items())
            if app_type == "sonarr"
        ]
        return replace(cfg, app=app_cfg, radarr_instances=radarr_instances, sonarr_instances=sonarr_instances)

    config = _with_ui_overrides(base_config)
    engine = Engine(config=config, logger=logger)
    config_lock = threading.Lock()
    run_lock = threading.Lock()
    run_state_lock = threading.Lock()
    autorun_threads_started: set[tuple[str, int]] = set()
    run_state: dict[str, Any] = {
        "running": False,
        "force": False,
        "started_at": None,
        "last_event": None,
        "actions_triggered": 0,
        "actions_skipped_cooldown": 0,
        "actions_skipped_rate_limit": 0,
        "last_title": None,
        "recent_actions": [],
        "error": None,
        "active_app_type": None,
        "active_instance_id": None,
        "active_instance_name": None,
    }

    def _prune_media_cache_for_config(cfg: RuntimeConfig, force_unreferenced: bool = False) -> dict[str, int]:
        return prune_media_cache(
            cfg.app.db_path,
            store.get_referenced_cover_urls(),
            int(getattr(cfg.app, "image_cache_retention_days", 30) or 30),
            force_unreferenced=force_unreferenced,
        )

    current_version = "dev"
    try:
        with open(project_dir / "version.txt", "r", encoding="utf-8") as f:
            v_text = f.read().strip()
            if v_text:
                current_version = v_text
    except Exception:
        pass

    if current_version == "dev":
        current_version = str(os.getenv("SEEKARR_VERSION", "") or "").strip() or "dev"
    version_lock = threading.Lock()
    version_state: dict[str, Any] = {
        "current": current_version,
        "latest": None,
        "release_url": "https://github.com/tumeden/seekarr/releases/latest",
        "update_available": False,
        "checked_at_epoch": 0.0,
    }

    def _refresh_version_state() -> None:
        now = time.time()
        with version_lock:
            last = float(version_state.get("checked_at_epoch") or 0.0)
            if (now - last) < 6 * 3600:
                return
            version_state["checked_at_epoch"] = now
        req = urllib.request.Request(
            "https://api.github.com/repos/tumeden/seekarr/releases/latest",
            headers={"Accept": "application/vnd.github+json", "User-Agent": "seekarr-webui"},
        )
        try:
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                payload = json.loads(resp.read().decode("utf-8", "ignore"))
            latest = str(payload.get("tag_name") or "").strip()
            release_url = str(payload.get("html_url") or "").strip() or version_state["release_url"]
            with version_lock:
                version_state["latest"] = latest or None
                version_state["release_url"] = release_url
                version_state["update_available"] = bool(latest and _is_newer_version(current_version, latest))
        except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError, OSError):
            return

    def _get_version_state() -> dict[str, Any]:
        with version_lock:
            return {
                "current": version_state.get("current"),
                "latest": version_state.get("latest"),
                "release_url": version_state.get("release_url"),
                "update_available": bool(version_state.get("update_available")),
            }

    threading.Thread(target=_refresh_version_state, name="seekarr-version-check", daemon=True).start()

    app = Flask(__name__)
    ui_dir = Path(__file__).resolve().parent / "ui"
    project_assets_dir = project_dir
    ui_asset_names = {
        "css/styles.css",
        "js/auth.js",
        "js/dashboard.js",
        "js/history.js",
        "js/init.js",
        "js/refresh.js",
        "js/settings.js",
        "js/state.js",
        "js/utils/common.js",
    }

    def _asset_path(name: str) -> Path:
        bundled = ui_dir / "assets" / name
        if bundled.exists():
            return bundled
        fallback = project_assets_dir / name
        return fallback

    def _ui_path(name: str) -> Path:
        bundled = ui_dir / name
        if bundled.exists():
            return bundled
        fallback = project_dir / name
        return fallback

    def _asset_cache_key() -> str:
        digest = hashlib.sha256(str(current_version).encode("utf-8"))
        for name in sorted(ui_asset_names):
            path = _ui_path(name)
            try:
                stat = path.stat()
            except OSError:
                continue
            digest.update(name.encode("utf-8"))
            digest.update(str(int(stat.st_mtime_ns)).encode("ascii"))
            digest.update(str(int(stat.st_size)).encode("ascii"))
        return digest.hexdigest()[:16]

    password_hash = store.get_webui_password_hash()

    def _json_unauthorized(msg: str = "Unauthorized") -> Any:
        return jsonify({"error": msg}), 401

    @app.before_request
    def _auth() -> Any:
        nonlocal password_hash
        if not request.path.startswith("/api/"):
            return None
        if request.path in ("/api/auth/status", "/api/auth/bootstrap"):
            return None
        if not password_hash:
            return _json_unauthorized("Web UI password not set")

        auth = request.headers.get("Authorization", "")
        if auth.startswith("Basic "):
            try:
                decoded = base64.b64decode(auth.split(" ", 1)[1]).decode("utf-8", "ignore")
                pw = decoded.split(":", 1)[1] if ":" in decoded else ""
            except Exception:
                pw = ""
        else:
            pw = str(request.headers.get("X-Seekarr-Password", "") or "")

        if not _verify_password(pw, password_hash):
            return _json_unauthorized()
        return None

    @app.get("/api/auth/status")
    def auth_status() -> Any:
        return jsonify({"password_set": bool(password_hash)})

    @app.post("/api/auth/bootstrap")
    def auth_bootstrap() -> Any:
        nonlocal password_hash
        if password_hash:
            return jsonify({"error": "Password already set"}), 409
        payload = request.get_json(silent=True) or {}
        pw = str(payload.get("password") or "").strip()
        if len(pw) < 8:
            return jsonify({"error": "Password must be at least 8 characters"}), 400
        password_hash = _hash_password(pw)
        store.set_webui_password_hash(password_hash)
        return jsonify({"ok": True})

    @app.post("/api/credentials/clear")
    def clear_credentials() -> Any:
        payload = request.get_json(silent=True) or {}
        app_type = str(payload.get("app") or "").strip().lower()
        try:
            instance_id = int(payload.get("instance_id") or 0)
        except (TypeError, ValueError):
            instance_id = 0
        if app_type not in ("radarr", "sonarr") or instance_id <= 0:
            return jsonify({"error": "Invalid instance"}), 400
        store.clear_arr_api_key(app_type, instance_id)
        return jsonify({"ok": True})

    @app.post("/api/instances/delete")
    def delete_instance() -> Any:
        payload = request.get_json(silent=True) or {}
        app_type = str(payload.get("app") or "").strip().lower()
        confirm_password = str(payload.get("confirm_password") or "")
        try:
            instance_id = int(payload.get("instance_id") or 0)
        except (TypeError, ValueError):
            instance_id = 0
        if app_type not in ("radarr", "sonarr") or instance_id <= 0:
            return jsonify({"error": "Invalid instance"}), 400
        if not password_hash or not _verify_password(confirm_password, password_hash):
            return jsonify({"error": "Password confirmation failed"}), 403
        try:
            store.delete_instance(app_type, instance_id)
            _reload_config()
            return jsonify({"ok": True})
        except Exception as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/api/instances/test_connection")
    def test_instance_connection() -> Any:
        payload = request.get_json(silent=True) or {}
        app_type = str(payload.get("app") or "").strip().lower()
        if app_type not in ("radarr", "sonarr"):
            return jsonify({"ok": False, "error": "Invalid app"}), 400
        try:
            instance_id = int(payload.get("instance_id") or 0)
        except (TypeError, ValueError):
            instance_id = 0
        try:
            arr_url = _normalize_arr_url(payload.get("arr_url"), app_type, instance_id or 1)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        if not arr_url:
            return jsonify({"ok": False, "error": "Arr URL is required"}), 400

        api_key = str(payload.get("arr_api_key") or "").strip()
        if not api_key and instance_id > 0:
            api_key = str(store.get_arr_api_key(app_type, instance_id) or "").strip()
        if not api_key:
            return jsonify({"ok": False, "error": "API key is required to test this connection"}), 400

        cfg = _get_config()
        client = ArrClient(
            name=app_type,
            config=ArrConfig(enabled=True, url=arr_url, api_key=api_key),
            timeout_seconds=max(5, int(cfg.app.request_timeout_seconds or 30)),
            verify_ssl=bool(cfg.app.verify_ssl),
            logger=logger,
        )
        try:
            status = client.fetch_system_status()
        except ArrRequestError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 502

        display_name = str(
            status.get("instanceName")
            or status.get("appName")
            or status.get("branch")
            or ("Radarr" if app_type == "radarr" else "Sonarr")
        ).strip()
        version = str(status.get("version") or "").strip()
        detected_app = str(status.get("appName") or "").strip().lower()
        if detected_app in ("radarr", "sonarr") and detected_app != app_type:
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": f"Connected, but this looks like {detected_app.title()} instead of {app_type.title()}",
                        "name": display_name,
                        "version": version,
                    }
                ),
                400,
            )

        detail = display_name or app_type.title()
        if version:
            detail = f"{detail} {version}"
        return jsonify({"ok": True, "name": display_name, "version": version, "message": f"Connected to {detail}"})

    def _get_config() -> RuntimeConfig:
        with config_lock:
            return config

    item_meta_cache: dict[tuple[str, int, str], tuple[float, dict[str, str]]] = {}
    item_meta_cache_lock = threading.Lock()
    media_backfill_lock = threading.Lock()
    media_backfill_last_started = 0.0
    media_backfill_loop_started = False

    def _find_instance(cfg: RuntimeConfig, app_type: str, instance_id: int) -> ArrSyncInstanceConfig | None:
        pool = cfg.radarr_instances if app_type == "radarr" else cfg.sonarr_instances if app_type == "sonarr" else []
        for inst in pool:
            if int(inst.instance_id) == int(instance_id):
                return inst
        return None

    def _resolve_arr_connection(
        cfg: RuntimeConfig, app_type: str, instance_id: int
    ) -> tuple[str, str, int, bool] | None:
        inst = _find_instance(cfg, app_type, instance_id)
        if inst is None:
            return None
        base_url = str(getattr(inst.arr, "url", "") or "").strip().rstrip("/")
        api_key = str(store.get_arr_api_key(app_type, instance_id) or getattr(inst.arr, "api_key", "") or "").strip()
        if not base_url or not api_key:
            return None
        return (
            base_url,
            api_key,
            max(5, int(cfg.app.request_timeout_seconds or 30)),
            bool(cfg.app.verify_ssl),
        )

    def _local_media_exists(cfg: RuntimeConfig, cover_url: str) -> bool:
        value = str(cover_url or "").strip()
        if not value.startswith("/media_cache/"):
            return True
        name = value.rsplit("/", 1)[-1]
        if not re.fullmatch(r"[a-f0-9]{64}\.(jpg|png|webp|gif)", name):
            return False
        return (media_cache_dir(cfg.app.db_path) / name).is_file()

    def _should_local_cache_cover(cfg: RuntimeConfig, cover_url: str) -> bool:
        value = str(cover_url or "").strip()
        return bool(getattr(cfg.app, "cache_images", False)) and bool(value) and not value.startswith("/media_cache/")

    def _cache_meta_cover(
        cfg: RuntimeConfig,
        *,
        app_type: str,
        instance_id: int,
        item_key: str,
        meta: dict[str, str],
        base_url: str,
        api_key: str,
        timeout_seconds: int,
        verify_ssl: bool,
    ) -> dict[str, str]:
        if not _should_local_cache_cover(cfg, meta.get("cover_url", "")):
            return meta
        cached = dict(meta)
        cached["cover_url"] = cache_cover_image(
            cfg.app.db_path,
            cached.get("cover_url", ""),
            app_type=app_type,
            instance_id=instance_id,
            item_key=item_key,
            timeout_seconds=timeout_seconds,
            verify_ssl=verify_ssl,
            base_url=base_url,
            api_key=api_key,
        )
        return cached

    def _persist_item_meta(app_type: str, instance_id: int, item_key: str, meta: dict[str, str]) -> None:
        if not meta.get("item_url") and not meta.get("cover_url"):
            return
        store.set_search_action_media(
            hunt_type=app_type,
            instance_id=instance_id,
            item_key=item_key,
            item_url=meta.get("item_url", ""),
            cover_url=meta.get("cover_url", ""),
        )

    def _scrub_missing_local_media(cfg: RuntimeConfig, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out = []
        for row in rows:
            current = dict(row)
            cover_url = str(current.get("cover_url") or "").strip()
            if cover_url and not _local_media_exists(cfg, cover_url):
                current["cover_url"] = ""
            out.append(current)
        return out

    def _resolve_item_meta(cfg: RuntimeConfig, app_type: str, instance_id: int, item_key: str) -> dict[str, str]:
        cache_key = (str(app_type).strip().lower(), int(instance_id), str(item_key or "").strip())
        now = time.time()
        conn = _resolve_arr_connection(cfg, app_type, instance_id)
        with item_meta_cache_lock:
            cached = item_meta_cache.get(cache_key)
            if cached and cached[0] > now:
                meta = dict(cached[1])
                if meta.get("cover_url") and not _local_media_exists(cfg, meta.get("cover_url", "")):
                    item_meta_cache.pop(cache_key, None)
                else:
                    if conn is not None:
                        base_url, api_key, timeout_seconds, verify_ssl = conn
                        meta = _cache_meta_cover(
                            cfg,
                            app_type=app_type,
                            instance_id=instance_id,
                            item_key=item_key,
                            meta=meta,
                            base_url=base_url,
                            api_key=api_key,
                            timeout_seconds=timeout_seconds,
                            verify_ssl=verify_ssl,
                        )
                        item_meta_cache[cache_key] = (cached[0], dict(meta))
                    _persist_item_meta(app_type, instance_id, item_key, meta)
                    return meta
        if conn is None:
            empty = {"cover_url": "", "item_url": ""}
            with item_meta_cache_lock:
                item_meta_cache[cache_key] = (now + 300, empty)
            return empty
        base_url, api_key, timeout_seconds, verify_ssl = conn
        meta = resolve_item_meta_by_key(base_url, api_key, timeout_seconds, verify_ssl, app_type, item_key)
        meta = _cache_meta_cover(
            cfg,
            app_type=app_type,
            instance_id=instance_id,
            item_key=item_key,
            meta=meta,
            base_url=base_url,
            api_key=api_key,
            timeout_seconds=timeout_seconds,
            verify_ssl=verify_ssl,
        )
        if meta.get("item_url") or meta.get("cover_url"):
            _persist_item_meta(app_type, instance_id, item_key, meta)
            _prune_media_cache_for_config(cfg)
        with item_meta_cache_lock:
            ttl = 1800 if (meta.get("item_url") or meta.get("cover_url")) else 300
            item_meta_cache[cache_key] = (now + ttl, meta)
        return dict(meta)

    def _run_search_action_media_backfill(reason: str, batch_limit: int = 25) -> None:
        if not media_backfill_lock.acquire(blocking=False):
            return
        try:
            cfg = _get_config()
            retry_before = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
            rows = store.get_search_actions_needing_media_backfill(limit=batch_limit, retry_before_iso=retry_before)
            if not rows:
                return
            logger.info("Backfilling search action media for %s rows (%s)", len(rows), reason)
            for row in rows:
                app_type = str(row.get("app_type") or "").strip().lower()
                item_key = str(row.get("item_key") or "").strip()
                try:
                    instance_id = int(row.get("instance_id") or 0)
                except (TypeError, ValueError):
                    instance_id = 0
                if app_type not in ("radarr", "sonarr") or instance_id <= 0 or not item_key:
                    continue
                try:
                    meta = _resolve_item_meta(cfg, app_type, instance_id, item_key)
                    if not meta.get("item_url") and not meta.get("cover_url"):
                        store.mark_search_action_media_checked(app_type, instance_id, item_key)
                except Exception:
                    logger.debug(
                        "Unable to backfill search action media for %s:%s %s",
                        app_type,
                        instance_id,
                        item_key,
                        exc_info=True,
                    )
                    store.mark_search_action_media_checked(app_type, instance_id, item_key)
                time.sleep(0.15)
        finally:
            media_backfill_lock.release()

    def _schedule_search_action_media_backfill(reason: str, min_interval_seconds: float = 300.0) -> None:
        nonlocal media_backfill_last_started
        now = time.monotonic()
        if (now - media_backfill_last_started) < min_interval_seconds:
            return
        media_backfill_last_started = now
        threading.Thread(
            target=_run_search_action_media_backfill,
            args=(reason,),
            name="webui-media-backfill",
            daemon=True,
        ).start()

    def _ensure_media_backfill_loop() -> None:
        nonlocal media_backfill_loop_started
        if media_backfill_loop_started:
            return
        media_backfill_loop_started = True

        def loop() -> None:
            while True:
                time.sleep(15 * 60)
                _schedule_search_action_media_backfill("periodic", min_interval_seconds=0.0)

        threading.Thread(target=loop, name="webui-media-backfill-loop", daemon=True).start()

    def _reload_config() -> None:
        nonlocal config
        new_base = load_runtime_config(base_config.app.db_path)
        new_config = _with_ui_overrides(new_base)
        with config_lock:
            config = new_config
            engine.config = new_config
        _ensure_autorun_threads(new_config)
        _schedule_search_action_media_backfill("config reload", min_interval_seconds=600.0)

    def _progress_cb(evt: dict[str, Any]) -> None:
        with run_state_lock:
            run_state["last_event"] = evt.get("type")
            if evt.get("type") == "cycle_started":
                run_state["running"] = True
                run_state["force"] = bool(evt.get("force", False))
                run_state["started_at"] = datetime.now(timezone.utc).isoformat()
                run_state["actions_triggered"] = 0
                run_state["actions_skipped_cooldown"] = 0
                run_state["actions_skipped_rate_limit"] = 0
                run_state["last_title"] = None
                run_state["error"] = None
                run_state["active_app_type"] = None
                run_state["active_instance_id"] = None
                run_state["active_instance_name"] = None
            elif evt.get("type") == "instance_started":
                run_state["active_app_type"] = evt.get("app_type")
                run_state["active_instance_id"] = evt.get("instance_id")
                run_state["active_instance_name"] = evt.get("instance_name")
            elif evt.get("type") == "item_triggered":
                run_state["actions_triggered"] = int(evt.get("actions_triggered") or 0)
                run_state["actions_skipped_cooldown"] = int(evt.get("actions_skipped_cooldown") or 0)
                run_state["actions_skipped_rate_limit"] = int(evt.get("actions_skipped_rate_limit") or 0)
                run_state["last_title"] = evt.get("title")
                # Keep a small recent history for the UI.
                try:
                    lst = list(run_state.get("recent_actions") or [])
                except TypeError:
                    lst = []
                lst.append(
                    {
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "app_type": evt.get("app_type"),
                        "instance_name": evt.get("instance_name"),
                        "title": evt.get("title"),
                    }
                )
                run_state["recent_actions"] = lst[-8:]
            elif evt.get("type") == "item_skipped_cooldown":
                run_state["actions_skipped_cooldown"] = int(evt.get("actions_skipped_cooldown") or 0)
            elif evt.get("type") == "item_skipped_rate_limit":
                run_state["actions_skipped_rate_limit"] = int(evt.get("actions_skipped_rate_limit") or 0)
            elif evt.get("type") == "instance_finished":
                # Clear "active" if we just finished the active instance.
                if run_state.get("active_app_type") == evt.get("app_type") and run_state.get(
                    "active_instance_id"
                ) == evt.get("instance_id"):
                    run_state["active_app_type"] = None
                    run_state["active_instance_id"] = None
                    run_state["active_instance_name"] = None
            elif evt.get("type") == "cycle_finished":
                run_state["running"] = False
                run_state["error"] = evt.get("error")
                run_state["active_app_type"] = None
                run_state["active_instance_id"] = None
                run_state["active_instance_name"] = None

    def _start_run_async(force: bool) -> bool:
        if not run_lock.acquire(blocking=False):
            return False

        def runner() -> None:
            try:
                engine.run_cycle(force=force, progress_cb=_progress_cb)
            except ArrRequestError as exc:
                logger.error("Run failed: %s", exc)
                with run_state_lock:
                    run_state["running"] = False
                    run_state["error"] = str(exc)
            except Exception as exc:
                if os.getenv("SEEKARR_DEBUG", "0").strip().lower() in ("1", "true", "yes", "on"):
                    logger.exception("Run failed: %s", exc)
                else:
                    logger.error("Run failed: %s", exc)
                with run_state_lock:
                    run_state["running"] = False
                    run_state["error"] = str(exc)
            finally:
                run_lock.release()

        t = threading.Thread(target=runner, name="webui-run", daemon=True)
        t.start()
        return True

    def _sleep_until(iso: str | None, max_seconds: float = 300.0, heartbeat_seconds: float = 30.0) -> None:
        if not iso:
            time.sleep(1.0)
            return
        try:
            dt = datetime.fromisoformat(str(iso))
        except ValueError:
            time.sleep(1.0)
            return
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        while True:
            now = datetime.now(timezone.utc)
            seconds = max(0.0, (dt.astimezone(timezone.utc) - now).total_seconds())
            if seconds <= 0:
                return
            store.set_scheduler_heartbeat()
            time.sleep(min(seconds, max_seconds, heartbeat_seconds))

    def _instance_sleep_window_enabled(inst: ArrSyncInstanceConfig) -> bool:
        value = getattr(inst, "quiet_hours_enabled", None)
        return True if value is None else bool(value)

    def _autorun_instance_loop(app_type: str, instance_id: int) -> None:
        # Independent per-instance scheduling (no fixed ticker).
        while True:
            try:
                store.set_scheduler_heartbeat()

                inst = engine._find_instance(app_type, instance_id)
                if not inst or not inst.enabled or not inst.arr.enabled:
                    time.sleep(5.0)
                    continue

                # Quiet-hours pre-check: schedule directly to quiet end so the dashboard and
                # autorun loop both enter sleep mode immediately without an unnecessary due run.
                if _instance_sleep_window_enabled(inst):
                    quiet_start = str(getattr(inst, "quiet_hours_start", None) or config.app.quiet_hours_start or "")
                    quiet_end = str(getattr(inst, "quiet_hours_end", None) or config.app.quiet_hours_end or "")
                    quiet_tz = str(getattr(config.app, "quiet_hours_timezone", "") or "")
                    quiet_end_utc = _quiet_hours_end_utc(
                        datetime.now(timezone.utc),
                        quiet_start,
                        quiet_end,
                        quiet_timezone=quiet_tz,
                    )
                    if quiet_end_utc:
                        quiet_iso = quiet_end_utc.isoformat()
                        store.set_next_sync_time(app_type, instance_id, quiet_iso)
                        _sleep_until(quiet_iso)
                        continue

                next_sync = store.get_next_sync_time(app_type, instance_id)
                if next_sync:
                    try:
                        dt = datetime.fromisoformat(str(next_sync))
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        if datetime.now(timezone.utc) < dt.astimezone(timezone.utc):
                            _sleep_until(next_sync)
                            continue
                    except ValueError:
                        pass

                # Due: try to run this instance (avoid overlap with manual runs).
                if not run_lock.acquire(blocking=False):
                    time.sleep(1.0)
                    continue
                try:
                    engine.run_instance(
                        app_type=app_type, instance_id=instance_id, force=False, progress_cb=_progress_cb
                    )
                finally:
                    run_lock.release()
            except ArrRequestError as exc:
                logger.error("Autorun loop error (%s:%s): %s", app_type, instance_id, exc)
                time.sleep(5.0)
            except Exception as exc:
                if os.getenv("SEEKARR_DEBUG", "0").strip().lower() in ("1", "true", "yes", "on"):
                    logger.exception("Autorun loop error (%s:%s): %s", app_type, instance_id, exc)
                else:
                    logger.error("Autorun loop error (%s:%s): %s", app_type, instance_id, exc)
                time.sleep(5.0)

    def _ensure_autorun_threads(cfg: RuntimeConfig) -> None:
        for app_type, instances in (("radarr", cfg.radarr_instances), ("sonarr", cfg.sonarr_instances)):
            for inst in instances:
                key = (app_type, int(inst.instance_id))
                if key in autorun_threads_started:
                    continue
                autorun_threads_started.add(key)
                threading.Thread(
                    target=_autorun_instance_loop,
                    args=key,
                    name=f"webui-autorun-{app_type}-{inst.instance_id}",
                    daemon=True,
                ).start()

    _ensure_autorun_threads(config)
    _ensure_media_backfill_loop()
    _schedule_search_action_media_backfill("startup", min_interval_seconds=0.0)

    @app.get("/")
    def index() -> Any:
        template = _ui_path("index.html")
        if not template.exists():
            return "Web UI file not found", 500
        html = template.read_text(encoding="utf-8").replace("__ASSET_CACHE_KEY__", _asset_cache_key())
        response = app.response_class(html, mimetype="text/html")
        response.cache_control.no_cache = True
        response.cache_control.max_age = 0
        return response

    @app.get("/api/status")
    def status() -> Any:
        _refresh_version_state()
        with run_state_lock:
            rs = dict(run_state)
        cfg = _get_config()

        # Rate status is a rolling-window count per instance, keyed as "app:instance_id".
        rate_status: dict[str, Any] = {}
        now = datetime.now(timezone.utc)
        for inst in config.radarr_instances:
            window_minutes = int(inst.rate_window_minutes or config.app.rate_window_minutes)
            since = (now - timedelta(minutes=window_minutes)).isoformat()
            used = store.count_search_events_since("radarr", inst.instance_id, since)
            rate_status[f"radarr:{inst.instance_id}"] = {"used": used, "window_minutes": window_minutes}
        for inst in config.sonarr_instances:
            window_minutes = int(inst.rate_window_minutes or config.app.rate_window_minutes)
            since = (now - timedelta(minutes=window_minutes)).isoformat()
            used = store.count_search_events_since("sonarr", inst.instance_id, since)
            rate_status[f"sonarr:{inst.instance_id}"] = {"used": used, "window_minutes": window_minutes}

        instance_last_run: dict[str, Any] = {}
        for inst in config.radarr_instances:
            instance_last_run[f"radarr:{inst.instance_id}"] = store.get_last_instance_run("radarr", inst.instance_id)
        for inst in config.sonarr_instances:
            instance_last_run[f"sonarr:{inst.instance_id}"] = store.get_last_instance_run("sonarr", inst.instance_id)

        history_limit = _normalize_history_limit(store.get_ui_app_settings().get("history_limit"))
        search_history: dict[str, Any] = {}
        for inst in cfg.radarr_instances:
            search_history[f"radarr:{inst.instance_id}"] = _scrub_missing_local_media(
                cfg,
                store.get_recent_search_actions("radarr", inst.instance_id, history_limit),
            )
        for inst in cfg.sonarr_instances:
            search_history[f"sonarr:{inst.instance_id}"] = _scrub_missing_local_media(
                cfg,
                store.get_recent_search_actions("sonarr", inst.instance_id, history_limit),
            )

        return jsonify(
            {
                "server_time_utc": datetime.now(timezone.utc).isoformat(),
                "version": _get_version_state(),
                "config": _config_view(cfg, store),
                "sync_status": store.get_sync_statuses(),
                "recent_runs": store.get_recent_runs(20),
                "recent_actions": _scrub_missing_local_media(cfg, store.get_recent_search_actions_global(50)),
                "rate_status": rate_status,
                "instance_last_run": instance_last_run,
                "search_history": search_history,
                "run_state": rs,
                "scheduler_heartbeat": store.get_scheduler_heartbeat(),
            }
        )

    @app.get("/api/settings")
    def get_settings() -> Any:
        cfg = _get_config()
        view = _config_view(cfg, store)
        return jsonify({"app": view.get("app", {}), "instances": view.get("instances", [])})

    @app.post("/api/settings")
    def save_settings() -> Any:
        payload = request.get_json(silent=True) or {}
        inst_in = payload.get("instances") if isinstance(payload.get("instances"), list) else []
        app_in = payload.get("app") if isinstance(payload.get("app"), dict) else {}

        try:
            store.set_ui_app_settings(
                quiet_hours_timezone=str(app_in.get("quiet_hours_timezone") or "").strip(),
                date_format=_normalize_date_format(app_in.get("date_format")),
                time_format=_normalize_time_format(app_in.get("time_format")),
                cache_images=bool(app_in.get("cache_images", False)),
                image_cache_retention_days=max(1, min(3650, int(app_in.get("image_cache_retention_days") or 30))),
                history_limit=_normalize_history_limit(app_in.get("history_limit")),
            )
            store.prune_search_action_history(_normalize_history_limit(app_in.get("history_limit")))
            _prune_media_cache_for_config(_get_config(), force_unreferenced=True)
            seen_keys: set[tuple[str, int]] = set()

            for row in inst_in:
                if not isinstance(row, dict):
                    continue
                app_name = str(row.get("app") or "").strip().lower()
                try:
                    iid = int(row.get("instance_id") or 0)
                except (TypeError, ValueError):
                    iid = 0
                if app_name not in ("radarr", "sonarr") or iid <= 0:
                    continue
                key = (app_name, iid)
                if key in seen_keys:
                    continue
                seen_keys.add(key)

                instance_name = _normalize_instance_name(row.get("instance_name"), app_name, iid)
                arr_url = _normalize_arr_url(row.get("arr_url"), app_name, iid)
                quiet_hours_enabled = _normalize_quiet_hours_enabled(row.get("quiet_hours_enabled"))
                quiet_hours_start = _normalize_hhmm_or_empty(row.get("quiet_hours_start"), "sleep start", app_name, iid)
                quiet_hours_end = _normalize_hhmm_or_empty(row.get("quiet_hours_end"), "sleep end", app_name, iid)
                values = {
                    "instance_name": instance_name,
                    "enabled": 1 if bool(row.get("enabled", True)) else 0,
                    "interval_minutes": max(15, min(60, int(row.get("interval_minutes") or 15))),
                    "search_missing": 1 if bool(row.get("search_missing", True)) else 0,
                    "search_cutoff_unmet": 1 if bool(row.get("search_cutoff_unmet", True)) else 0,
                    "upgrade_scope": _normalize_upgrade_scope(row.get("upgrade_scope") or "wanted"),
                    "search_order": _normalize_search_order(row.get("search_order") or "smart"),
                    "quiet_hours_enabled": 1 if quiet_hours_enabled else 0,
                    "quiet_hours_start": quiet_hours_start,
                    "quiet_hours_end": quiet_hours_end,
                    "min_hours_after_release": max(0, int(row.get("min_hours_after_release") or 0)),
                    "min_seconds_between_actions": max(0, int(row.get("min_seconds_between_actions") or 0)),
                    "max_missing_actions_per_instance_per_sync": max(
                        0, int(row.get("max_missing_actions_per_instance_per_sync") or 0)
                    ),
                    "max_cutoff_actions_per_instance_per_sync": max(
                        0, int(row.get("max_cutoff_actions_per_instance_per_sync") or 0)
                    ),
                    "sonarr_missing_mode": _normalize_sonarr_missing_mode(row.get("sonarr_missing_mode") or "smart"),
                    "item_retry_hours": max(1, int(row.get("item_retry_hours") or 1)),
                    "rate_window_minutes": max(1, int(row.get("rate_window_minutes") or 1)),
                    "rate_cap": max(1, int(row.get("rate_cap") or 1)),
                    "arr_url": arr_url,
                }
                store.upsert_ui_instance_settings(app_name, iid, values)

                api_key = str(row.get("arr_api_key") or "").strip()
                if api_key:
                    store.set_arr_api_key(app_name, iid, api_key)

            _reload_config()
            _prune_media_cache_for_config(_get_config())
            return jsonify({"ok": True})
        except Exception as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/api/media_cache/clear")
    def clear_media_cache() -> Any:
        cfg = _get_config()
        root = media_cache_dir(cfg.app.db_path)
        files_removed = 0
        bytes_removed = 0
        if root.exists():
            for path in root.iterdir():
                if not path.is_file():
                    continue
                try:
                    stat = path.stat()
                    path.unlink()
                    files_removed += 1
                    bytes_removed += int(stat.st_size)
                except OSError:
                    continue
        rows_updated = store.clear_local_search_action_cover_urls()
        _schedule_search_action_media_backfill("cache clear", min_interval_seconds=0.0)
        return jsonify(
            {
                "ok": True,
                "files_removed": files_removed,
                "bytes_removed": bytes_removed,
                "rows_updated": rows_updated,
                "media_cache": media_cache_stats(cfg.app.db_path),
            }
        )

    @app.post("/api/run")
    def run_now() -> Any:
        payload = request.get_json(silent=True) or {}
        force = bool(payload.get("force", False))
        # Use the same async runner as autorun.
        if not _start_run_async(force=force):
            return jsonify({"error": "Run already in progress"}), 409
        return jsonify({"message": "Run started", "force": force}), 202

    @app.post("/api/run_instance")
    def run_instance() -> Any:
        payload = request.get_json(silent=True) or {}
        app_type = str(payload.get("app") or "").strip().lower()
        instance_id = int(payload.get("instance_id") or 0)
        force = bool(payload.get("force", True))
        if app_type not in ("radarr", "sonarr") or instance_id <= 0:
            return jsonify({"error": "Invalid instance"}), 400

        if not run_lock.acquire(blocking=False):
            return jsonify({"error": "Run already in progress"}), 409

        def runner() -> None:
            try:
                engine.run_instance(app_type=app_type, instance_id=instance_id, force=force, progress_cb=_progress_cb)
            except ArrRequestError as exc:
                logger.error("Run failed: %s", exc)
                with run_state_lock:
                    run_state["running"] = False
                    run_state["error"] = str(exc)
            except Exception as exc:
                if os.getenv("SEEKARR_DEBUG", "0").strip().lower() in ("1", "true", "yes", "on"):
                    logger.exception("Run failed: %s", exc)
                else:
                    logger.error("Run failed: %s", exc)
                with run_state_lock:
                    run_state["running"] = False
                    run_state["error"] = str(exc)
            finally:
                run_lock.release()

        threading.Thread(target=runner, name="webui-run-instance", daemon=True).start()
        return jsonify({"message": f"Instance run started: {app_type}:{instance_id}", "force": force}), 202

    @app.get("/api/item_meta")
    def recent_action_item_meta() -> Any:
        app_type = str(request.args.get("app") or "").strip().lower()
        item_key = str(request.args.get("item_key") or "").strip()
        try:
            instance_id = int(request.args.get("instance_id") or 0)
        except (TypeError, ValueError):
            instance_id = 0
        if app_type not in ("radarr", "sonarr") or instance_id <= 0 or not item_key:
            return jsonify({"error": "Invalid item"}), 404
        meta = _resolve_item_meta(_get_config(), app_type, instance_id, item_key)
        if not meta.get("item_url") and not meta.get("cover_url"):
            return jsonify({"error": "Item link unavailable"}), 404
        return jsonify(meta)

    @app.get("/favicon.ico")
    def favicon() -> Any:
        icon = _asset_path("seekarr-logo.svg")
        if icon.exists():
            return send_file(icon, mimetype="image/svg+xml")
        return "", 204

    @app.get("/media_cache/<path:filename>")
    def media_cache_file(filename: str) -> Any:
        name = str(filename or "").strip()
        if not re.fullmatch(r"[a-f0-9]{64}\.(jpg|png|webp|gif)", name):
            return jsonify({"error": "Media not found"}), 404
        media_file = media_cache_dir(_get_config().app.db_path) / name
        if not media_file.exists() or not media_file.is_file():
            return jsonify({"error": "Media not found"}), 404
        mimetype = {
            ".jpg": "image/jpeg",
            ".png": "image/png",
            ".webp": "image/webp",
            ".gif": "image/gif",
        }.get(media_file.suffix.lower(), "application/octet-stream")
        try:
            return send_file(media_file, mimetype=mimetype, max_age=7 * 86400, conditional=True, etag=True)
        except FileNotFoundError:
            return jsonify({"error": "Media not found"}), 404
        except OSError:
            return jsonify({"error": "Media not found"}), 404

    @app.get("/assets/banner.svg")
    def asset_banner() -> Any:
        banner = _asset_path("seekarr-banner.svg")
        if not banner.exists():
            return jsonify({"error": "Banner asset not found"}), 404
        return send_file(banner, mimetype="image/svg+xml")

    @app.get("/assets/logo.svg")
    def asset_logo() -> Any:
        logo = _asset_path("seekarr-logo.svg")
        if not logo.exists():
            return jsonify({"error": "Logo asset not found"}), 404
        return send_file(logo, mimetype="image/svg+xml")

    @app.get("/assets/sidebar-brand.svg")
    def asset_sidebar_brand() -> Any:
        brand = _asset_path("seekarr-sidebar-brand.svg")
        if not brand.exists():
            return jsonify({"error": "Sidebar brand asset not found"}), 404
        return send_file(brand, mimetype="image/svg+xml")

    @app.get("/assets/<path:filename>")
    def asset_webui_file(filename: str) -> Any:
        name = str(filename or "").strip()
        if name not in ui_asset_names:
            return jsonify({"error": "UI asset not found"}), 404
        asset = _ui_path(name)
        if not asset.exists():
            return jsonify({"error": "UI asset not found"}), 404
        if name.endswith(".css"):
            return send_file(asset, mimetype="text/css")
        return send_file(asset, mimetype="text/javascript")

    return app


def main() -> int:
    parser = argparse.ArgumentParser(description="Seekarr Web UI")
    parser.add_argument("--db-path", help="Path to the Seekarr SQLite database.")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8788, help="Bind port (default: 8788)")
    args = parser.parse_args()

    app = create_app(args.db_path)
    # Production default: use waitress (WSGI server). This avoids Flask's dev server warnings
    # and behaves more like a real deployment on Windows/Linux.
    from waitress import serve

    serve(app, host=args.host, port=args.port, threads=8)
    return 0
