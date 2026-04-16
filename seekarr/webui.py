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

from flask import Flask, jsonify, request, send_file

from .arr import ArrRequestError
from .config import ArrConfig, ArrSyncInstanceConfig, RuntimeConfig, load_config
from .engine import Engine, _quiet_hours_end_utc
from .logging_utils import setup_logging
from .state import StateStore


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


def _config_view(config: RuntimeConfig, store: StateStore) -> dict[str, Any]:
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


def create_app(config_path: str) -> Flask:
    config_path = str(Path(config_path).resolve())
    project_dir = Path(__file__).resolve().parent.parent
    base_config = load_config(config_path)
    setup_logging(base_config.app.log_level)
    logger = logging.getLogger("seekarr.webui")
    wz = logging.getLogger("werkzeug")
    wz.addFilter(_QuietAccessFilter())
    store = StateStore(base_config.app.db_path)

    def _find_base_instance(cfg: RuntimeConfig, app_type: str, instance_id: int) -> ArrSyncInstanceConfig | None:
        instances = cfg.radarr_instances if app_type == "radarr" else cfg.sonarr_instances
        for inst in instances:
            if int(inst.instance_id) == int(instance_id):
                return inst
        return None

    def _materialize_db_instance(
        cfg: RuntimeConfig,
        app_type: str,
        instance_id: int,
        row: dict[str, Any],
        base_inst: ArrSyncInstanceConfig | None = None,
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

        base_arr = base_inst.arr if base_inst is not None else ArrConfig(enabled=True, url="", api_key="")
        enabled = _to_bool(row.get("enabled"), base_inst.enabled if base_inst is not None else True)
        interval_minutes = max(
            15, min(60, _to_int(row.get("interval_minutes"), base_inst.interval_minutes if base_inst else 15))
        )
        instance_name = str(row.get("instance_name") or (base_inst.instance_name if base_inst else "")).strip()
        return ArrSyncInstanceConfig(
            instance_id=max(1, int(instance_id)),
            instance_name=instance_name or _default_instance_name(app_type, instance_id),
            enabled=enabled,
            interval_minutes=interval_minutes,
            search_missing=_to_bool(row.get("search_missing"), base_inst.search_missing if base_inst else True),
            search_cutoff_unmet=_to_bool(
                row.get("search_cutoff_unmet"), base_inst.search_cutoff_unmet if base_inst else True
            ),
            upgrade_scope=_normalize_upgrade_scope(
                row.get("upgrade_scope")
                if row.get("upgrade_scope") is not None
                else (base_inst.upgrade_scope if base_inst else "wanted")
            ),
            search_order=_normalize_search_order(
                row.get("search_order")
                if row.get("search_order") is not None
                else (base_inst.search_order if base_inst else "smart")
            ),
            quiet_hours_start=str(
                row.get("quiet_hours_start")
                if row.get("quiet_hours_start") is not None
                else (
                    base_inst.quiet_hours_start
                    if base_inst and base_inst.quiet_hours_start is not None
                    else cfg.app.quiet_hours_start
                )
            ).strip(),
            quiet_hours_end=str(
                row.get("quiet_hours_end")
                if row.get("quiet_hours_end") is not None
                else (
                    base_inst.quiet_hours_end
                    if base_inst and base_inst.quiet_hours_end is not None
                    else cfg.app.quiet_hours_end
                )
            ).strip(),
            min_hours_after_release=_to_int(
                row.get("min_hours_after_release"),
                base_inst.min_hours_after_release
                if base_inst and base_inst.min_hours_after_release is not None
                else cfg.app.min_hours_after_release,
            ),
            min_seconds_between_actions=_to_int(
                row.get("min_seconds_between_actions"),
                base_inst.min_seconds_between_actions
                if base_inst and base_inst.min_seconds_between_actions is not None
                else cfg.app.min_seconds_between_actions,
            ),
            max_missing_actions_per_instance_per_sync=_to_int(
                row.get("max_missing_actions_per_instance_per_sync"),
                base_inst.max_missing_actions_per_instance_per_sync
                if base_inst and base_inst.max_missing_actions_per_instance_per_sync is not None
                else cfg.app.max_missing_actions_per_instance_per_sync,
            ),
            max_cutoff_actions_per_instance_per_sync=_to_int(
                row.get("max_cutoff_actions_per_instance_per_sync"),
                base_inst.max_cutoff_actions_per_instance_per_sync
                if base_inst and base_inst.max_cutoff_actions_per_instance_per_sync is not None
                else cfg.app.max_cutoff_actions_per_instance_per_sync,
            ),
            sonarr_missing_mode=_normalize_sonarr_missing_mode(
                row.get("sonarr_missing_mode")
                if row.get("sonarr_missing_mode") is not None
                else (base_inst.sonarr_missing_mode if base_inst else "smart")
            ),
            item_retry_hours=_to_int(
                row.get("item_retry_hours"),
                base_inst.item_retry_hours
                if base_inst and base_inst.item_retry_hours is not None
                else cfg.app.item_retry_hours,
            ),
            rate_window_minutes=_to_int(
                row.get("rate_window_minutes"),
                base_inst.rate_window_minutes
                if base_inst and base_inst.rate_window_minutes is not None
                else cfg.app.rate_window_minutes,
            ),
            rate_cap=_to_int(
                row.get("rate_cap"),
                base_inst.rate_cap if base_inst and base_inst.rate_cap is not None else cfg.app.rate_cap_per_instance,
            ),
            arr=ArrConfig(
                enabled=enabled,
                url=str(row.get("arr_url") or base_arr.url or "").strip(),
                api_key=str(base_arr.api_key or "").strip(),
            ),
        )

    def _bootstrap_ui_settings_from_yaml(cfg: RuntimeConfig) -> None:
        """
        One-time migration path:
        - Seed DB-backed UI settings from YAML-configured values when missing.
        - Never overwrite existing DB values.
        """
        existing = store.get_all_ui_instance_settings()
        app_existing = store.get_ui_app_settings()
        if not str(app_existing.get("quiet_hours_timezone") or "").strip():
            store.set_ui_app_settings(quiet_hours_timezone=str(cfg.app.quiet_hours_timezone or "").strip())

        def _seed_instance(app_type: str, inst: Any) -> None:
            key = (app_type, int(inst.instance_id))
            if key not in existing:
                store.upsert_ui_instance_settings(
                    app_type,
                    int(inst.instance_id),
                    {
                        "instance_name": str(
                            inst.instance_name or _default_instance_name(app_type, int(inst.instance_id))
                        ).strip(),
                        "enabled": 1 if bool(inst.enabled) else 0,
                        "interval_minutes": int(inst.interval_minutes),
                        "search_missing": 1 if bool(inst.search_missing) else 0,
                        "search_cutoff_unmet": 1 if bool(inst.search_cutoff_unmet) else 0,
                        "upgrade_scope": str(getattr(inst, "upgrade_scope", "wanted") or "wanted").strip().lower(),
                        "search_order": str(inst.search_order or "smart").strip().lower(),
                        "quiet_hours_start": str(inst.quiet_hours_start or "").strip(),
                        "quiet_hours_end": str(inst.quiet_hours_end or "").strip(),
                        "min_hours_after_release": (
                            int(inst.min_hours_after_release) if inst.min_hours_after_release is not None else None
                        ),
                        "min_seconds_between_actions": (
                            int(inst.min_seconds_between_actions)
                            if inst.min_seconds_between_actions is not None
                            else None
                        ),
                        "max_missing_actions_per_instance_per_sync": (
                            int(inst.max_missing_actions_per_instance_per_sync)
                            if inst.max_missing_actions_per_instance_per_sync is not None
                            else None
                        ),
                        "max_cutoff_actions_per_instance_per_sync": (
                            int(inst.max_cutoff_actions_per_instance_per_sync)
                            if inst.max_cutoff_actions_per_instance_per_sync is not None
                            else None
                        ),
                        "sonarr_missing_mode": str(inst.sonarr_missing_mode or "smart").strip().lower(),
                        "item_retry_hours": int(inst.item_retry_hours) if inst.item_retry_hours is not None else None,
                        "rate_window_minutes": (
                            int(inst.rate_window_minutes) if inst.rate_window_minutes is not None else None
                        ),
                        "rate_cap": int(inst.rate_cap) if inst.rate_cap is not None else None,
                        "arr_url": str(inst.arr.url or "").strip(),
                    },
                )
            if (not store.has_arr_api_key(app_type, int(inst.instance_id))) and str(inst.arr.api_key or "").strip():
                store.set_arr_api_key(app_type, int(inst.instance_id), str(inst.arr.api_key).strip())

        for inst in cfg.radarr_instances:
            _seed_instance("radarr", inst)
        for inst in cfg.sonarr_instances:
            _seed_instance("sonarr", inst)

    _bootstrap_ui_settings_from_yaml(base_config)

    def _with_ui_overrides(cfg: RuntimeConfig) -> RuntimeConfig:
        app_overrides = store.get_ui_app_settings()
        qtz = str(app_overrides.get("quiet_hours_timezone") or "").strip()
        app_cfg = replace(cfg.app, quiet_hours_timezone=qtz or cfg.app.quiet_hours_timezone)

        raw_overrides = store.get_all_ui_instance_settings()
        if raw_overrides:
            radarr_instances = [
                _materialize_db_instance(
                    cfg, "radarr", instance_id, row, _find_base_instance(cfg, "radarr", instance_id)
                )
                for (app_type, instance_id), row in sorted(raw_overrides.items())
                if app_type == "radarr"
            ]
            sonarr_instances = [
                _materialize_db_instance(
                    cfg, "sonarr", instance_id, row, _find_base_instance(cfg, "sonarr", instance_id)
                )
                for (app_type, instance_id), row in sorted(raw_overrides.items())
                if app_type == "sonarr"
            ]
        else:
            radarr_instances = list(cfg.radarr_instances)
            sonarr_instances = list(cfg.sonarr_instances)
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
        "autorun_enabled": True,
        "autorun_last_check": None,
        "autorun_last_run_started": None,
        "active_app_type": None,
        "active_instance_id": None,
        "active_instance_name": None,
    }
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
    asset_cache_key = current_version

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
    assets_dir = Path(__file__).resolve().parent / "assets"
    project_assets_dir = project_dir

    def _asset_path(name: str) -> Path:
        bundled = assets_dir / name
        if bundled.exists():
            return bundled
        fallback = project_assets_dir / name
        return fallback

    password_hash = store.get_webui_password_hash()
    env_pw = str(os.getenv("SEEKARR_WEBUI_PASSWORD", "") or "").strip()
    if not password_hash and env_pw:
        password_hash = _hash_password(env_pw)
        store.set_webui_password_hash(password_hash)

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

    def _get_config() -> RuntimeConfig:
        with config_lock:
            return config

    def _reload_config() -> None:
        nonlocal config
        new_base = load_config(config_path)
        if Path(new_base.app.db_path).resolve() != Path(base_config.app.db_path).resolve():
            raise ValueError("Changing app.db_path requires a restart.")
        new_config = _with_ui_overrides(new_base)
        with config_lock:
            config = new_config
            engine.config = new_config
        _ensure_autorun_threads(new_config)

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

    def _sleep_until(iso: str | None, max_seconds: float = 300.0) -> None:
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
        now = datetime.now(timezone.utc)
        seconds = max(0.0, (dt.astimezone(timezone.utc) - now).total_seconds())
        time.sleep(min(seconds, max_seconds))

    def _autorun_instance_loop(app_type: str, instance_id: int) -> None:
        # Independent per-instance scheduling (no fixed ticker).
        while True:
            try:
                store.set_scheduler_heartbeat()
                with run_state_lock:
                    enabled = bool(run_state.get("autorun_enabled", True))
                    run_state["autorun_last_check"] = datetime.now(timezone.utc).isoformat()
                if not enabled:
                    time.sleep(1.0)
                    continue

                inst = engine._find_instance(app_type, instance_id)
                if not inst or not inst.enabled or not inst.arr.enabled:
                    time.sleep(5.0)
                    continue

                # Quiet-hours pre-check: schedule directly to quiet end so the dashboard and
                # autorun loop both enter sleep mode immediately without an unnecessary due run.
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
                    with run_state_lock:
                        run_state["autorun_last_run_started"] = datetime.now(timezone.utc).isoformat()
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

    @app.get("/")
    def index() -> str:
        return """
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Seekarr</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
  <link rel="stylesheet" href="/assets/webui.css?v=__ASSET_CACHE_KEY__"/>
</head>
<body class="auth-locked">
  <div class="modal auth-splash" id="auth-modal">
    <div class="modal-card auth-splash-card">
      <div class="auth-splash-brand">
<img src="/assets/banner.svg" alt="Seekarr banner"/>
      </div>
      <div class="auth-panel">
        <div class="modal-row">
          <div class="modal-title" id="auth-title">Authentication</div>
        </div>
        <div class="modal-body">
          <div class="subline" id="auth-sub" style="margin-bottom:10px;">Enter your Web UI password.</div>
          <div class="field">
            <div class="label" id="auth-label">Password</div>
            <input class="cfg mono" id="auth-password" name="seekarr_webui_password" type="password" value=""
                   autocomplete="off" autocapitalize="none" autocorrect="off" spellcheck="false" />
            <div class="subline" id="auth-hint" style="margin-top:6px;"></div>
          </div>
          <div class="subline" id="auth-error" style="margin-top:10px; color: rgba(254, 202, 202, 0.98);"></div>
        </div>
        <div class="modal-actions">
          <button class="btn-primary" id="auth-submit">CONTINUE</button>
        </div>
      </div>
    </div>
  </div>
  <div class="modal" id="delete-instance-modal">
    <div class="modal-card">
      <div class="modal-row">
        <div class="modal-title">Remove Instance</div>
      </div>
      <div class="modal-body">
        <div id="delete-instance-sub" style="font-size:15px; line-height:1.45; color:var(--text-secondary);"></div>
        <div
          id="delete-instance-warning"
          style="display:none; margin-top:12px; padding:12px; border-radius:10px; border:1px solid rgba(245, 158, 11, 0.22); background:rgba(245, 158, 11, 0.08); color:rgba(253, 230, 138, 0.96); font-size:13px; line-height:1.45;"
        ></div>
        <div class="subline" style="margin-top:12px; color:rgba(254, 202, 202, 0.92);">
          This removes the instance configuration, stored API key, schedule state, and instance-specific history.
        </div>
        <div class="field" style="margin-top:16px;">
          <div class="label">Current Web UI Password</div>
          <input
            class="cfg mono"
            id="delete-instance-password"
            name="seekarr_delete_password"
            type="password"
            value=""
            autocomplete="current-password"
            autocapitalize="none"
            autocorrect="off"
            spellcheck="false"
          />
        </div>
        <div class="subline" id="delete-instance-error" style="margin-top:10px; color: rgba(254, 202, 202, 0.98);"></div>
      </div>
      <div class="modal-actions">
        <button class="btn-secondary" id="delete-instance-cancel" type="button">CANCEL</button>
        <button class="btn-danger" id="delete-instance-submit" type="button">REMOVE INSTANCE</button>
      </div>
    </div>
  </div>

  <div class="app">
    <aside class="sidebar">
      <div class="brand">
        <img src="/assets/sidebar-brand.svg" alt="Seekarr search automation" class="sidebar-brand-image"/>
      </div>
      <nav class="sidebar-nav" aria-label="Primary">
      <a class="nav-item nav-control active" data-section="dashboard" href="#">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="9"></rect><rect x="14" y="3" width="7" height="5"></rect><rect x="14" y="12" width="7" height="9"></rect><rect x="3" y="16" width="7" height="5"></rect></svg>
        Dashboard
      </a>

      <a class="nav-item nav-control" data-section="runs" href="#">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"></circle><polyline points="12 6 12 12 16 14"></polyline></svg>
        History
      </a>
      <a class="nav-item nav-control" data-section="settings" href="#">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"></circle><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"></path></svg>
        Configuration
      </a>
      </nav>
      <div class="sidebar-badges">
        <a class="sidebar-badge" href="https://github.com/tumeden/seekarr" target="_blank" rel="noopener noreferrer">GitHub</a>
        <a class="sidebar-badge" href="https://hub.docker.com/r/tumeden/seekarr" target="_blank" rel="noopener noreferrer">Docker Hub</a>
        <a class="sidebar-badge sidebar-badge-support" href="https://ko-fi.com/tumeden" target="_blank" rel="noopener noreferrer">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M7 6h9a1 1 0 0 1 1 1v7a4 4 0 0 1-4 4h-3a4 4 0 0 1-4-4V7a1 1 0 0 1 1-1z"></path><path d="M17 8h1.5a2.5 2.5 0 0 1 0 5H17"></path><path d="M8 3c0 1 .7 1.6 1.4 2.2.7.6 1.4 1.2 1.4 2.3"></path><path d="M12 3c0 1 .7 1.6 1.4 2.2.7.6 1.4 1.2 1.4 2.3"></path></svg>
          Donate a coffee
        </a>
      </div>
    </aside>
    <main class="main">
      <header class="topbar">
        <div class="topbar-copy">
          <div class="topbar-title-wrap">
            <h1 id="topbar-title">Dashboard</h1>
            <p id="topbar-subtitle">Overview, schedules, and recent search activity.</p>
          </div>
        </div>
        <div class="topbar-actions">
          <span class="topbar-message" id="msg"></span>
          <div class="actions dashboard-actions">
            <label class="chip toggle-chip">
              <input id="autorun-toggle" type="checkbox" checked />
              Auto-run
            </label>
          </div>
          <span class="topbar-badge" id="version-chip">Version --</span>
          <a class="topbar-badge update" id="update-chip" href="https://github.com/tumeden/seekarr/releases/latest"
             target="_blank" rel="noopener noreferrer" style="display:none;">Update available</a>
        </div>
      </header>
      <div class="content-canvas">
      <div class="mobile-nav" aria-label="Sections">
        <button class="mobile-nav-item nav-control active" data-section="dashboard" type="button">Dashboard</button>
        <button class="mobile-nav-item nav-control" data-section="runs" type="button">History</button>
        <button class="mobile-nav-item nav-control" data-section="settings" type="button">Configuration</button>
      </div>
      
      <section class="content-section active" id="section-dashboard">
        <div class="dashboard-header">
          <div class="dashboard-brand">
            <img src="/assets/logo.svg" alt="Seekarr logo" class="dashboard-brand-logo"/>
            <div class="dashboard-brand-copy">
              <h1>Seekarr</h1>
              <div class="dashboard-brand-tagline">Missing + upgrade search automation</div>
            </div>
          </div>
        </div>

        <div class="cards-grid" id="instance-cards"></div>

        <div class="card">
          <div class="section-head">
            <h3>Recent Actions</h3>
            <div class="subline">Across all configured instances</div>
          </div>
          <div id="recent-actions" class="mono">-</div>
        </div>
      </section>



      <section class="content-section" id="section-runs">
        <div class="card">
          <h3>Search History (Per Instance)</h3>
          <div id="runs-wrap"></div>
        </div>
      </section>

      <section class="content-section" id="section-settings">
        <div class="settings-head">
          <div class="settings-tabs" id="settings-tabs"></div>
          <div class="settings-actions">
            <button class="btn-secondary" id="add-radarr-instance" type="button">
            + Add Radarr Instance
            </button>
            <button class="btn-secondary" id="add-sonarr-instance" type="button">
            + Add Sonarr Instance
            </button>
          </div>
        </div>
        
        <div id="settings-content-wrapper">
          <div class="card settings-tab-content settings-global-card" id="settings-tab-global">
            <div class="settings-global-head">
              <svg class="settings-global-icon" viewBox="0 0 24 24" fill="none" stroke="var(--accent-color)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"></circle><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"></path></svg>
              <h3>Global Configuration</h3>
            </div>
            <div class="subline settings-global-copy">App-wide settings affecting all instances.</div>
            
            <div class="field settings-global-field">
              <div class="label">Quiet Hours Timezone</div>
              <input id="settings-quiet-timezone" class="cfg mono" type="text" list="timezone-options"
                     placeholder="Search timezone (example: America/New_York)"/>
              <datalist id="timezone-options"></datalist>
            </div>
            <div class="subline settings-global-help">
              Used for quiet start/end evaluation. Leave empty to use server/container local timezone.
            </div>
          </div>
          
          <div id="settings-instance-cards"></div>
        </div>
      </section>
      </div>
    </main>
  </div>
  <div class="settings-save-fab" id="settings-save-fab">
    <span class="subline" id="settings-msg"></span>
    <button class="btn-primary save-button" id="save-settings" type="button">SAVE CONFIGURATION</button>
  </div>
  <div class="toast-stack" id="toast-stack"></div>
  <script>
    let authHeader = '';
    let passwordIsSet = false;
    let authInFlight = false;
    let authMode = '';
    let timersStarted = false;
    let timezoneOptionsLoaded = false;
    let activeTimeZone = '';
    let refreshTimer = null;
    let countdownTimer = null;
    let settingsBaseline = '';
    let settingsDirty = false;
    let settingsStatusMessage = '';
    let deleteInstanceTarget = null;
    let toastSeq = 0;
    const authStorageKey = 'seekarr_auth_header';
    const timezoneFallback = [
      'UTC', 'Etc/UTC',
      'America/New_York', 'America/Chicago', 'America/Denver', 'America/Los_Angeles', 'America/Phoenix',
      'America/Anchorage', 'Pacific/Honolulu',
      'Europe/London', 'Europe/Paris', 'Europe/Berlin',
      'Asia/Tokyo', 'Asia/Seoul', 'Asia/Kolkata', 'Asia/Singapore', 'Asia/Shanghai',
      'Australia/Sydney', 'Australia/Perth'
    ];
    function populateTimezoneOptions() {
      if (timezoneOptionsLoaded) return;
      timezoneOptionsLoaded = true;
      const dl = document.getElementById('timezone-options');
      if (!dl) return;
      let zones = [];
      try {
        if (Intl && typeof Intl.supportedValuesOf === 'function') {
          zones = Intl.supportedValuesOf('timeZone') || [];
        }
      } catch (e) {}
      if (!zones.length) zones = timezoneFallback.slice();
      zones = Array.from(new Set([...zones, ...timezoneFallback])).sort((a, b) => a.localeCompare(b));
      const frag = document.createDocumentFragment();
      for (const z of zones) {
        const o = document.createElement('option');
        o.value = z;
        frag.appendChild(o);
      }
      dl.appendChild(frag);
    }
    function startTimers() {
      if (timersStarted) return;
      timersStarted = true;
      refreshTimer = setInterval(refresh, 5000);
      countdownTimer = setInterval(tickCountdowns, 1000);
    }

    function loadAuthHeader() {
      try {
        const v = localStorage.getItem(authStorageKey);
        authHeader = (v && typeof v === 'string') ? v : '';
      } catch (e) {
        authHeader = '';
      }
    }

    function saveAuthHeader() {
      try {
        if (authHeader) localStorage.setItem(authStorageKey, authHeader);
      } catch (e) {}
    }

    function clearAuthHeader() {
      authHeader = '';
      try {
        localStorage.removeItem(authStorageKey);
      } catch (e) {}
    }

    function apiFetch(url, opts) {
      const o = opts ? Object.assign({}, opts) : {};
      o.headers = o.headers ? Object.assign({}, o.headers) : {};
      if (authHeader) o.headers['Authorization'] = authHeader;
      if (!('cache' in o)) o.cache = 'no-store';
      return fetch(url, o);
    }

    function showAuthModal(mode) {
      const modal = document.getElementById('auth-modal');
      const title = document.getElementById('auth-title');
      const sub = document.getElementById('auth-sub');
      const label = document.getElementById('auth-label');
      const hint = document.getElementById('auth-hint');
      const err = document.getElementById('auth-error');
      const pw = document.getElementById('auth-password');
      const btn = document.getElementById('auth-submit');

      err.textContent = '';
      btn.disabled = false;

      const isShown = modal.classList.contains('show');
      const modeChanged = (authMode !== mode);
      authMode = mode;
      if (!isShown || modeChanged) {
        pw.value = '';
      }

      if (mode === 'set') {
        title.textContent = 'Set Web UI Password';
        sub.textContent = 'First run: set a password to protect Seekarr.';
        label.textContent = 'New Password';
        pw.setAttribute('autocomplete', 'new-password');
        hint.textContent = 'Minimum 8 characters. Saved as a salted hash in the SQLite DB.';
      } else {
        title.textContent = 'Unlock Seekarr';
        sub.textContent = 'Enter your Web UI password to continue.';
        label.textContent = 'Password';
        pw.setAttribute('autocomplete', 'current-password');
        hint.textContent = '';
      }

      document.body.classList.add('auth-locked');
      modal.classList.add('show');
      setTimeout(() => pw.focus(), 50);
    }

    function hideAuthModal() {
      document.getElementById('auth-modal').classList.remove('show');
      document.body.classList.remove('auth-locked');
    }

    function showDeleteInstanceModal(target) {
      deleteInstanceTarget = target || null;
      const modal = document.getElementById('delete-instance-modal');
      const sub = document.getElementById('delete-instance-sub');
      const warning = document.getElementById('delete-instance-warning');
      const err = document.getElementById('delete-instance-error');
      const pw = document.getElementById('delete-instance-password');
      const btn = document.getElementById('delete-instance-submit');
      const appLabel = String(target?.app || '').toUpperCase();
      const instanceLabel = String(target?.instanceName || `#${target?.instanceId || ''}`).trim();

      sub.textContent = `Enter your Web UI password to remove ${appLabel} instance "${instanceLabel}".`;
      if (target?.discardUnsaved) {
        warning.style.display = 'block';
        warning.textContent = 'You have unsaved configuration changes. Removing this instance will discard them.';
      } else {
        warning.style.display = 'none';
        warning.textContent = '';
      }
      err.textContent = '';
      pw.value = '';
      btn.disabled = false;
      modal.classList.add('show');
      setTimeout(() => pw.focus(), 50);
    }

    function hideDeleteInstanceModal() {
      document.getElementById('delete-instance-modal').classList.remove('show');
      document.getElementById('delete-instance-error').textContent = '';
      document.getElementById('delete-instance-password').value = '';
      document.getElementById('delete-instance-submit').disabled = false;
      deleteInstanceTarget = null;
    }

    async function submitDeleteInstance() {
      if (!deleteInstanceTarget) return;
      const msg = document.getElementById('settings-msg');
      const err = document.getElementById('delete-instance-error');
      const pw = document.getElementById('delete-instance-password');
      const btn = document.getElementById('delete-instance-submit');
      const confirmPassword = String(pw.value || '');
      err.textContent = '';
      btn.disabled = true;

      try {
        const r = await apiFetch('/api/instances/delete', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            app: deleteInstanceTarget.app,
            instance_id: deleteInstanceTarget.instanceId,
            confirm_password: confirmPassword,
          }),
        });
        const data = await r.json().catch(() => ({}));
        if (!r.ok) {
          err.textContent = data.error || 'Remove failed';
          btn.disabled = false;
          return;
        }
        hideDeleteInstanceModal();
        window.settingsActiveTab = 'global';
        msg.textContent = 'Instance removed';
        settingsStatusMessage = 'Instance removed';
        syncSettingsSaveFab();
        await loadSettings();
        await refresh();
        showToast('Instance Removed', 'Returned to Global Settings.');
      } catch (e) {
        err.textContent = 'Remove failed';
        btn.disabled = false;
      }
    }

    async function ensureAuth() {
      const modal = document.getElementById('auth-modal');
      if (modal.classList.contains('show')) return;
      if (authInFlight) return;
      authInFlight = true;
      if (!authHeader) loadAuthHeader();
      if (authHeader) {
        const ok = await apiFetch('/api/status').then(r => r.ok).catch(() => false);
        if (ok) {
          hideAuthModal();
          await refresh();
          startTimers();
          authInFlight = false;
          return;
        }
        clearAuthHeader();
      }
      const st = await fetch('/api/auth/status', { cache: 'no-store' }).then(r => r.json()).catch(() => ({}));
      passwordIsSet = !!st.password_set;
      showAuthModal(passwordIsSet ? 'login' : 'set');
      authInFlight = false;
    }

    async function authSubmit() {
      const btn = document.getElementById('auth-submit');
      const err = document.getElementById('auth-error');
      const pw = String(document.getElementById('auth-password').value || '');
      err.textContent = '';
      btn.disabled = true;

      if (!passwordIsSet) {
        const r = await fetch('/api/auth/bootstrap', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ password: pw }),
        });
        const data = await r.json().catch(() => ({}));
        if (!r.ok) {
          err.textContent = data.error || 'Failed to set password';
          btn.disabled = false;
          return;
        }
        passwordIsSet = true;
      }

      authHeader = 'Basic ' + btoa('seekarr:' + pw);
      const testOk = await apiFetch('/api/status').then(r => r.ok).catch(() => false);
      if (!testOk) {
        err.textContent = 'Invalid password';
        clearAuthHeader();
        btn.disabled = false;
        return;
      }
      saveAuthHeader();

      hideAuthModal();
      await refresh();
      startTimers();
    }

    function asBadge(ok) {
      return ok ? '<span class="badge ok">ON</span>' : '<span class="badge off">OFF</span>';
    }
    function asPill(ok, label, title) {
      const t = title ? ` title="${title}"` : '';
      return ok
        ? `<span class="badge ok"${t}>${label}</span>`
        : `<span class="badge off"${t}>${label}</span>`;
    }
    function safe(v) { return (v === null || v === undefined) ? '' : String(v); }
    function showToast(title, text, tone='success') {
      const stack = document.getElementById('toast-stack');
      if (!stack) return;
      const toast = document.createElement('div');
      const id = `toast-${++toastSeq}`;
      toast.className = `toast ${tone}`;
      toast.id = id;
      toast.innerHTML = `<div class="toast-title">${safe(title)}</div><div class="toast-text">${safe(text)}</div>`;
      stack.appendChild(toast);
      requestAnimationFrame(() => toast.classList.add('show'));
      window.setTimeout(() => {
        toast.classList.remove('show');
        window.setTimeout(() => {
          const el = document.getElementById(id);
          if (el) el.remove();
        }, 180);
      }, 2600);
    }
    function syncSettingsSaveFab() {
      const fab = document.getElementById('settings-save-fab');
      const msg = document.getElementById('settings-msg');
      const btn = document.getElementById('save-settings');
      if (!fab || !msg || !btn) return;
      const show = settingsDirty || btn.disabled;
      fab.classList.toggle('show', show);
      msg.textContent = settingsStatusMessage || (settingsDirty ? 'Unsaved configuration changes' : '');
    }
    function setSettingsDirtyState(dirty, message='') {
      settingsDirty = !!dirty;
      settingsStatusMessage = message;
      syncSettingsSaveFab();
    }
    function buildSettingsPayload() {
      const instances = [];
      document.querySelectorAll('#settings-instance-cards [data-key]').forEach(tr => {
        const key = tr.getAttribute('data-key') || '';
        const parts = key.split(':');
        if (parts.length < 2) return;
        const app = parts[0];
        const instance_id = Number(parts[1] || 0);
        instances.push({
          app,
          instance_id,
          instance_name: String(tr.querySelector('.si_name')?.value || '').trim(),
          enabled: !!tr.querySelector('.si_enabled')?.checked,
          interval_minutes: Number(tr.querySelector('.si_interval')?.value || 0),
          search_missing: !!tr.querySelector('.si_missing')?.checked,
          search_cutoff_unmet: !!tr.querySelector('.si_cutoff')?.checked,
          upgrade_scope: String(tr.querySelector('.si_upgrade_scope')?.value || 'wanted'),
          search_order: String(tr.querySelector('.si_search_order')?.value || 'smart'),
          quiet_hours_start: String(tr.querySelector('.si_quiet_start')?.value || '').trim(),
          quiet_hours_end: String(tr.querySelector('.si_quiet_end')?.value || '').trim(),
          min_hours_after_release: Number(tr.querySelector('.si_after_release')?.value || 0),
          min_seconds_between_actions: Number(tr.querySelector('.si_between')?.value || 0),
          max_missing_actions_per_instance_per_sync: Number(tr.querySelector('.si_missing_per_run')?.value || 0),
          max_cutoff_actions_per_instance_per_sync: Number(tr.querySelector('.si_upgrades_per_run')?.value || 0),
          sonarr_missing_mode: (app === 'sonarr') ? String(tr.querySelector('.si_missing_mode')?.value || 'smart') : undefined,
          item_retry_hours: Number(tr.querySelector('.si_retry')?.value || 0),
          rate_window_minutes: Number(tr.querySelector('.si_rate_window')?.value || 0),
          rate_cap: Number(tr.querySelector('.si_rate_cap')?.value || 0),
          arr_url: String(tr.querySelector('.si_url')?.value || '').trim(),
          arr_api_key: String(tr.querySelector('.si_apikey')?.value || '').trim(),
        });
      });
      instances.sort((a, b) => {
        if (a.app !== b.app) return a.app.localeCompare(b.app);
        return a.instance_id - b.instance_id;
      });
      return {
        app: {
          quiet_hours_timezone: String(document.getElementById('settings-quiet-timezone')?.value || '').trim(),
        },
        instances,
      };
    }
    function settingsPayloadFingerprint(payload) {
      return JSON.stringify(payload || { app: { quiet_hours_timezone: '' }, instances: [] });
    }
    function refreshSettingsDirtyState(message='') {
      const current = settingsPayloadFingerprint(buildSettingsPayload());
      setSettingsDirtyState(current !== settingsBaseline, message);
    }
    function confirmDiscardUnsavedSettings(actionLabel) {
      if (!settingsDirty) return true;
      return confirm(`You have unsaved configuration changes. ${actionLabel} will discard them. Continue?`);
    }
    function getTimeZoneLabel() {
      return activeTimeZone ? activeTimeZone : 'local';
    }
    function fmtTime(iso) {
      if (!iso) return '';
      const t = Date.parse(iso);
      if (!Number.isFinite(t)) return safe(iso);
      const dt = new Date(t);
      const opts = {
        year: 'numeric',
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
        hour12: false,
      };
      if (activeTimeZone) opts.timeZone = activeTimeZone;
      try {
        const parts = new Intl.DateTimeFormat('en-CA', opts).formatToParts(dt);
        const byType = {};
        for (const p of parts) byType[p.type] = p.value;
        return `${byType.year}-${byType.month}-${byType.day} ${byType.hour}:${byType.minute}:${byType.second}`;
      } catch (e) {
        return dt.toLocaleString();
      }
    }
    const sectionMeta = {
      dashboard: {
        title: 'Dashboard',
        subtitle: 'Overview, schedules, and recent search activity.',
      },
      runs: {
        title: 'History',
        subtitle: 'Per-instance search history and recent activity.',
      },
      settings: {
        title: 'Configuration',
        subtitle: 'Global settings, instance controls, and automation behavior.',
      },
    };
    function syncTopbar(name) {
      const meta = sectionMeta[name] || sectionMeta.dashboard;
      const title = document.getElementById('topbar-title');
      const subtitle = document.getElementById('topbar-subtitle');
      if (title) title.textContent = meta.title;
      if (subtitle) subtitle.textContent = meta.subtitle;
    }
    function setSection(name) {
      document.querySelectorAll('.content-section').forEach(s => s.classList.remove('active'));
      document.getElementById(`section-${name}`)?.classList.add('active');
      document.querySelectorAll('.nav-control').forEach(a => a.classList.remove('active'));
      document.querySelectorAll(`.nav-control[data-section="${name}"]`).forEach(a => a.classList.add('active'));
      syncTopbar(name);
    }
    document.querySelectorAll('.nav-control').forEach(a => {
      a.addEventListener('click', (e) => {
        e.preventDefault();
        setSection(a.dataset.section);
      });
    });
    function fmtCountdown(iso) {
      if (!iso) return '';
      const t = Date.parse(iso);
      if (!Number.isFinite(t)) return '';
      const diff = Math.floor((t - Date.now()) / 1000);
      if (diff <= 0) return 'DUE';
      const h = Math.floor(diff / 3600);
      const m = Math.floor((diff % 3600) / 60);
      const s = diff % 60;
      if (h > 0) return `${h}h ${m}m`;
      if (m > 0) return `${m}m ${s}s`;
      return `${s}s`;
    }

    function tickCountdowns() {
      document.querySelectorAll('[data-next-sync]').forEach(el => {
        const iso = el.getAttribute('data-next-sync');
        const cd = fmtCountdown(iso);
        el.textContent = cd;
        if (el.classList.contains('big-countdown')) {
          el.classList.toggle('due', cd === 'DUE');
        }
      });
    }

    async function setAutorun(enabled) {
      try {
        await apiFetch('/api/autorun', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ enabled }),
        });
      } catch (e) {}
    }

    async function forceRunInstance(app, instanceId) {
      const msg = document.getElementById('msg');
      msg.textContent = `Force run started for ${app}:${instanceId}...`;
      const r = await apiFetch('/api/run_instance', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({ app, instance_id: instanceId, force: true })
      });
      const data = await r.json();
      if (!r.ok) {
        msg.textContent = data.error || 'Failed to start run';
        return;
      }

      msg.textContent = (data.message || 'Run started') + ' (waiting for completion...)';

      // Poll briefly so the UI gives immediate feedback even when 0 actions are triggered.
      const key = `${app}:${instanceId}`;
      const startedMs = Date.now();
      for (let i = 0; i < 40; i++) {
        await new Promise(res => setTimeout(res, 500));
        let st;
        try {
          st = await (await apiFetch('/api/status', { cache:'no-store' })).json();
        } catch (e) {
          continue;
        }
        const rs = st.run_state || {};
        const lr = st.instance_last_run ? st.instance_last_run[key] : null;
        const finished = !rs.running;
        if (!finished) continue;
        if (lr && lr.finished_at) {
          const fin = Date.parse(lr.finished_at);
          if (Number.isFinite(fin) && fin >= (startedMs - 2000)) {
            const s = lr.stats || {};
            msg.textContent =
              `${app.toUpperCase()} ${lr.instance_name || ''} finished: ` +
              `wanted ${s.wanted_count ?? '-'}, ` +
              `triggered ${s.actions_triggered ?? '-'}, ` +
              `cooldown ${s.actions_skipped_cooldown ?? '-'}, ` +
              `not-released ${s.actions_skipped_not_released ?? '-'}, ` +
              `rate ${s.actions_skipped_rate_limit ?? '-'}.`;
            break;
          }
        }
      }

      await refresh();
    }

    async function refresh() {
      const r = await apiFetch('/api/status');
      if (r.status === 401) {
        await ensureAuth();
        return;
      }
      const data = await r.json();
      activeTimeZone = String(data?.config?.app?.quiet_hours_timezone || '').trim();
      const ver = data.version || {};
      const versionChip = document.getElementById('version-chip');
      if (versionChip) {
        versionChip.textContent = `Version ${safe(ver.current || '-')}`;
      }
      const updateChip = document.getElementById('update-chip');
      if (updateChip) {
        if (ver.update_available) {
          updateChip.style.display = 'inline-block';
          updateChip.href = String(ver.release_url || 'https://github.com/tumeden/seekarr/releases/latest');
          updateChip.title = ver.latest ? `Latest: ${ver.latest}` : 'Update available';
        } else {
          updateChip.style.display = 'none';
        }
      }
      
      const rs = data.run_state || {};
      const autorunToggle = document.getElementById('autorun-toggle');
      if (autorunToggle && autorunToggle.checked !== !!rs.autorun_enabled) {
        autorunToggle.checked = !!rs.autorun_enabled;
      }
      const hb = data.scheduler_heartbeat || null;
      const hbMs = hb ? Date.parse(hb) : NaN;
      const alive = Number.isFinite(hbMs) && (Date.now() - hbMs) < 120000;
      // We don't surface "scheduler online" as a top-level badge, but we still use it for notes.

      const syncMap = {};
      for (const s of data.sync_status || []) {
        syncMap[`${s.app_type}:${s.instance_id}`] = s;
      }



      const instances = Array.isArray(data.config?.instances) ? data.config.instances : [];
      const cards = document.getElementById('instance-cards');
      cards.setAttribute('data-count', String(instances.length));
      cards.innerHTML = '';
      if (!instances.length) {
        cards.innerHTML = `<div class="card empty-state-card"><div class="section-head"><h3>No Instances Configured</h3><div class="subline">Add a Radarr or Sonarr instance from Configuration.</div></div></div>`;
      }
      for (const i of instances) {
        const key = `${i.app}:${i.instance_id}`;
        const s = syncMap[key] || {};
        const used = Number((data.rate_status?.[key]?.used) ?? 0);
        const cap = Number(i.rate_cap ?? 0);
        const remaining = Math.max(0, cap - used);
        const lr = (data.instance_last_run && data.instance_last_run[key]) ? data.instance_last_run[key] : null;
        const lrs = lr && lr.stats ? lr.stats : {};
        const cd = fmtCountdown(s.next_sync_time);
        const due = (cd === 'DUE');
        const runningThis =
          !!rs.running &&
          rs.active_app_type === i.app &&
          Number(rs.active_instance_id) === Number(i.instance_id);

        let statusText = 'Waiting';
        let statusClass = 'waiting';
        if (!i.enabled) {
          statusText = 'Off';
          statusClass = 'off';
        } else if (runningThis) {
          statusText = 'Running';
          statusClass = 'running';
        } else if (due) {
          statusText = 'Due';
          statusClass = 'due';
        }

        let note = 'Scheduled';
        if (due) {
          if (!alive) note = 'Due, but scheduler is OFF';
          else if (!rs.autorun_enabled) note = 'Due, but auto-run is off';
          else note = 'Due, will run on the next scheduler tick';
        }
        const pct = cap > 0 ? Math.min(100, Math.round((used / cap) * 100)) : 0;
        const barClass = (used >= cap && cap > 0) ? 'bar cap' : 'bar';
        const canForce = !!i.enabled;
        const disabledAttr = (!canForce || !!rs.running) ? 'disabled' : '';
        const runTitle = runningThis ? 'Run in progress' : (canForce ? 'Run now' : 'Run unavailable');
        const statusHtml = statusClass === 'waiting' ? '' : `<span class="status ${statusClass}">${statusText}</span>`;
        const safeUrl = i.arr_url ? safe(i.arr_url) : 'URL not set';
        cards.innerHTML += `
          <div class="instance-card instance-card-shell" data-app="${safe(i.app)}">
            <div>
              <div class="instance-head">
                <div class="instance-main">
                  <div class="instance-eyebrow">
                    <svg class="instance-eyebrow-icon" width="16" height="16" fill="none" stroke="var(--accent-color)" stroke-width="2" viewBox="0 0 24 24"><rect x="2" y="3" width="20" height="14" rx="2" ry="2"></rect><line x1="8" y1="21" x2="16" y2="21"></line><line x1="12" y1="17" x2="12" y2="21"></line></svg>
                    <span>${safe(i.app).toUpperCase()}</span>
                  </div>
                  <div class="instance-title">
                    <span class="instance-name">${safe(i.instance_name)}</span>
                    <span class="instance-id">#${safe(i.instance_id)}</span>
                  </div>
                  <div class="instance-meta">
                    <span class="mono">${safeUrl}</span>
                  </div>
                </div>
                <div class="instance-utility">
                  ${statusHtml}
                  <div class="instance-control-row">
                    <button class="card-icon-btn" onclick="window.settingsActiveTab='${safe(i.app)}:${safe(i.instance_id)}'; setSection('settings'); loadSettings(); return false;" type="button" title="Settings" aria-label="Settings">
                      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="3"></circle><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"></path></svg>
                    </button>
                    <button class="card-icon-btn card-icon-btn-run" data-force-app="${safe(i.app)}" data-force-id="${safe(i.instance_id)}" ${disabledAttr} type="button" title="${runTitle}" aria-label="${runTitle}">
                      <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M5 7.5c0-1.1 1.2-1.77 2.13-1.2l5.74 3.5c.9.55.9 1.85 0 2.4l-5.74 3.5C6.2 16.27 5 15.6 5 14.5v-7z"></path><path d="M12 7.5c0-1.1 1.2-1.77 2.13-1.2l5.74 3.5c.9.55.9 1.85 0 2.4l-5.74 3.5C13.2 16.27 12 15.6 12 14.5v-7z"></path></svg>
                    </button>
                  </div>
                </div>
              </div>
              <div class="countdown-block">
                <div>
                  <div class="big-countdown ${due ? 'due' : ''}" data-next-sync="${safe(s.next_sync_time)}">${cd}</div>
                  <div class="subline mono countdown-meta" title="${safe(s.next_sync_time) || ''}">Next run (${safe(getTimeZoneLabel())}): ${fmtTime(s.next_sync_time) || '-'}</div>
                  <div class="subline countdown-note ${due ? 'warn' : ''}">${note}</div>
                </div>
              </div>
              <div class="rate-panel">
                <div class="subline rate-row">
                  <span>Rate window (${safe(i.rate_window_minutes)}m)</span>
                  <span class="mono rate-value">${used} / ${cap}</span>
                </div>
                <div class="progress progress-slim">
                  <div class="${barClass}" style="width:${pct}%;"></div>
                </div>
              </div>
            </div>
            <div class="instance-metrics">
              <div class="kv metrics-grid">
                <div><div class="k">Wanted</div><div class="v text-strong">${safe(lrs.wanted_count ?? '-')}</div></div>
                <div><div class="k">Triggered</div><div class="v text-success text-strong">${safe(lrs.actions_triggered ?? '-')}</div></div>
                <div><div class="k">Interval</div><div class="v">${safe(i.interval_minutes)}m</div></div>
                <div><div class="k">Retry</div><div class="v">${safe(i.item_retry_hours)}h</div></div>
                <div><div class="k k-nowrap">Last Sync</div><div class="v mono metric-time">${fmtTime(s.last_sync_time) || '-'}</div></div>
                <div><div class="k">Window</div><div class="v">${safe(i.rate_window_minutes)}m</div></div>
              </div>
            </div>
          </div>
        `;
      }

      // Search History: Tabs + Pagination
      const runsWrap = document.getElementById('runs-wrap');
      const sh = data.search_history || {};
      if (!window.historyActiveTab && instances.length > 0) {
        window.historyActiveTab = `${instances[0].app}:${instances[0].instance_id}`;
      }
      if (!window.historyPage) window.historyPage = {};
      
      const PAGE_SIZE = 10;
      
      let tabsHtml = '<div class="history-tabs">';
      instances.forEach(inst => {
        const key = `${inst.app}:${inst.instance_id}`;
        const isActive = (window.historyActiveTab === key);
        tabsHtml += `<button class="tab-btn history-tab ${isActive ? 'active' : ''}" onclick="window.historyActiveTab='${key}'; window.historyPage['${key}']=1; refresh(); return false;">${safe(inst.app).toUpperCase()} - ${safe(inst.instance_name)}</button>`;
      });
      tabsHtml += '</div>';

      let contentHtml = '';
      const activeInst = instances.find(inst => `${inst.app}:${inst.instance_id}` === window.historyActiveTab);
      if (activeInst) {
        const key = window.historyActiveTab;
        const rows = sh[key] || [];
        const totalRows = rows.length;
        const totalPages = Math.ceil(totalRows / PAGE_SIZE) || 1;
        let currentPage = window.historyPage[key] || 1;
        if (currentPage > totalPages) currentPage = totalPages;
        
        const startIdx = (currentPage - 1) * PAGE_SIZE;
        const pageRows = rows.slice(startIdx, startIdx + PAGE_SIZE);
        
        let body = '';
        for (const row of pageRows) {
          body += `<tr>
            <td class="mono time" title="${safe(row.occurred_at) || ''}">${fmtTime(row.occurred_at) || '-'}</td>
            <td class="title">${safe(row.title)}</td>
          </tr>`;
        }
        if (!body) {
          body = `<tr><td colspan="2" class="mono history-empty">No searches recorded yet.</td></tr>`;
        }

        // Pagination controls
        let paginationHtml = '';
        if (totalPages > 1) {
          paginationHtml = `<div class="history-pagination">
            <div class="history-pagination-info">Showing ${startIdx + 1} to ${Math.min(startIdx + PAGE_SIZE, totalRows)} of ${totalRows} entries</div>
            <div class="history-pagination-controls">
              <button class="btn-mini btn-mini-neutral" ${currentPage === 1 ? 'disabled' : ''} onclick="window.historyPage['${key}']=${currentPage-1}; refresh(); return false;">Previous</button>
              <div class="page-status">Page ${currentPage} of ${totalPages}</div>
              <button class="btn-mini btn-mini-neutral" ${currentPage === totalPages ? 'disabled' : ''} onclick="window.historyPage['${key}']=${currentPage+1}; refresh(); return false;">Next</button>
            </div>
          </div>`;
        }

        contentHtml = `
          <div class="card history-card">
            <div class="table-wrap">
              <table class="history">
                <colgroup>
                  <col class="col-time" />
                  <col />
                </colgroup>
                <thead>
                  <tr>
                    <th>Time (${safe(getTimeZoneLabel())})</th><th>Title Searched</th>
                  </tr>
                </thead>
                <tbody>${body}</tbody>
              </table>
            </div>
            ${paginationHtml}
          </div>`;
      }
      runsWrap.innerHTML = tabsHtml + contentHtml;

      const actionsEl = document.getElementById('recent-actions');
      const actions = Array.isArray(data.recent_actions)
        ? data.recent_actions.map(a => ({
            ts: a.occurred_at,
            app_type: a.app_type,
            instance_name: a.instance_name,
            title: a.title,
          }))
        : (Array.isArray(rs.recent_actions) ? rs.recent_actions : []);
      if (!actions.length) {
        actionsEl.textContent = '-';
      } else {
        const lines = actions.map(a => {
          const ts = fmtTime(a.ts) || '--';
          const app = (a.app_type || '').toUpperCase();
          const inst = a.instance_name ? ` (${a.instance_name})` : '';
          const title = a.title || '';
          return `${ts}  ${app}${inst}  ${title}`;
        });
        actionsEl.textContent = lines.join('\\n');
      }
      tickCountdowns();
    }
    document.getElementById('instance-cards').addEventListener('click', (e) => {
      const btn = e.target && e.target.closest ? e.target.closest('button[data-force-app]') : null;
      if (!btn) return;
      if (btn.disabled) return;
      const app = btn.getAttribute('data-force-app');
      const id = Number(btn.getAttribute('data-force-id') || 0);
      if (!app || !id) return;
      forceRunInstance(app, id);
    });
    document.getElementById('autorun-toggle').addEventListener('change', (e) => setAutorun(!!e.target.checked));
    document.getElementById('auth-submit').addEventListener('click', authSubmit);
    document.getElementById('auth-password').addEventListener('keydown', (e) => {
      if (e.key === 'Enter') authSubmit();
    });
    document.getElementById('delete-instance-cancel').addEventListener('click', hideDeleteInstanceModal);
    document.getElementById('delete-instance-submit').addEventListener('click', submitDeleteInstance);
    document.getElementById('delete-instance-password').addEventListener('keydown', (e) => {
      if (e.key === 'Enter') submitDeleteInstance();
      if (e.key === 'Escape') hideDeleteInstanceModal();
    });
    document.getElementById('delete-instance-modal').addEventListener('click', (e) => {
      if (e.target === e.currentTarget) hideDeleteInstanceModal();
    });
    document.getElementById('settings-instance-cards').addEventListener('click', async (e) => {
      const msg = document.getElementById('settings-msg');
      const deleteBtn = e.target && e.target.closest ? e.target.closest('button[data-delete-instance]') : null;
      if (deleteBtn) {
        if (deleteBtn.disabled) return;
        const app = String(deleteBtn.getAttribute('data-app') || '').trim();
        const instanceId = Number(deleteBtn.getAttribute('data-id') || 0);
        const instanceName = String(deleteBtn.getAttribute('data-name') || '').trim();
        if (!app || !instanceId) return;
        showDeleteInstanceModal({
          app,
          instanceId,
          instanceName: instanceName || `#${instanceId}`,
          discardUnsaved: settingsDirty,
        });
        return;
      }

      const clearBtn = e.target && e.target.closest ? e.target.closest('button[data-clear-key]') : null;
      if (!clearBtn) return;
      if (clearBtn.disabled) return;
      const app = String(clearBtn.getAttribute('data-app') || '').trim();
      const instanceId = Number(clearBtn.getAttribute('data-id') || 0);
      if (!app || !instanceId) return;
      if (!confirmDiscardUnsavedSettings('Deleting the stored API key')) return;
      if (!confirm(`Delete the stored ${app.toUpperCase()} API key for instance #${instanceId}?`)) return;
      const r = await apiFetch('/api/credentials/clear', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ app, instance_id: instanceId }),
      });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) {
        msg.textContent = data.error || 'Delete failed';
        settingsStatusMessage = data.error || 'Delete failed';
        syncSettingsSaveFab();
        return;
      }
      msg.textContent = 'API key deleted';
      settingsStatusMessage = 'API key deleted';
      syncSettingsSaveFab();
      await loadSettings();
    });
    setSection('dashboard');
    ensureAuth();

    if (!window.settingsActiveTab) window.settingsActiveTab = 'global';
    function sortSettingsInstances(instances) {
      return [...(instances || [])].sort((a, b) => {
        const appA = String(a.app || '');
        const appB = String(b.app || '');
        if (appA !== appB) return appA.localeCompare(appB);
        return Number(a.instance_id || 0) - Number(b.instance_id || 0);
      });
    }
    function nextSettingsInstanceId(app) {
      let maxId = 0;
      (window.settingsInstances || []).forEach(inst => {
        if (String(inst.app || '').trim().toLowerCase() !== app) return;
        maxId = Math.max(maxId, Number(inst.instance_id || 0));
      });
      return maxId + 1;
    }
    function newSettingsInstance(app) {
      const instanceId = nextSettingsInstanceId(app);
      const label = app === 'radarr' ? 'Radarr' : 'Sonarr';
      return {
        app,
        instance_id: instanceId,
        instance_name: `${label} ${instanceId}`,
        enabled: true,
        interval_minutes: 15,
        search_missing: true,
        search_cutoff_unmet: true,
        upgrade_scope: 'wanted',
        search_order: 'smart',
        quiet_hours_start: '23:00',
        quiet_hours_end: '06:00',
        min_hours_after_release: 8,
        min_seconds_between_actions: 2,
        max_missing_actions_per_instance_per_sync: 5,
        max_cutoff_actions_per_instance_per_sync: 1,
        sonarr_missing_mode: 'smart',
        item_retry_hours: 72,
        rate_window_minutes: 60,
        rate_cap: 25,
        arr_url: '',
        api_key_set: false,
      };
    }
    window.updateSettingsTabs = function(instances) {
      const tabsWrap = document.getElementById('settings-tabs');
      if (!tabsWrap) return;
      const instanceKeys = new Set((instances || []).map(inst => `${inst.app}:${inst.instance_id}`));
      if (window.settingsActiveTab !== 'global' && !instanceKeys.has(window.settingsActiveTab)) {
        window.settingsActiveTab = (instances && instances.length) ? `${instances[0].app}:${instances[0].instance_id}` : 'global';
      }

      const isGlobal = (window.settingsActiveTab === 'global');
      let html = `<button class="tab-btn settings-tab-btn ${isGlobal ? 'active' : ''}" onclick="window.settingsActiveTab='global'; window.updateSettingsTabs(window.settingsInstances); return false;">Global Settings</button>`;

      (instances || []).forEach(inst => {
        const key = `${inst.app}:${inst.instance_id}`;
        const isActive = (window.settingsActiveTab === key);
        const instanceName = String(inst.instance_name || `${String(inst.app || '').toUpperCase()} ${inst.instance_id}`).trim();
        html += `<button class="tab-btn settings-tab-btn ${isActive ? 'active' : ''}" onclick="window.settingsActiveTab='${key}'; window.updateSettingsTabs(window.settingsInstances); return false;">${safe(inst.app).toUpperCase()} - ${safe(instanceName)}</button>`;
      });
      tabsWrap.innerHTML = html;

      document.querySelectorAll('.settings-tab-content').forEach(el => {
        if (el.id === `settings-tab-${window.settingsActiveTab}`) el.style.display = 'block';
        else el.style.display = 'none';
      });
    };
    function renderSettingsCards(instances) {
      const wrap = document.getElementById('settings-instance-cards');
      wrap.innerHTML = '';
      window.settingsInstances = sortSettingsInstances(instances);
      if (!window.settingsInstances.length) {
        wrap.innerHTML = `<div class="card settings-tab-content empty-state-card" id="settings-tab-empty"><div class="subline">No instances configured yet. Use the add buttons above to create a Radarr or Sonarr instance.</div></div>`;
      }
      for (const inst of window.settingsInstances) {
        const key = `${inst.app}:${inst.instance_id}`;
        const instanceName = String(inst.instance_name || `${String(inst.app || '').toUpperCase()} ${inst.instance_id}`).trim();
        const mode = String(inst.sonarr_missing_mode || 'smart').toLowerCase();
        const upgradeScopeRaw = String(inst.upgrade_scope || 'wanted').toLowerCase();
        const upgradeScope = (upgradeScopeRaw === 'all_monitored') ? 'both' : upgradeScopeRaw;
        const order = String(inst.search_order || 'smart').toLowerCase();
        const orderUi = `
              <div class="field">
                <div class="label">Search Order</div>
                <select class="cfg si_search_order">
                  <option value="smart" ${order === 'smart' ? 'selected' : ''}>Smart (Recent, Random, Oldest)</option>
                  <option value="newest" ${order === 'newest' ? 'selected' : ''}>Newest First</option>
                  <option value="random" ${order === 'random' ? 'selected' : ''}>Random</option>
                  <option value="oldest" ${order === 'oldest' ? 'selected' : ''}>Oldest First</option>
                </select>
              </div>
        `;
        const behaviorUi = `
            <div class="settings-grid-auto settings-grid-spaced">
              <div class="field">
                <div class="label">
                  Quiet Start (HH:MM)
                  <span class="info-icon" title="Searches will pause during quiet hours. Force runs bypass these hours."><svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm1 15h-2v-6h2v6zm0-8h-2V7h2v2z"/></svg></span>
                </div>
                <input class="cfg mono si_quiet_start" type="text" value="${safe(inst.quiet_hours_start)}" placeholder="23:00"/>
              </div>
              <div class="field">
                <div class="label">
                  Quiet End (HH:MM)
                  <span class="info-icon" title="Searches resume after this time. Force runs bypass these hours."><svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm1 15h-2v-6h2v6zm0-8h-2V7h2v2z"/></svg></span>
                </div>
                <input class="cfg mono si_quiet_end" type="text" value="${safe(inst.quiet_hours_end)}" placeholder="06:00"/>
              </div>
              <div class="field">
                <div class="label">
                  Hours After Release
                  <span class="info-icon" title="Minimum hours after a title's release date before Seekarr will search for it."><svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm1 15h-2v-6h2v6zm0-8h-2V7h2v2z"/></svg></span>
                </div>
                <input class="cfg si_after_release" type="number" min="0" value="${safe(inst.min_hours_after_release)}"/>
              </div>
              <div class="field">
                <div class="label">
                  Seconds Between
                  <span class="info-icon" title="Minimum delay in seconds between consecutive search actions to avoid hammering the indexer."><svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm1 15h-2v-6h2v6zm0-8h-2V7h2v2z"/></svg></span>
                </div>
                <input class="cfg si_between" type="number" min="0" value="${safe(inst.min_seconds_between_actions)}"/>
              </div>
              <div class="field">
                <div class="label">
                  Upgrade Source
                  <span class="info-icon" title="Wanted List = Arr's cutoff-unmet upgrade list. Monitored Items = Any monitored item that has files. Both = Combines both sources."><svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm1 15h-2v-6h2v6zm0-8h-2V7h2v2z"/></svg></span>
                </div>
                <select class="cfg si_upgrade_scope">
                  <option value="wanted" ${upgradeScope === 'wanted' ? 'selected' : ''}>Wanted List</option>
                  <option value="monitored" ${upgradeScope === 'monitored' ? 'selected' : ''}>Monitored Items</option>
                  <option value="both" ${upgradeScope === 'both' ? 'selected' : ''}>Both Sources</option>
                </select>
              </div>
            </div>
        `;
        const limitsUi = `
            <div class="settings-grid-auto">
              <div class="field">
                <div class="label">Interval (min)</div>
                <input class="cfg si_interval" type="number" min="1" value="${safe(inst.interval_minutes)}"/>
              </div>
              <div class="field">
                <div class="label">Retry (hours)</div>
                <input class="cfg si_retry" type="number" min="1" value="${safe(inst.item_retry_hours)}"/>
              </div>
              <div class="field">
                <div class="label">Rate Window (min)</div>
                <input class="cfg si_rate_window" type="number" min="1" value="${safe(inst.rate_window_minutes)}"/>
              </div>
              <div class="field">
                <div class="label">Rate Cap</div>
                <input class="cfg si_rate_cap" type="number" min="1" value="${safe(inst.rate_cap)}"/>
              </div>
              <div class="field">
                <div class="label">Missing Per Run</div>
                <input class="cfg si_missing_per_run" type="number" min="0" value="${safe(inst.max_missing_actions_per_instance_per_sync)}"/>
              </div>
              <div class="field">
                <div class="label">Upgrades Per Run</div>
                <input class="cfg si_upgrades_per_run" type="number" min="0" value="${safe(inst.max_cutoff_actions_per_instance_per_sync)}"/>
              </div>
            </div>
        `;
        const modeUi = (inst.app === 'sonarr') ? `
              <div class="field field-stack-gap">
                <div class="label">
                  Missing Mode
                  <span class="info-icon" title="Smart = auto-selects best mode. Season Packs = uses SeasonSearch API. Show Batch = EpisodeSearch for all missing eps in a show. Episode = per-episode search."><svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm1 15h-2v-6h2v6zm0-8h-2V7h2v2z"/></svg></span>
                </div>
                <select class="cfg si_missing_mode">
                  <option value="smart" ${mode === 'smart' ? 'selected' : ''}>Smart</option>
                  <option value="season_packs" ${mode === 'season_packs' ? 'selected' : ''}>Season Packs</option>
                  <option value="shows" ${mode === 'shows' ? 'selected' : ''}>Show Batch</option>
                  <option value="episodes" ${mode === 'episodes' ? 'selected' : ''}>Episode</option>
                </select>
              </div>
        ` : '';
        wrap.innerHTML += `
          <div class="card settings-tab-content settings-instance-card" id="settings-tab-${safe(key)}" data-app="${safe(inst.app)}" data-key="${safe(key)}">
            <div class="instance-head settings-instance-head">
              <div>
                <div class="instance-title settings-instance-title">
                  <svg class="settings-instance-icon" width="22" height="22" fill="none" stroke="var(--accent-color)" stroke-width="2" viewBox="0 0 24 24"><rect x="2" y="3" width="20" height="14" rx="2" ry="2"></rect><line x1="8" y1="21" x2="16" y2="21"></line><line x1="12" y1="17" x2="12" y2="21"></line></svg>
                  <span>${safe(inst.app).toUpperCase()} - ${safe(instanceName)}</span>
                  <span class="settings-instance-badge">#${safe(inst.instance_id)}</span>
                </div>
                <div class="subline mono settings-instance-url">${safe(inst.arr_url) || '-'}</div>
              </div>
              <div class="pill-row settings-toggle-group">
                <label class="tog subline settings-toggle-chip"><input type="checkbox" class="si_enabled" ${inst.enabled ? 'checked' : ''}> Enabled</label>
                <label class="tog subline settings-toggle-chip"><input type="checkbox" class="si_missing" ${inst.search_missing ? 'checked' : ''}> Missing</label>
                <label class="tog subline settings-toggle-chip"><input type="checkbox" class="si_cutoff" ${inst.search_cutoff_unmet ? 'checked' : ''}> Upgrades</label>
              </div>
            </div>

            <div class="settings-panel">
              <h4 class="settings-panel-title">Connection Details</h4>
              <div class="settings-grid-wide">
                <div class="field">
                  <div class="label">Instance Name</div>
                  <input class="cfg si_name" type="text" value="${safe(instanceName)}"/>
                </div>
                <div class="field">
                  <div class="label">Arr URL</div>
                  <input class="cfg mono si_url" type="text" value="${safe(inst.arr_url)}"/>
                </div>
                <div class="field">
                  <div class="label">
                    API Key
                    <span class="info-icon" title="Enter a new key to update it. Leave blank to keep the existing key unchanged."><svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm1 15h-2v-6h2v6zm0-8h-2V7h2v2z"/></svg></span>
                  </div>
                  <div class="inline-input settings-api-key-row">
                    <input class="cfg mono si_apikey" type="password" value="" placeholder="${inst.api_key_set ? '********' : '(not set)'}"/>
                    <button class="icon-btn danger" type="button" title="Delete stored API key"
                            data-clear-key="1" data-app="${safe(inst.app)}" data-id="${safe(inst.instance_id)}" ${inst.api_key_set ? '' : 'disabled'}>
                      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="16" height="16">
                        <path d="M3 6h18"></path>
                        <path d="M8 6V4h8v2"></path>
                        <path d="M6 6l1 16h10l1-16"></path>
                        <path d="M10 11v6"></path>
                        <path d="M14 11v6"></path>
                      </svg>
                    </button>
                  </div>
                </div>
              </div>
            </div>

            <div class="settings-panel">
              <h4 class="settings-panel-title">Limits & Intervals</h4>
              ${limitsUi}
            </div>

            <div class="settings-panel">
              <h4 class="settings-panel-title">Search Behavior</h4>
              ${behaviorUi}
              <div class="settings-grid-auto">
                ${orderUi}
                ${modeUi}
              </div>
            </div>

            <div class="settings-panel danger-panel">
              <h4 class="settings-panel-title danger-title">Remove Instance</h4>
              <div class="subline danger-copy">
                Removes this instance from Seekarr and deletes its stored API key, schedule state, and instance-specific history.
              </div>
              <button
                class="btn-secondary danger-soft"
                type="button"
                data-delete-instance="1"
                data-app="${safe(inst.app)}"
                data-id="${safe(inst.instance_id)}"
                data-name="${safe(instanceName)}"
              >
                Remove This Instance
              </button>
            </div>

          </div>
        `;


      }
      window.updateSettingsTabs(window.settingsInstances);
    }

    function addSettingsInstance(app) {
      const next = newSettingsInstance(app);
      window.settingsInstances = sortSettingsInstances([...(window.settingsInstances || []), next]);
      window.settingsActiveTab = `${next.app}:${next.instance_id}`;
      renderSettingsCards(window.settingsInstances);
      refreshSettingsDirtyState();
    }

    async function loadSettings() {
      populateTimezoneOptions();
      const r = await apiFetch('/api/settings', { cache:'no-store' });
      const data = await r.json();
      const appCfg = data.app || {};
      document.getElementById('settings-quiet-timezone').value = String(appCfg.quiet_hours_timezone || '').trim();
      renderSettingsCards(data.instances || []);
      settingsBaseline = settingsPayloadFingerprint(buildSettingsPayload());
      setSettingsDirtyState(false, '');
    }

    async function saveSettings() {
      const msg = document.getElementById('settings-msg');
      const btn = document.getElementById('save-settings');
      btn.disabled = true;
      settingsStatusMessage = 'Saving...';
      syncSettingsSaveFab();

      try {
        const payload = buildSettingsPayload();
        const instances = payload.instances;
        const invalidInstance = instances.find(inst => !inst.instance_name);
        if (invalidInstance) {
          msg.textContent = `Instance #${invalidInstance.instance_id} needs a name`;
          settingsStatusMessage = msg.textContent;
          return;
        }

        const r = await apiFetch('/api/settings', {
          method:'POST',
          headers:{'Content-Type':'application/json'},
          body: JSON.stringify(payload),
        });
        const data = await r.json();
        if (!r.ok) {
          msg.textContent = data.error || 'Save failed';
          settingsStatusMessage = msg.textContent;
          return;
        }
        msg.textContent = 'Saved';
        await loadSettings();
        await refresh();
      } catch (e) {
        msg.textContent = 'Save failed';
        settingsStatusMessage = 'Save failed';
      } finally {
        btn.disabled = false;
        syncSettingsSaveFab();
      }
    }

    document.getElementById('add-radarr-instance').addEventListener('click', () => addSettingsInstance('radarr'));
    document.getElementById('add-sonarr-instance').addEventListener('click', () => addSettingsInstance('sonarr'));
    document.getElementById('save-settings').addEventListener('click', saveSettings);
    document.getElementById('section-settings').addEventListener('input', (e) => {
      const target = e.target;
      if (!target || !(target instanceof HTMLElement)) return;
      if (
        target.id === 'settings-quiet-timezone' ||
        target.closest('#settings-instance-cards')
      ) {
        refreshSettingsDirtyState();
      }
    });
    document.getElementById('section-settings').addEventListener('change', (e) => {
      const target = e.target;
      if (!target || !(target instanceof HTMLElement)) return;
      if (
        target.id === 'settings-quiet-timezone' ||
        target.closest('#settings-instance-cards')
      ) {
        refreshSettingsDirtyState();
      }
    });
    document.querySelectorAll('.nav-control').forEach(a => {
      a.addEventListener('click', () => {
        if (a.dataset.section === 'settings') loadSettings();
      });
    });
    syncSettingsSaveFab();
  </script>
</body>
</html>
""".replace("__ASSET_CACHE_KEY__", asset_cache_key)

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

        search_history: dict[str, Any] = {}
        for inst in cfg.radarr_instances:
            search_history[f"radarr:{inst.instance_id}"] = store.get_recent_search_actions(
                "radarr", inst.instance_id, 50
            )
        for inst in cfg.sonarr_instances:
            search_history[f"sonarr:{inst.instance_id}"] = store.get_recent_search_actions(
                "sonarr", inst.instance_id, 50
            )

        return jsonify(
            {
                "server_time_utc": datetime.now(timezone.utc).isoformat(),
                "version": _get_version_state(),
                "config": _config_view(cfg, store),
                "sync_status": store.get_sync_statuses(),
                "recent_runs": store.get_recent_runs(20),
                "recent_actions": store.get_recent_search_actions_global(50),
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
            store.set_ui_app_settings(quiet_hours_timezone=str(app_in.get("quiet_hours_timezone") or "").strip())
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

                values = {
                    "instance_name": str(row.get("instance_name") or _default_instance_name(app_name, iid)).strip()
                    or _default_instance_name(app_name, iid),
                    "enabled": 1 if bool(row.get("enabled", True)) else 0,
                    "interval_minutes": max(15, min(60, int(row.get("interval_minutes") or 15))),
                    "search_missing": 1 if bool(row.get("search_missing", True)) else 0,
                    "search_cutoff_unmet": 1 if bool(row.get("search_cutoff_unmet", True)) else 0,
                    "upgrade_scope": _normalize_upgrade_scope(row.get("upgrade_scope") or "wanted"),
                    "search_order": _normalize_search_order(row.get("search_order") or "smart"),
                    "quiet_hours_start": str(row.get("quiet_hours_start") or "").strip(),
                    "quiet_hours_end": str(row.get("quiet_hours_end") or "").strip(),
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
                    "arr_url": str(row.get("arr_url") or "").strip(),
                }
                store.upsert_ui_instance_settings(app_name, iid, values)

                api_key = str(row.get("arr_api_key") or "").strip()
                if api_key:
                    store.set_arr_api_key(app_name, iid, api_key)

            _reload_config()
            return jsonify({"ok": True})
        except Exception as exc:
            return jsonify({"error": str(exc)}), 400

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

    @app.post("/api/autorun")
    def set_autorun() -> Any:
        payload = request.get_json(silent=True) or {}
        enabled = bool(payload.get("enabled", True))
        with run_state_lock:
            run_state["autorun_enabled"] = enabled
        return jsonify({"autorun_enabled": enabled})

    @app.get("/favicon.ico")
    def favicon() -> Any:
        icon = _asset_path("seekarr-logo.svg")
        if icon.exists():
            return send_file(icon, mimetype="image/svg+xml")
        return "", 204

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

    @app.get("/assets/webui.css")
    def asset_webui_css() -> Any:
        stylesheet = _asset_path("webui.css")
        if not stylesheet.exists():
            return jsonify({"error": "Stylesheet asset not found"}), 404
        return send_file(stylesheet, mimetype="text/css")

    return app


def main() -> int:
    parser = argparse.ArgumentParser(description="Seekarr Web UI")
    parser.add_argument("--config", required=True, help="Path to YAML config file.")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8788, help="Bind port (default: 8788)")
    parser.add_argument(
        "--allow-public",
        action="store_true",
        help="Allow binding to a non-localhost host (NOT recommended without a reverse proxy/auth).",
    )
    args = parser.parse_args()

    host = str(args.host or "").strip()
    allow_public = bool(args.allow_public) or os.getenv("SEEKARR_ALLOW_PUBLIC_WEBUI", "0").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    if host not in ("127.0.0.1", "::1", "localhost") and not allow_public:
        raise SystemExit(
            f"Refusing to bind Web UI to host={host!r} without --allow-public "
            "(to prevent accidentally exposing API endpoints)."
        )

    config_path = str(Path(args.config).resolve())
    try:
        dotenv_path = Path(config_path).parent / ".env"
        if dotenv_path.exists():
            for raw in dotenv_path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
    except OSError:
        pass

    app = create_app(config_path)
    # Production default: use waitress (WSGI server). This avoids Flask's dev server warnings
    # and behaves more like a real deployment on Windows/Linux.
    from waitress import serve

    serve(app, host=args.host, port=args.port, threads=8)
    return 0
