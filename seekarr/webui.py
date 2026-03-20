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
from .config import RuntimeConfig, load_config
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
    for inst in config.radarr_instances:
        rows.append(_instance_row("radarr", inst))
    for inst in config.sonarr_instances:
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
    base_config = load_config(config_path)
    setup_logging(base_config.app.log_level)
    logger = logging.getLogger("seekarr.webui")
    wz = logging.getLogger("werkzeug")
    wz.addFilter(_QuietAccessFilter())
    store = StateStore(base_config.app.db_path)

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

        def _to_bool(value: Any) -> bool | None:
            if value is None:
                return None
            try:
                return bool(int(value))
            except (TypeError, ValueError):
                return bool(value)

        def _to_int(value: Any) -> int | None:
            if value is None:
                return None
            try:
                return int(value)
            except (TypeError, ValueError):
                return None

        def _apply_instance(app_type: str, inst: Any) -> Any:
            ov = raw_overrides.get((app_type, int(inst.instance_id)))
            if not ov:
                return inst
            updates: dict[str, Any] = {}

            for f in ("enabled", "search_missing", "search_cutoff_unmet"):
                b = _to_bool(ov.get(f))
                if b is not None:
                    updates[f] = b
            for f in (
                "interval_minutes",
                "min_hours_after_release",
                "min_seconds_between_actions",
                "max_missing_actions_per_instance_per_sync",
                "max_cutoff_actions_per_instance_per_sync",
                "item_retry_hours",
                "rate_window_minutes",
                "rate_cap",
            ):
                iv = _to_int(ov.get(f))
                if iv is not None:
                    updates[f] = iv
            for f in ("upgrade_scope", "search_order", "quiet_hours_start", "quiet_hours_end", "sonarr_missing_mode"):
                v = ov.get(f)
                if v is not None:
                    updates[f] = str(v).strip()

            arr_url = ov.get("arr_url")
            if arr_url is not None and str(arr_url).strip():
                updates["arr"] = replace(inst.arr, url=str(arr_url).strip())

            return replace(inst, **updates) if updates else inst

        radarr_instances = [_apply_instance("radarr", inst) for inst in cfg.radarr_instances]
        sonarr_instances = [_apply_instance("sonarr", inst) for inst in cfg.sonarr_instances]
        return replace(cfg, app=app_cfg, radarr_instances=radarr_instances, sonarr_instances=sonarr_instances)

    config = _with_ui_overrides(base_config)
    engine = Engine(config=config, logger=logger)
    config_lock = threading.Lock()
    run_lock = threading.Lock()
    run_state_lock = threading.Lock()
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
    assets_dir = Path(__file__).resolve().parent / "assets"
    repo_assets_dir = Path(__file__).resolve().parent.parent

    def _asset_path(name: str) -> Path:
        bundled = assets_dir / name
        if bundled.exists():
            return bundled
        fallback = repo_assets_dir / name
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

    for inst in config.radarr_instances:
        threading.Thread(
            target=_autorun_instance_loop,
            args=("radarr", int(inst.instance_id)),
            name=f"webui-autorun-radarr-{inst.instance_id}",
            daemon=True,
        ).start()
    for inst in config.sonarr_instances:
        threading.Thread(
            target=_autorun_instance_loop,
            args=("sonarr", int(inst.instance_id)),
            name=f"webui-autorun-sonarr-{inst.instance_id}",
            daemon=True,
        ).start()

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
  <style>
    :root {
      /* Seekarr: modern dark theme */
      --bg-primary: #090a0c;
      --bg-secondary: #111318;
      --bg-tertiary: #181b21;
      --text-primary: #f8fafc;
      --text-secondary: #e2e8f0;
      --text-muted: #94a3b8;
      --accent-color: #f97316; /* orange */
      --accent-hover: #fb923c;
      --success-color: #22c55e;
      --warning-color: #f59e0b;
      --error-color: #ef4444;
      --glass-bg: rgba(17, 19, 24, 0.65);
      --glass-border: rgba(249, 115, 22, 0.12);
      --radius-md: 10px;
      --radius-lg: 14px;
      --transition: all 0.2s cubic-bezier(0.4, 0, 0.2, 1);
    }
    * { box-sizing: border-box; }
    html, body {
      margin: 0;
      height: 100%;
      font-family: 'Inter', -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg-primary);
      color: var(--text-primary);
      -webkit-font-smoothing: antialiased;
    }

    .app {
      display: grid;
      grid-template-columns: 220px 1fr;
      min-height: 100vh;
    }
    .sidebar {
      background: linear-gradient(180deg, rgba(11, 11, 12, 0.98), rgba(8, 8, 9, 0.98));
      border-right: 1px solid rgba(249, 115, 22, 0.14);
      padding: 18px 14px;
    }
    .brand {
      font-weight: 800;
      font-size: 16px;
      letter-spacing: .3px;
      color: #fff;
      margin-bottom: 14px;
    }
    .nav-item {
      display: flex;
      align-items: center;
      gap: 10px;
      text-decoration: none;
      color: var(--text-muted);
      padding: 10px 14px;
      margin-bottom: 6px;
      border-radius: 8px;
      font-weight: 500;
      font-size: 14px;
      background: transparent;
      border: 1px solid transparent;
      border-left: 3px solid transparent;
      transition: var(--transition);
    }
    .nav-item svg {
      width: 18px;
      height: 18px;
      opacity: 0.7;
      transition: var(--transition);
    }
    .nav-item:hover {
      background: rgba(255, 255, 255, 0.03);
      color: var(--text-secondary);
    }
    .nav-item:hover svg {
      opacity: 1;
    }
    .nav-item.active {
      color: #fff;
      background: linear-gradient(90deg, rgba(249, 115, 22, 0.15), transparent);
      border-left: 3px solid var(--accent-color);
    }
    .nav-item.active svg {
      color: var(--accent-color);
      opacity: 1;
    }
    .sidebar-badges {
      margin-top: 12px;
      padding-top: 12px;
      border-top: 1px solid rgba(249, 115, 22, 0.14);
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
    }
    .sidebar-badge {
      display: inline-block;
      text-decoration: none;
      color: var(--text-secondary);
      font-size: 11px;
      font-weight: 700;
      padding: 5px 8px;
      border-radius: 999px;
      border: 1px solid rgba(255, 255, 255, 0.10);
      background: rgba(15, 23, 42, 0.45);
    }
    .sidebar-badge:hover {
      border-color: rgba(249, 115, 22, 0.35);
      color: #fff;
    }
    .sidebar-badge.update {
      border-color: rgba(245, 158, 11, 0.35);
      background: rgba(245, 158, 11, 0.14);
      color: rgba(253, 230, 138, 0.98);
    }
    .sidebar-badge.update:hover {
      border-color: rgba(245, 158, 11, 0.55);
    }
    .main { min-width: 0; display: flex; flex-direction: column; }
    .hero-banner {
      padding: 10px 16px 0 16px;
    }
    .hero-banner img {
      width: min(760px, 100%);
      height: auto;
      border-radius: 12px;
      border: 1px solid rgba(249, 115, 22, 0.16);
      display: block;
    }
    
    .chip {
      display: inline-flex;
      align-items: center;
      font-size: 12px;
      font-weight: 600;
      color: var(--text-muted);
      background: rgba(30, 41, 59, 0.6);
      border: 1px solid rgba(148, 163, 184, 0.2);
      border-radius: 999px;
      padding: 4px 10px;
    }
    .content-section { display: none; padding: 16px; }
    .content-section.active { display: block; }
    .actions {
      display: flex;
      gap: 10px;
      margin-bottom: 14px;
      flex-wrap: wrap;
      align-items: center;
    }
    button {
      border: 0;
      border-radius: 8px;
      cursor: pointer;
      padding: 9px 16px;
      font-weight: 600;
      color: #fff;
      transition: var(--transition);
    }
    button:active {
      transform: scale(0.97);
    }
    #run { background: var(--accent-color); }
    #run:hover { background: var(--accent-hover); }
    #runforce { background: var(--warning-color); color: #121212; }
    #runforce:hover { filter: brightness(1.08); }
    #msg { color: var(--text-muted); font-size: 13px; }
    .cards-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(320px, 1fr));
      gap: 12px;
      margin-bottom: 12px;
    }
    .instance-card {
      background: var(--glass-bg);
      border: 1px solid var(--glass-border);
      border-radius: var(--radius-lg);
      padding: 12px;
    }
    .instance-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      margin-bottom: 10px;
      padding-bottom: 10px;
      border-bottom: 1px solid rgba(71, 85, 105, 0.35);
    }
    .instance-title {
      font-weight: 700;
      color: var(--text-secondary);
      font-size: 14px;
    }
    .pill-row { display: flex; gap: 6px; flex-wrap: wrap; justify-content: flex-end; }
    .big-countdown {
      font-size: 30px;
      font-weight: 800;
      color: var(--text-primary);
      letter-spacing: 0.3px;
      margin: 6px 0 10px 0;
    }
    .subline { color: var(--text-muted); font-size: 12px; }
    .kv {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 8px;
      margin-top: 10px;
    }
    .kv .k {
      color: var(--text-muted);
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: .04em;
    }
    .kv .v { color: var(--text-secondary); font-size: 13px; margin-top: 3px; }
    .stat {
      background: var(--glass-bg);
      border: 1px solid var(--glass-border);
      border-radius: var(--radius-md);
      padding: 10px;
    }
    .stat-label { color: var(--text-muted); font-size: 11px; text-transform: uppercase; letter-spacing: .04em; }
    .stat-value { margin-top: 4px; font-size: 22px; font-weight: 700; color: var(--text-secondary); }
    .field { display: block; }
    .info-icon {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 16px;
      height: 16px;
      margin-left: 6px;
      vertical-align: middle;
      color: var(--accent-color);
      cursor: help;
      opacity: 0.7;
      transition: opacity 0.2s, transform 0.2s;
    }
    .info-icon:hover { 
      opacity: 1;
      transform: scale(1.1);
    }
    .card {
      background: var(--glass-bg);
      border: 1px solid var(--glass-border);
      border-radius: var(--radius-lg);
      padding: 12px;
      margin-bottom: 12px;
    }
    .card h3 { margin: 0 0 10px 0; font-size: 14px; color: var(--text-secondary); }
    .table-wrap { overflow-x: auto; }
    table { width: 100%; border-collapse: collapse; font-size: 13px; }
    th, td { text-align: left; padding: 12px 10px; border-bottom: 1px solid rgba(255, 255, 255, 0.05); white-space: nowrap; transition: background 0.2s; }
    tbody tr:hover { background: rgba(255, 255, 255, 0.02); }
    th { color: var(--text-muted); font-size: 11px; text-transform: uppercase; letter-spacing: .06em; font-weight: 600; }
    td { color: var(--text-secondary); }
    .mono { font-family: Consolas, monospace; }

    /* Search history tables: fixed time column, wrapping titles for consistent alignment. */
    table.history { table-layout: fixed; }
    table.history col.col-time { width: 200px; }
    table.history td.time { white-space: nowrap; }
    table.history td.title { white-space: normal; overflow-wrap: anywhere; }
    .badge {
      display: inline-block;
      padding: 2px 8px;
      border-radius: 999px;
      font-size: 11px;
      font-weight: 700;
    }
    .ok { background: rgba(34, 197, 94, 0.18); color: var(--success-color); }
    .off { background: rgba(239, 68, 68, 0.18); color: var(--error-color); }
    .warn { background: rgba(245, 158, 11, 0.2); color: var(--warning-color); }

    /* UI refresh (no framework) */
    body {
      background:
        radial-gradient(900px 520px at 12% 10%, rgba(249, 115, 22, 0.12), transparent 60%),
        radial-gradient(900px 520px at 86% 18%, rgba(34, 197, 94, 0.08), transparent 55%),
        linear-gradient(180deg, var(--bg-primary), #050505);
    }
    .sidebar {
      background: linear-gradient(180deg, rgba(14, 16, 20, 0.8), rgba(9, 10, 12, 0.9));
      backdrop-filter: blur(16px);
    }
    .topbar {
      background: linear-gradient(180deg, rgba(16, 17, 19, 0.82), rgba(10, 10, 11, 0.92));
      backdrop-filter: blur(10px);
    }
    .stat, .card, .instance-card {
      box-shadow:
        0 10px 28px rgba(0, 0, 0, 0.38),
        inset 0 1px 0 rgba(255, 255, 255, 0.03);
    }
    .instance-card {
      position: relative;
      overflow: hidden;
      padding: 16px;
      border-color: rgba(255, 255, 255, 0.04);
      background:
        radial-gradient(520px 220px at 12% 8%, rgba(255, 255, 255, 0.04), transparent 55%),
        var(--glass-bg);
      backdrop-filter: blur(12px);
      transition: var(--transition);
      box-shadow: 0 4px 20px rgba(0, 0, 0, 0.2);
    }
    .instance-card:hover {
      transform: translateY(-3px);
      border-color: rgba(249, 115, 22, 0.3);
      box-shadow: 0 8px 30px rgba(0, 0, 0, 0.4), 0 0 15px rgba(249, 115, 22, 0.1);
    }
    /* Keep per-app accents but stay within the orange brand. */
    .instance-card[data-app="radarr"] { --accent-app: rgba(249, 115, 22, 0.92); }
    .instance-card[data-app="sonarr"] { --accent-app: rgba(251, 146, 60, 0.92); }
    .instance-card::before {
      content: "";
      position: absolute;
      left: 0;
      top: 0;
      height: 3px;
      width: 100%;
      background: linear-gradient(90deg, var(--accent-app, rgba(249, 115, 22, 0.85)), transparent 70%);
      opacity: 0.9;
    }
    .big-countdown {
      font-size: 34px;
      margin: 10px 0 8px 0;
      font-variant-numeric: tabular-nums;
    }
    .big-countdown.due {
      color: #fca5a5;
      text-shadow: 0 0 18px rgba(239, 68, 68, 0.2);
    }
    .subline.warn {
      color: rgba(251, 191, 36, 0.95);
    }
    .progress {
      height: 8px;
      border-radius: 999px;
      background: rgba(148, 163, 184, 0.14);
      overflow: hidden;
      border: 1px solid rgba(255, 255, 255, 0.05);
    }
    .progress > .bar {
      height: 100%;
      width: 0%;
      border-radius: 999px;
      /* Match the instance accent color (radarr/sonarr) instead of a random multi-color gradient. */
      background: var(--accent-app, rgba(249, 115, 22, 0.9));
      transition: width 120ms ease;
    }
    .progress > .bar.cap {
      background: rgba(239, 68, 68, 0.92);
    }
    .status {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 3px 9px;
      border-radius: 999px;
      font-size: 11px;
      font-weight: 800;
      letter-spacing: .02em;
      border: 1px solid rgba(255, 255, 255, 0.10);
      background: rgba(148, 163, 184, 0.12);
      color: rgba(226, 232, 240, 0.95);
    }
    .status.running { border-color: rgba(245, 158, 11, 0.35); background: rgba(245, 158, 11, 0.18); color: rgba(253, 230, 138, 0.98); }
    .status.due { border-color: rgba(239, 68, 68, 0.30); background: rgba(239, 68, 68, 0.16); color: rgba(254, 202, 202, 0.98); }
    .status.off { border-color: rgba(239, 68, 68, 0.25); background: rgba(239, 68, 68, 0.10); color: rgba(254, 202, 202, 0.90); }
    #recent-actions { white-space: pre-wrap; line-height: 1.35; max-height: 180px; overflow: auto; }
    .btn-mini {
      border: 1px solid rgba(255, 255, 255, 0.10);
      background: rgba(239, 68, 68, 0.16);
      border-color: rgba(239, 68, 68, 0.28);
      color: rgba(254, 202, 202, 0.98);
      padding: 5px 10px;
      border-radius: 999px;
      font-size: 11px;
      font-weight: 800;
      letter-spacing: .02em;
      cursor: pointer;
    }
    .btn-mini:hover { border-color: rgba(239, 68, 68, 0.52); background: rgba(239, 68, 68, 0.22); }
    .btn-mini:disabled { opacity: 0.5; cursor: not-allowed; }
    .settings-grid {
      grid-template-columns: repeat(2, minmax(340px, 1fr));
    }
    .field {
      display: flex;
      flex-direction: column;
      gap: 6px;
      margin-top: 10px;
    }
    .field:first-child { margin-top: 0; }
    .field .label {
      color: var(--text-muted);
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: .04em;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    input.cfg, select.cfg {
      width: 100%;
      padding: 9px 10px;
      height: 38px;
      line-height: 18px;
      font: inherit;
      border-radius: 10px;
      border: 1px solid rgba(255, 255, 255, 0.10);
      background: rgba(15, 23, 42, 0.55);
      color: var(--text-secondary);
      outline: none;
    }
    input.cfg:focus, select.cfg:focus {
      border-color: rgba(249, 115, 22, 0.45);
      box-shadow: 0 0 0 3px rgba(249, 115, 22, 0.16);
    }
    .inline-input {
      display: flex;
      gap: 8px;
      align-items: center;
    }
    .inline-input > input.cfg { flex: 1; }
    .icon-btn {
      width: 38px;
      height: 38px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      border-radius: 10px;
      border: 1px solid rgba(255, 255, 255, 0.10);
      background: rgba(15, 23, 42, 0.55);
      color: rgba(226, 232, 240, 0.95);
      padding: 0;
      cursor: pointer;
    }
    .icon-btn:hover { border-color: rgba(249, 115, 22, 0.35); }
    .icon-btn:disabled { opacity: 0.5; cursor: not-allowed; }
    .icon-btn.danger {
      border-color: rgba(239, 68, 68, 0.30);
      background: rgba(239, 68, 68, 0.12);
      color: rgba(254, 202, 202, 0.98);
    }
    .icon-btn.danger:hover { border-color: rgba(239, 68, 68, 0.55); background: rgba(239, 68, 68, 0.18); }
    .icon-btn svg { width: 16px; height: 16px; }
    .tog {
      display: inline-flex;
      gap: 8px;
      align-items: center;
      user-select: none;
    }
    .tog input[type="checkbox"] { transform: translateY(1px); }
    .two-col {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 10px;
      align-items: start;
    }
    /* In a two-column row, both columns should align to the same top edge. */
    .two-col .field { margin-top: 0; }
    @media (max-width: 900px) {
      .app { grid-template-columns: 1fr; }
      .sidebar { display: none; }
      .cards-grid { grid-template-columns: 1fr; }
      .settings-grid { grid-template-columns: 1fr; }
      .two-col { grid-template-columns: 1fr; }
    }

    .modal {
      position: fixed;
      inset: 0;
      display: none;
      align-items: center;
      justify-content: center;
      background: rgba(0,0,0,.55);
      z-index: 1000;
      padding: 18px;
    }
    .modal.show { display: flex; }
    .modal-card {
      width: min(520px, 100%);
      background: linear-gradient(180deg, rgba(255,255,255,.06), rgba(255,255,255,.03));
      border: 1px solid rgba(255,255,255,.10);
      border-radius: 14px;
      box-shadow: 0 18px 60px rgba(0,0,0,.55);
      padding: 14px;
    }
    .modal-row { display:flex; justify-content:space-between; align-items:center; gap:12px; }
    .modal-title { font-weight: 800; letter-spacing:.06em; text-transform: uppercase; color: var(--text-secondary); font-size: 13px; }
    .modal-body { margin-top: 12px; }
    .modal-actions { display:flex; justify-content:flex-end; gap:10px; margin-top: 12px; }
    .btn-primary {
      border: 1px solid rgba(255, 255, 255, 0.10);
      background: linear-gradient(180deg, rgba(249, 115, 22, 0.95), rgba(234, 88, 12, 0.88));
      color: #0b0d12;
      padding: 9px 12px;
      border-radius: 10px;
      font-weight: 900;
      letter-spacing: .06em;
      cursor: pointer;
    }
    .btn-primary:disabled { opacity:.55; cursor:not-allowed; }
  </style>
</head>
<body>
  <div class="modal" id="auth-modal">
    <div class="modal-card">
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

  <div class="app">
    <aside class="sidebar">
      <div class="brand" style="display: flex; align-items: center; gap: 8px; font-size: 18px; margin-bottom: 24px;">
        <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="var(--accent-color)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"></circle><line x1="21" y1="21" x2="16.65" y2="16.65"></line></svg>
        Seekarr
      </div>
      <a class="nav-item active" data-section="dashboard" href="#">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="9"></rect><rect x="14" y="3" width="7" height="5"></rect><rect x="14" y="12" width="7" height="9"></rect><rect x="3" y="16" width="7" height="5"></rect></svg>
        Dashboard
      </a>

      <a class="nav-item" data-section="runs" href="#">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"></circle><polyline points="12 6 12 12 16 14"></polyline></svg>
        History
      </a>
      <a class="nav-item" data-section="settings" href="#">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"></circle><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"></path></svg>
        Configuration
      </a>
      <div class="sidebar-badges">
        <a class="sidebar-badge" href="https://github.com/tumeden/seekarr" target="_blank" rel="noopener noreferrer">GitHub</a>
        <a class="sidebar-badge" href="https://hub.docker.com/r/tumeden/seekarr" target="_blank" rel="noopener noreferrer">Docker Hub</a>
        <span class="sidebar-badge" id="version-chip">Version --</span>
        <a class="sidebar-badge update" id="update-chip" href="https://github.com/tumeden/seekarr/releases/latest"
           target="_blank" rel="noopener noreferrer" style="display:none;">Update available</a>
      </div>
    </aside>
    <main class="main">
      <div class="hero-banner">
        <img src="/branding/banner.svg" alt="Seekarr banner"/>
      </div>
      
      <section class="content-section active" id="section-dashboard">
        <div class="actions">
          <label class="chip" style="gap:8px; cursor:pointer;">
            <input id="autorun-toggle" type="checkbox" style="accent-color: var(--accent-color);" checked />
            Auto-run
          </label>
          <span id="msg"></span>
        </div>

        <div class="cards-grid" id="instance-cards"></div>

        <div class="card">
          <h3>Recent Actions</h3>
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
        <div id="settings-tabs" style="display:flex; gap:8px; margin-bottom:16px; flex-wrap:wrap;"></div>
        
        <div id="settings-content-wrapper">
          <div class="card settings-tab-content" id="settings-tab-global" style="margin-top:0; padding:20px;">
            <div style="display:flex; align-items:center; gap:10px; margin-bottom:16px;">
              <svg viewBox="0 0 24 24" fill="none" stroke="var(--accent-color)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:20px; height:20px;"><circle cx="12" cy="12" r="3"></circle><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"></path></svg>
              <h3 style="margin:0; font-size:16px;">Global Configuration</h3>
            </div>
            <div class="subline" style="font-size:13px; margin-bottom:24px;">App-wide settings affecting all instances.</div>
            
            <div class="field" style="margin-top:16px; max-width:400px;">
              <div class="label">Quiet Hours Timezone</div>
              <input id="settings-quiet-timezone" class="cfg mono" type="text" list="timezone-options"
                     placeholder="Search timezone (example: America/New_York)"/>
              <datalist id="timezone-options"></datalist>
            </div>
            <div class="subline" style="margin-top:6px;">
              Used for quiet start/end evaluation. Leave empty to use server/container local timezone.
            </div>
          </div>
          
          <div id="settings-instance-cards"></div>
        </div>

        <div class="actions" style="justify-content:flex-end; margin-top:20px; padding-top:20px; border-top:1px solid rgba(255,255,255,0.05);">
          <span class="subline" id="settings-msg" style="margin-right:16px; font-size:14px; font-weight:600; color:var(--text-secondary);"></span>
          <button class="btn-primary" id="save-settings" style="padding:10px 24px; font-size:14px; box-shadow: 0 4px 12px rgba(249, 115, 22, 0.2);">SAVE CONFIGURATION</button>
        </div>
      </section>
    </main>
  </div>
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
        pw.setAttribute('autocomplete', 'new-password');
        hint.textContent = '';
      }

      modal.classList.add('show');
      setTimeout(() => pw.focus(), 50);
    }

    function hideAuthModal() {
      document.getElementById('auth-modal').classList.remove('show');
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
    function setSection(name) {
      document.querySelectorAll('.content-section').forEach(s => s.classList.remove('active'));
      document.getElementById(`section-${name}`)?.classList.add('active');
      document.querySelectorAll('.nav-item').forEach(a => a.classList.remove('active'));
      document.querySelector(`.nav-item[data-section=\"${name}\"]`)?.classList.add('active');
    }
    document.querySelectorAll('.nav-item').forEach(a => {
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



      const cards = document.getElementById('instance-cards');
      cards.innerHTML = '';
      for (const i of data.config.instances) {
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

        let statusText = 'WAIT';
        let statusClass = '';
        if (!i.enabled) {
          statusText = 'OFF';
          statusClass = 'off';
        } else if (runningThis) {
          statusText = 'RUNNING';
          statusClass = 'running';
        } else if (due) {
          statusText = 'DUE';
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
        cards.innerHTML += `
          <div class="instance-card" data-app="${safe(i.app)}" style="display: flex; flex-direction: column; justify-content: space-between;">
            <div>
              <div class="instance-head" style="margin-bottom: 20px; border-bottom: 1px solid rgba(255,255,255,0.06); padding-bottom: 16px;">
                <div class="instance-title" style="display:flex; align-items:center; gap:10px; font-size: 16px;">
                  <svg width="20" height="20" fill="none" stroke="var(--accent-color)" stroke-width="2" viewBox="0 0 24 24" style="opacity:0.9;"><rect x="2" y="3" width="20" height="14" rx="2" ry="2"></rect><line x1="8" y1="21" x2="16" y2="21"></line><line x1="12" y1="17" x2="12" y2="21"></line></svg>
                  ${safe(i.app).toUpperCase()} - <span style="font-weight: 500;">${safe(i.instance_name)}</span>
                  <span style="color:var(--text-muted); font-size:12px; font-weight: 500;">#${safe(i.instance_id)}</span>
                </div>
                <div class="pill-row" style="gap: 8px;">
                  <span class="status ${statusClass}" style="padding: 4px 10px; font-size: 12px;">${statusText}</span>
                  <button class="icon-btn" onclick="window.settingsActiveTab='${safe(i.app)}:${safe(i.instance_id)}'; setSection('settings'); loadSettings(); return false;" title="Configure Instance" style="padding: 5px 8px; background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.1); border-radius: 6px; color: var(--text-secondary); cursor: pointer; display: flex; align-items: center; justify-content: center; transition: 0.2s;" onmouseover="this.style.background='rgba(255,255,255,0.1)'; this.style.color='#fff';" onmouseout="this.style.background='rgba(255,255,255,0.05)'; this.style.color='var(--text-secondary)';">
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"></circle><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"></path></svg>
                  </button>
                  <button class="btn-mini" data-force-app="${safe(i.app)}" data-force-id="${safe(i.instance_id)}" ${disabledAttr} style="padding: 5px 14px;">FORCE</button>
                </div>
              </div>
              <div style="display: flex; justify-content: space-between; align-items: flex-end; margin-bottom: 16px;">
                <div>
                  <div class="big-countdown ${due ? 'due' : ''}" data-next-sync="${safe(s.next_sync_time)}" style="margin:0 0 4px 0;">${cd}</div>
                  <div class="subline mono" title="${safe(s.next_sync_time) || ''}" style="opacity: 0.8;">Next run (${safe(getTimeZoneLabel())}): ${fmtTime(s.next_sync_time) || '-'}</div>
                  <div class="subline ${due ? 'warn' : ''}" style="margin-top:2px;">${note}</div>
                </div>
              </div>
              <div style="margin-bottom: 20px;">
                <div class="subline" style="display:flex; justify-content:space-between; gap:10px; font-weight:600;">
                  <span>Rate window (${safe(i.rate_window_minutes)}m)</span>
                  <span class="mono" style="color: var(--text-primary);">${used} / ${cap}</span>
                </div>
                <div class="progress" style="margin-top:8px; height: 6px; background: rgba(0,0,0,0.3);">
                  <div class="${barClass}" style="width:${pct}%;"></div>
                </div>
              </div>
            </div>
            <div style="background: rgba(0,0,0,0.15); border-radius: 10px; padding: 14px; border: 1px solid rgba(255,255,255,0.03);">
              <div class="kv" style="grid-template-columns: repeat(3, 1fr); gap: 12px;">
                <div><div class="k">Wanted</div><div class="v" style="font-weight:600;">${safe(lrs.wanted_count ?? '-')}</div></div>
                <div><div class="k">Triggered</div><div class="v" style="font-weight:600; color:var(--success-color);">${safe(lrs.actions_triggered ?? '-')}</div></div>
                <div><div class="k">Interval</div><div class="v">${safe(i.interval_minutes)}m</div></div>
                <div><div class="k">Retry</div><div class="v">${safe(i.item_retry_hours)}h</div></div>
                <div><div class="k" style="white-space:nowrap;">Last Sync</div><div class="v mono" style="font-size:11px;">${fmtTime(s.last_sync_time) || '-'}</div></div>
                <div><div class="k">Window</div><div class="v">${safe(i.rate_window_minutes)}m</div></div>
              </div>
            </div>
          </div>
        `;
      }

      // Search History: Tabs + Pagination
      const runsWrap = document.getElementById('runs-wrap');
      const sh = data.search_history || {};
      const instances = data.config.instances || [];
      if (!window.historyActiveTab && instances.length > 0) {
        window.historyActiveTab = `${instances[0].app}:${instances[0].instance_id}`;
      }
      if (!window.historyPage) window.historyPage = {};
      
      const PAGE_SIZE = 10;
      
      let tabsHtml = '<div style="display:flex; gap:8px; margin-bottom:16px; flex-wrap:wrap;">';
      instances.forEach(inst => {
        const key = `${inst.app}:${inst.instance_id}`;
        const isActive = (window.historyActiveTab === key);
        const bg = isActive ? 'var(--accent-color)' : 'rgba(255,255,255,0.05)';
        const color = isActive ? '#fff' : 'var(--text-secondary)';
        const border = isActive ? 'transparent' : 'rgba(255,255,255,0.1)';
        tabsHtml += `<button style="background:${bg}; color:${color}; border:1px solid ${border}; padding:8px 16px; font-size:13px; font-weight:600; border-radius:8px;" onclick="window.historyActiveTab='${key}'; window.historyPage['${key}']=1; refresh(); return false;">${safe(inst.app).toUpperCase()} - ${safe(inst.instance_name)}</button>`;
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
          body = `<tr><td colspan="2" class="mono" style="color: var(--text-muted); padding:16px;">No searches recorded yet.</td></tr>`;
        }

        // Pagination controls
        let paginationHtml = '';
        if (totalPages > 1) {
          paginationHtml = `<div style="display:flex; justify-content:space-between; align-items:center; margin-top:16px; padding-top:16px; border-top:1px solid rgba(255,255,255,0.05);">
            <div style="font-size:12px; color:var(--text-muted);">Showing ${startIdx + 1} to ${Math.min(startIdx + PAGE_SIZE, totalRows)} of ${totalRows} entries</div>
            <div style="display:flex; gap:8px;">
              <button class="btn-mini" style="padding:6px 12px; background:rgba(255,255,255,0.05); color:var(--text-primary); border-color:rgba(255,255,255,0.1);" ${currentPage === 1 ? 'disabled' : ''} onclick="window.historyPage['${key}']=${currentPage-1}; refresh(); return false;">Previous</button>
              <div style="display:flex; align-items:center; font-size:13px; font-weight:600; padding:0 8px;">Page ${currentPage} of ${totalPages}</div>
              <button class="btn-mini" style="padding:6px 12px; background:rgba(255,255,255,0.05); color:var(--text-primary); border-color:rgba(255,255,255,0.1);" ${currentPage === totalPages ? 'disabled' : ''} onclick="window.historyPage['${key}']=${currentPage+1}; refresh(); return false;">Next</button>
            </div>
          </div>`;
        }

        contentHtml = `
          <div class="card" style="margin-top:0;">
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
    document.getElementById('settings-instance-cards').addEventListener('click', async (e) => {
      const btn = e.target && e.target.closest ? e.target.closest('button[data-clear-key]') : null;
      if (!btn) return;
      if (btn.disabled) return;
      const app = String(btn.getAttribute('data-app') || '').trim();
      const instanceId = Number(btn.getAttribute('data-id') || 0);
      if (!app || !instanceId) return;
      if (!confirm(`Delete the stored ${app.toUpperCase()} API key for instance #${instanceId}?`)) return;
      const r = await apiFetch('/api/credentials/clear', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ app, instance_id: instanceId }),
      });
      const data = await r.json().catch(() => ({}));
      const msg = document.getElementById('settings-msg');
      if (!r.ok) {
        msg.textContent = data.error || 'Delete failed';
        return;
      }
      msg.textContent = 'API key deleted';
      await loadSettings();
    });
    setSection('dashboard');
    ensureAuth();

    if (!window.settingsActiveTab) window.settingsActiveTab = 'global';
    window.updateSettingsTabs = function(instances) {
      const tabsWrap = document.getElementById('settings-tabs');
      if (!tabsWrap) return;
      
      const isGlobal = (window.settingsActiveTab === 'global');
      const gBg = isGlobal ? 'var(--accent-color)' : 'rgba(255,255,255,0.05)';
      const gColor = isGlobal ? '#fff' : 'var(--text-secondary)';
      const gBorder = isGlobal ? 'transparent' : 'rgba(255,255,255,0.1)';
      let html = `<button style="background:${gBg}; color:${gColor}; border:1px solid ${gBorder}; padding:8px 16px; font-size:13px; font-weight:600; border-radius:8px; cursor:pointer;" onclick="window.settingsActiveTab='global'; window.updateSettingsTabs(window.settingsInstances); return false;">Global Settings</button>`;

      (instances || []).forEach(inst => {
        const key = `${inst.app}:${inst.instance_id}`;
        const isActive = (window.settingsActiveTab === key);
        const bg = isActive ? 'var(--accent-color)' : 'rgba(255,255,255,0.05)';
        const color = isActive ? '#fff' : 'var(--text-secondary)';
        const border = isActive ? 'transparent' : 'rgba(255,255,255,0.1)';
        html += `<button style="background:${bg}; color:${color}; border:1px solid ${border}; padding:8px 16px; font-size:13px; font-weight:600; border-radius:8px; cursor:pointer;" onclick="window.settingsActiveTab='${key}'; window.updateSettingsTabs(window.settingsInstances); return false;">${safe(inst.app).toUpperCase()} - ${safe(inst.instance_name)}</button>`;
      });
      tabsWrap.innerHTML = html;

      document.querySelectorAll('.settings-tab-content').forEach(el => {
        if (el.id === `settings-tab-${window.settingsActiveTab}`) el.style.display = 'block';
        else el.style.display = 'none';
      });
    };

    async function loadSettings() {
      populateTimezoneOptions();
      const r = await apiFetch('/api/settings', { cache:'no-store' });
      const data = await r.json();
      const appCfg = data.app || {};
      document.getElementById('settings-quiet-timezone').value = String(appCfg.quiet_hours_timezone || '').trim();

      const wrap = document.getElementById('settings-instance-cards');
      wrap.innerHTML = '';
      window.settingsInstances = data.instances || [];
      for (const inst of window.settingsInstances) {
        const key = `${inst.app}:${inst.instance_id}`;
        const mode = String(inst.sonarr_missing_mode || 'smart').toLowerCase();
        const upgradeScopeRaw = String(inst.upgrade_scope || 'wanted').toLowerCase();
        const upgradeScope = (upgradeScopeRaw === 'all_monitored') ? 'both' : upgradeScopeRaw;
        const order = String(inst.search_order || 'smart').toLowerCase();
        const orderUi = `
              <div class="field">
                <div class="label" style="font-weight:600; font-size:11px; text-transform:uppercase; letter-spacing:.04em; color:var(--text-secondary); margin-bottom:6px;">Search Order</div>
                <select class="cfg si_search_order" style="width:100%; min-height:40px; display:block; padding:8px 12px; border-radius:8px; background:rgba(0,0,0,0.3); border:1px solid rgba(255,255,255,0.08); color:var(--text-primary); font-size:14px; font-family:inherit; outline:none; box-sizing:border-box;">
                  <option value="smart" ${order === 'smart' ? 'selected' : ''}>Smart (Recent, Random, Oldest)</option>
                  <option value="newest" ${order === 'newest' ? 'selected' : ''}>Newest First</option>
                  <option value="random" ${order === 'random' ? 'selected' : ''}>Random</option>
                  <option value="oldest" ${order === 'oldest' ? 'selected' : ''}>Oldest First</option>
                </select>
              </div>
        `;
        const behaviorUi = `
            <div style="display:grid; grid-template-columns:repeat(auto-fill, minmax(220px, 1fr)); gap:20px; align-items:end; margin-bottom:20px;">
              <div class="field">
                <div class="label" style="font-weight:600; font-size:11px; text-transform:uppercase; letter-spacing:.04em; color:var(--text-secondary); margin-bottom:6px;">
                  Quiet Start (HH:MM)
                  <span class="info-icon" title="Searches will pause during quiet hours. Force runs bypass these hours."><svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm1 15h-2v-6h2v6zm0-8h-2V7h2v2z"/></svg></span>
                </div>
                <input class="cfg mono si_quiet_start" type="text" value="${safe(inst.quiet_hours_start)}" placeholder="23:00" style="width:100%; min-height:40px; display:block; box-sizing:border-box; padding:8px 12px; border-radius:8px; background:rgba(0,0,0,0.3); border:1px solid rgba(255,255,255,0.08); color:var(--text-primary); font-size:14px; outline:none;"/>
              </div>
              <div class="field">
                <div class="label" style="font-weight:600; font-size:11px; text-transform:uppercase; letter-spacing:.04em; color:var(--text-secondary); margin-bottom:6px;">
                  Quiet End (HH:MM)
                  <span class="info-icon" title="Searches resume after this time. Force runs bypass these hours."><svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm1 15h-2v-6h2v6zm0-8h-2V7h2v2z"/></svg></span>
                </div>
                <input class="cfg mono si_quiet_end" type="text" value="${safe(inst.quiet_hours_end)}" placeholder="06:00" style="width:100%; min-height:40px; display:block; box-sizing:border-box; padding:8px 12px; border-radius:8px; background:rgba(0,0,0,0.3); border:1px solid rgba(255,255,255,0.08); color:var(--text-primary); font-size:14px; outline:none;"/>
              </div>
              <div class="field">
                <div class="label" style="font-weight:600; font-size:11px; text-transform:uppercase; letter-spacing:.04em; color:var(--text-secondary); margin-bottom:6px;">
                  Hours After Release
                  <span class="info-icon" title="Minimum hours after a title's release date before Seekarr will search for it."><svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm1 15h-2v-6h2v6zm0-8h-2V7h2v2z"/></svg></span>
                </div>
                <input class="cfg si_after_release" type="number" min="0" value="${safe(inst.min_hours_after_release)}" style="width:100%; min-height:40px; display:block; box-sizing:border-box; padding:8px 12px; border-radius:8px; background:rgba(0,0,0,0.3); border:1px solid rgba(255,255,255,0.08); color:var(--text-primary); font-size:14px; outline:none;"/>
              </div>
              <div class="field">
                <div class="label" style="font-weight:600; font-size:11px; text-transform:uppercase; letter-spacing:.04em; color:var(--text-secondary); margin-bottom:6px;">
                  Seconds Between
                  <span class="info-icon" title="Minimum delay in seconds between consecutive search actions to avoid hammering the indexer."><svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm1 15h-2v-6h2v6zm0-8h-2V7h2v2z"/></svg></span>
                </div>
                <input class="cfg si_between" type="number" min="0" value="${safe(inst.min_seconds_between_actions)}" style="width:100%; min-height:40px; display:block; box-sizing:border-box; padding:8px 12px; border-radius:8px; background:rgba(0,0,0,0.3); border:1px solid rgba(255,255,255,0.08); color:var(--text-primary); font-size:14px; outline:none;"/>
              </div>
              <div class="field">
                <div class="label" style="font-weight:600; font-size:11px; text-transform:uppercase; letter-spacing:.04em; color:var(--text-secondary); margin-bottom:6px;">
                  Upgrade Source
                  <span class="info-icon" title="Wanted List = Arr's cutoff-unmet upgrade list. Monitored Items = Any monitored item that has files. Both = Combines both sources."><svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm1 15h-2v-6h2v6zm0-8h-2V7h2v2z"/></svg></span>
                </div>
                <select class="cfg si_upgrade_scope" style="width:100%; min-height:40px; display:block; box-sizing:border-box; padding:8px 12px; border-radius:8px; background:rgba(0,0,0,0.3); border:1px solid rgba(255,255,255,0.08); color:var(--text-primary); font-size:14px; font-family:inherit; outline:none;">
                  <option value="wanted" ${upgradeScope === 'wanted' ? 'selected' : ''}>Wanted List</option>
                  <option value="monitored" ${upgradeScope === 'monitored' ? 'selected' : ''}>Monitored Items</option>
                  <option value="both" ${upgradeScope === 'both' ? 'selected' : ''}>Both Sources</option>
                </select>
              </div>
            </div>
        `;
        const limitsUi = `
            <div style="display:grid; grid-template-columns:repeat(auto-fill, minmax(220px, 1fr)); gap:20px; align-items:end;">
              <div class="field">
                <div class="label" style="font-weight:600; font-size:11px; text-transform:uppercase; letter-spacing:.04em; color:var(--text-secondary); margin-bottom:6px;">Interval (min)</div>
                <input class="cfg si_interval" type="number" min="1" value="${safe(inst.interval_minutes)}" style="width:100%; min-height:40px; display:block; box-sizing:border-box; padding:8px 12px; border-radius:8px; background:rgba(0,0,0,0.3); border:1px solid rgba(255,255,255,0.08); color:var(--text-primary); font-size:14px; outline:none;"/>
              </div>
              <div class="field">
                <div class="label" style="font-weight:600; font-size:11px; text-transform:uppercase; letter-spacing:.04em; color:var(--text-secondary); margin-bottom:6px;">Retry (hours)</div>
                <input class="cfg si_retry" type="number" min="1" value="${safe(inst.item_retry_hours)}" style="width:100%; min-height:40px; display:block; box-sizing:border-box; padding:8px 12px; border-radius:8px; background:rgba(0,0,0,0.3); border:1px solid rgba(255,255,255,0.08); color:var(--text-primary); font-size:14px; outline:none;"/>
              </div>
              <div class="field">
                <div class="label" style="font-weight:600; font-size:11px; text-transform:uppercase; letter-spacing:.04em; color:var(--text-secondary); margin-bottom:6px;">Rate Window (min)</div>
                <input class="cfg si_rate_window" type="number" min="1" value="${safe(inst.rate_window_minutes)}" style="width:100%; min-height:40px; display:block; box-sizing:border-box; padding:8px 12px; border-radius:8px; background:rgba(0,0,0,0.3); border:1px solid rgba(255,255,255,0.08); color:var(--text-primary); font-size:14px; outline:none;"/>
              </div>
              <div class="field">
                <div class="label" style="font-weight:600; font-size:11px; text-transform:uppercase; letter-spacing:.04em; color:var(--text-secondary); margin-bottom:6px;">Rate Cap</div>
                <input class="cfg si_rate_cap" type="number" min="1" value="${safe(inst.rate_cap)}" style="width:100%; min-height:40px; display:block; box-sizing:border-box; padding:8px 12px; border-radius:8px; background:rgba(0,0,0,0.3); border:1px solid rgba(255,255,255,0.08); color:var(--text-primary); font-size:14px; outline:none;"/>
              </div>
              <div class="field">
                <div class="label" style="font-weight:600; font-size:11px; text-transform:uppercase; letter-spacing:.04em; color:var(--text-secondary); margin-bottom:6px;">Missing Per Run</div>
                <input class="cfg si_missing_per_run" type="number" min="0" value="${safe(inst.max_missing_actions_per_instance_per_sync)}" style="width:100%; min-height:40px; display:block; box-sizing:border-box; padding:8px 12px; border-radius:8px; background:rgba(0,0,0,0.3); border:1px solid rgba(255,255,255,0.08); color:var(--text-primary); font-size:14px; outline:none;"/>
              </div>
              <div class="field">
                <div class="label" style="font-weight:600; font-size:11px; text-transform:uppercase; letter-spacing:.04em; color:var(--text-secondary); margin-bottom:6px;">Upgrades Per Run</div>
                <input class="cfg si_upgrades_per_run" type="number" min="0" value="${safe(inst.max_cutoff_actions_per_instance_per_sync)}" style="width:100%; min-height:40px; display:block; box-sizing:border-box; padding:8px 12px; border-radius:8px; background:rgba(0,0,0,0.3); border:1px solid rgba(255,255,255,0.08); color:var(--text-primary); font-size:14px; outline:none;"/>
              </div>
            </div>
        `;
        const modeUi = (inst.app === 'sonarr') ? `
              <div class="field" style="margin-top:20px;">
                <div class="label" style="font-weight:600; font-size:11px; text-transform:uppercase; letter-spacing:.04em; color:var(--text-secondary); margin-bottom:6px;">
                  Missing Mode
                  <span class="info-icon" title="Smart = auto-selects best mode. Season Packs = uses SeasonSearch API. Show Batch = EpisodeSearch for all missing eps in a show. Episode = per-episode search."><svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm1 15h-2v-6h2v6zm0-8h-2V7h2v2z"/></svg></span>
                </div>
                <select class="cfg si_missing_mode" style="width:100%; min-height:40px; display:block; padding:8px 12px; border-radius:8px; background:rgba(0,0,0,0.3); border:1px solid rgba(255,255,255,0.08); color:var(--text-primary); font-size:14px; font-family:inherit; outline:none; box-sizing:border-box;">
                  <option value="smart" ${mode === 'smart' ? 'selected' : ''}>Smart</option>
                  <option value="season_packs" ${mode === 'season_packs' ? 'selected' : ''}>Season Packs</option>
                  <option value="shows" ${mode === 'shows' ? 'selected' : ''}>Show Batch</option>
                  <option value="episodes" ${mode === 'episodes' ? 'selected' : ''}>Episode</option>
                </select>
              </div>
        ` : '';
        wrap.innerHTML += `
          <div class="card settings-tab-content" id="settings-tab-${safe(key)}" data-app="${safe(inst.app)}" data-key="${safe(key)}" style="padding:24px; display:none; margin-top:0;">
            <div class="instance-head" style="align-items:flex-start; border-bottom:1px solid rgba(255,255,255,0.05); padding-bottom:20px; margin-bottom:24px;">
              <div>
                <div class="instance-title" style="font-size:18px; display:flex; gap:10px; align-items:center;">
                  <svg width="22" height="22" fill="none" stroke="var(--accent-color)" stroke-width="2" viewBox="0 0 24 24"><rect x="2" y="3" width="20" height="14" rx="2" ry="2"></rect><line x1="8" y1="21" x2="16" y2="21"></line><line x1="12" y1="17" x2="12" y2="21"></line></svg>
                  ${safe(inst.app).toUpperCase()} - ${safe(inst.instance_name)} 
                  <span style="color:var(--text-muted); font-size:13px; font-weight:500;">#${safe(inst.instance_id)}</span>
                </div>
                <div class="subline mono" style="margin-top:8px; opacity:0.8;">${safe(inst.arr_url) || '-'}</div>
              </div>
              <div class="pill-row" style="gap:20px; background:rgba(0,0,0,0.4); padding:10px 16px; border-radius:10px; border:1px solid rgba(255,255,255,0.05);">
                <label class="tog subline" style="cursor:pointer; font-weight:600; font-size:14px; color:var(--text-primary); display:flex; align-items:center; gap:8px;"><input type="checkbox" class="si_enabled" ${inst.enabled ? 'checked' : ''} style="width:18px; height:18px; accent-color:var(--accent-color);"> Enabled</label>
                <label class="tog subline" style="cursor:pointer; font-weight:600; font-size:14px; color:var(--text-primary); display:flex; align-items:center; gap:8px;"><input type="checkbox" class="si_missing" ${inst.search_missing ? 'checked' : ''} style="width:18px; height:18px; accent-color:var(--accent-color);"> Missing</label>
                <label class="tog subline" style="cursor:pointer; font-weight:600; font-size:14px; color:var(--text-primary); display:flex; align-items:center; gap:8px;"><input type="checkbox" class="si_cutoff" ${inst.search_cutoff_unmet ? 'checked' : ''} style="width:18px; height:18px; accent-color:var(--accent-color);"> Upgrades</label>
              </div>
            </div>

            <!-- Connection Settings -->
            <div style="background:rgba(255,255,255,0.015); border:1px solid rgba(255,255,255,0.05); border-radius:12px; padding:20px; margin-bottom:24px;">
              <h4 style="margin:0 0 16px 0; color:var(--text-primary); font-size:16px; letter-spacing:0.02em; border-bottom:1px solid rgba(255,255,255,0.05); padding-bottom:10px;">Connection Details</h4>
              <div style="display:grid; grid-template-columns:repeat(auto-fill, minmax(280px, 1fr)); gap:20px; align-items:end;">
                <div class="field">
                  <div class="label" style="font-weight:600; font-size:11px; text-transform:uppercase; letter-spacing:.04em; color:var(--text-secondary); margin-bottom:6px;">Arr URL</div>
                  <input class="cfg mono si_url" type="text" value="${safe(inst.arr_url)}" style="width:100%; min-height:40px; display:block; padding:8px 12px; box-sizing:border-box; border-radius:8px; background:rgba(0,0,0,0.3); border:1px solid rgba(255,255,255,0.08); color:var(--text-primary); font-size:14px; outline:none;"/>
                </div>
                <div class="field">
                  <div class="label" style="font-weight:600; font-size:11px; text-transform:uppercase; letter-spacing:.04em; color:var(--text-secondary); margin-bottom:6px;">
                    API Key
                    <span class="info-icon" title="Enter a new key to update it. Leave blank to keep the existing key unchanged."><svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm1 15h-2v-6h2v6zm0-8h-2V7h2v2z"/></svg></span>
                  </div>
                  <div class="inline-input" style="display:flex; gap:10px; width:100%;">
                    <input class="cfg mono si_apikey" type="password" value="" placeholder="${inst.api_key_set ? '********' : '(not set)'}" style="flex:1; width:100%; min-height:40px; display:block; box-sizing:border-box; padding:8px 12px; border-radius:8px; background:rgba(0,0,0,0.3); border:1px solid rgba(255,255,255,0.08); color:var(--text-primary); font-size:14px; outline:none;"/>
                    <button class="icon-btn danger" type="button" title="Delete stored API key" style="height:40px; width:40px; padding:0; display:flex; align-items:center; justify-content:center; background:rgba(239,68,68,0.1); color:#ef4444; border:1px solid rgba(239,68,68,0.2); border-radius:8px; cursor:pointer; flex-shrink:0;"
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

            <!-- Limits & Intervals -->
            <div style="background:rgba(255,255,255,0.015); border:1px solid rgba(255,255,255,0.05); border-radius:12px; padding:20px; margin-bottom:24px;">
              <h4 style="margin:0 0 16px 0; color:var(--text-primary); font-size:16px; letter-spacing:0.02em; border-bottom:1px solid rgba(255,255,255,0.05); padding-bottom:10px;">Limits & Intervals</h4>
              ${limitsUi}
            </div>

            <!-- Search Behavior -->
            <div style="background:rgba(255,255,255,0.015); border:1px solid rgba(255,255,255,0.05); border-radius:12px; padding:20px; margin-bottom:24px;">
              <h4 style="margin:0 0 16px 0; color:var(--text-primary); font-size:16px; letter-spacing:0.02em; border-bottom:1px solid rgba(255,255,255,0.05); padding-bottom:10px;">Search Behavior</h4>
              ${behaviorUi}
              <div style="display:grid; grid-template-columns:repeat(auto-fill, minmax(220px, 1fr)); gap:20px; align-items:end;">
                ${orderUi}
                ${modeUi}
              </div>
            </div>

          </div>
        `;


      }
      window.updateSettingsTabs(window.settingsInstances);
    }

    async function saveSettings() {
      const msg = document.getElementById('settings-msg');
      msg.textContent = 'Saving...';

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

      const payload = {
        app: {
          quiet_hours_timezone: String(document.getElementById('settings-quiet-timezone')?.value || '').trim(),
        },
        instances,
      };

      const r = await apiFetch('/api/settings', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify(payload),
      });
      const data = await r.json();
      if (!r.ok) {
        msg.textContent = data.error || 'Save failed';
        return;
      }
      msg.textContent = 'Saved';
      await refresh();
    }

    document.getElementById('save-settings').addEventListener('click', saveSettings);
    document.querySelectorAll('.nav-item').forEach(a => {
      a.addEventListener('click', () => {
        if (a.dataset.section === 'settings') loadSettings();
      });
    });
  </script>
</body>
</html>
"""

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

                values = {
                    "enabled": 1 if bool(row.get("enabled", True)) else 0,
                    "interval_minutes": max(1, int(row.get("interval_minutes") or 15)),
                    "search_missing": 1 if bool(row.get("search_missing", True)) else 0,
                    "search_cutoff_unmet": 1 if bool(row.get("search_cutoff_unmet", True)) else 0,
                    "upgrade_scope": str(row.get("upgrade_scope") or "wanted").strip().lower(),
                    "search_order": str(row.get("search_order") or "smart").strip().lower(),
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
                    "sonarr_missing_mode": str(row.get("sonarr_missing_mode") or "smart").strip().lower(),
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

    @app.get("/branding/banner.svg")
    def branding_banner() -> Any:
        banner = _asset_path("seekarr-banner.svg")
        if not banner.exists():
            return jsonify({"error": "Banner asset not found"}), 404
        return send_file(banner, mimetype="image/svg+xml")

    @app.get("/branding/logo.svg")
    def branding_logo() -> Any:
        logo = _asset_path("seekarr-logo.svg")
        if not logo.exists():
            return jsonify({"error": "Logo asset not found"}), 404
        return send_file(logo, mimetype="image/svg+xml")

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
