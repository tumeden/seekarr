import argparse
import base64
import hashlib
import hmac
import logging
import os
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml
from flask import Flask, jsonify, request

from .arr import ArrRequestError
from .config import RuntimeConfig, load_config
from .engine import Engine
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
            "sonarr_missing_mode": str(getattr(inst, "sonarr_missing_mode", "season_packs") or "season_packs"),
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
    return {"instances": rows}


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

def create_app(config_path: str) -> Flask:
    config_path = str(Path(config_path).resolve())
    config = load_config(config_path)
    setup_logging(config.app.log_level)
    logger = logging.getLogger("seekarr.webui")
    wz = logging.getLogger("werkzeug")
    wz.addFilter(_QuietAccessFilter())
    engine = Engine(config=config, logger=logger)
    store = StateStore(config.app.db_path)
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
        "autorun_enabled": os.getenv("WEBUI_AUTORUN_DEFAULT", "1").strip().lower() not in ("0", "false", "no", "off"),
        "autorun_last_check": None,
        "autorun_last_run_started": None,
        "active_app_type": None,
        "active_instance_id": None,
        "active_instance_name": None,
    }

    app = Flask(__name__)

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
        new_config = load_config(config_path)
        # We intentionally do not support changing db_path via Web UI. If db_path changes,
        # the caller should restart services with the new config.
        if Path(new_config.app.db_path).resolve() != Path(config.app.db_path).resolve():
            raise ValueError("Changing app.db_path via Web UI is not supported. Edit config and restart.")
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
  <style>
    :root {
      /* Seekarr: orange + black theme */
      --bg-primary: #0b0b0c;
      --bg-secondary: #101113;
      --bg-tertiary: #17181b;
      --text-primary: #f8fafc;
      --text-secondary: #e2e8f0;
      --text-muted: #94a3b8;
      --accent-color: #f97316; /* orange */
      --accent-hover: #fb923c;
      --success-color: #22c55e;
      --warning-color: #f59e0b;
      --error-color: #ef4444;
      --glass-bg: rgba(16, 17, 19, 0.72);
      --glass-border: rgba(249, 115, 22, 0.18);
      --radius-md: 10px;
      --radius-lg: 14px;
    }
    * { box-sizing: border-box; }
    html, body { margin: 0; height: 100%; font-family: "Segoe UI", Arial, sans-serif; background: var(--bg-primary); color: var(--text-primary); }
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
      font-weight: 700;
      font-size: 15px;
      letter-spacing: .4px;
      color: var(--text-secondary);
      margin-bottom: 14px;
    }
    .nav-item {
      display: block;
      text-decoration: none;
      color: var(--text-muted);
      padding: 10px 12px;
      margin-bottom: 8px;
      border-radius: 8px;
      background: rgba(30, 41, 59, 0.35);
      border: 1px solid transparent;
    }
    .nav-item.active {
      color: #fff;
      border-color: rgba(249, 115, 22, 0.40);
      background: rgba(249, 115, 22, 0.18);
    }
    .main { min-width: 0; display: flex; flex-direction: column; }
    
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
      padding: 9px 14px;
      font-weight: 600;
      color: #fff;
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
    th, td { text-align: left; padding: 8px 6px; border-bottom: 1px solid rgba(71, 85, 105, 0.35); white-space: nowrap; }
    th { color: var(--text-muted); font-size: 11px; text-transform: uppercase; letter-spacing: .04em; }
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
        radial-gradient(900px 520px at 12% 10%, rgba(249, 115, 22, 0.18), transparent 60%),
        radial-gradient(900px 520px at 86% 18%, rgba(34, 197, 94, 0.12), transparent 55%),
        linear-gradient(180deg, #0b0b0c, #070707);
    }
    .sidebar {
      background: linear-gradient(180deg, rgba(11, 11, 12, 0.92), rgba(7, 7, 7, 0.96));
      backdrop-filter: blur(10px);
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
      padding: 14px;
      border-color: rgba(255, 255, 255, 0.06);
      background:
        radial-gradient(520px 220px at 12% 8%, rgba(255, 255, 255, 0.06), transparent 55%),
        var(--glass-bg);
      transition: transform 120ms ease, border-color 120ms ease;
    }
    .instance-card:hover {
      transform: translateY(-1px);
      border-color: rgba(249, 115, 22, 0.26);
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
      <div class="brand">Seekarr</div>
      <a class="nav-item active" data-section="dashboard" href="#">Dashboard</a>
      <a class="nav-item" data-section="instances" href="#">Instances</a>
      <a class="nav-item" data-section="runs" href="#">Search History</a>
      <a class="nav-item" data-section="settings" href="#">Settings</a>
    </aside>
    <main class="main">
      
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

      <section class="content-section" id="section-instances">
        <div class="card">
          <h3>Instances</h3>
          <div class="table-wrap">
            <table id="instances">
              <thead><tr><th>App</th><th>Name</th><th>Enabled</th><th>Wanted</th><th>Interval</th><th>Retry</th><th>Rate</th><th>Last Sync (UTC)</th><th>Next Sync (UTC)</th><th>Countdown</th><th>URL</th></tr></thead>
              <tbody></tbody>
            </table>
          </div>
        </div>
      </section>

      <section class="content-section" id="section-runs">
        <div class="card">
          <h3>Search History (Per Instance)</h3>
          <div id="runs-wrap"></div>
        </div>
      </section>

      <section class="content-section" id="section-settings">
        <div class="card" style="margin-top:12px;">
          <h3>Instances</h3>
          <div class="subline">API keys are stored encrypted in the SQLite DB (not shown in UI).</div>
          <div style="height:10px;"></div>
          <div class="cards-grid" id="settings-instance-cards"></div>
        </div>

        <div class="actions" style="justify-content:flex-end; margin-top:12px;">
          <button class="btn-mini" id="save-settings">SAVE</button>
          <span class="subline" id="settings-msg" style="margin-left:10px;"></span>
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
    let refreshTimer = null;
    let countdownTimer = null;

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
        authHeader = '';
        btn.disabled = false;
        return;
      }

      hideAuthModal();
      await refresh();
      if (!timersStarted) {
        timersStarted = true;
        refreshTimer = setInterval(refresh, 5000);
        countdownTimer = setInterval(tickCountdowns, 1000);
      }
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
    function fmtUtc(iso) {
      if (!iso) return '';
      const t = Date.parse(iso);
      if (!Number.isFinite(t)) return safe(iso);
      return new Date(t).toISOString().replace('T', ' ').slice(0, 19);
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

      const iBody = document.querySelector('#instances tbody');
      iBody.innerHTML = '';
      for (const i of data.config.instances) {
        const key = `${i.app}:${i.instance_id}`;
        const s = syncMap[key] || {};
        const wantedPills =
          `${asPill(!!i.search_missing, 'MISS', 'Search missing items')}` +
          ` ${asPill(!!i.search_cutoff_unmet, 'UPG', 'Search upgrades (cutoff unmet)')}`;
        iBody.innerHTML += `<tr>
          <td>${safe(i.app)}</td>
          <td>${safe(i.instance_name)} <span class="mono" style="color: var(--text-muted);">#${safe(i.instance_id)}</span></td>
          <td>${asBadge(i.enabled)}</td>
          <td>${wantedPills}</td>
          <td>${safe(i.interval_minutes)}m</td>
          <td>${safe(i.item_retry_hours)}h</td>
          <td>${safe(i.rate_cap)}/${safe(i.rate_window_minutes)}m</td>
          <td class="mono" title="${safe(s.last_sync_time) || ''}">${fmtUtc(s.last_sync_time) || '-'}</td>
          <td class="mono" title="${safe(s.next_sync_time) || ''}">${fmtUtc(s.next_sync_time) || '-'}</td>
          <td class="mono" data-next-sync="${safe(s.next_sync_time)}">${fmtCountdown(s.next_sync_time)}</td>
          <td class="mono">${safe(i.arr_url)}</td>
        </tr>`;
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
          <div class="instance-card" data-app="${safe(i.app)}">
            <div class="instance-head">
              <div class="instance-title">${safe(i.app).toUpperCase()} - ${safe(i.instance_name)} (#${safe(i.instance_id)})</div>
              <div class="pill-row">
                <span class="status ${statusClass}">${statusText}</span>
                <button class="btn-mini" data-force-app="${safe(i.app)}" data-force-id="${safe(i.instance_id)}" ${disabledAttr}>FORCE</button>
              </div>
            </div>
            <div class="big-countdown ${due ? 'due' : ''}" data-next-sync="${safe(s.next_sync_time)}">${cd}</div>
            <div class="subline mono" title="${safe(s.next_sync_time) || ''}">Next run: UTC ${fmtUtc(s.next_sync_time) || '-'}</div>
            <div class="subline ${due ? 'warn' : ''}">${note}</div>
            <div style="margin-top:10px;">
              <div class="subline" style="display:flex; justify-content:space-between; gap:10px;">
                <span>Rate window (${safe(i.rate_window_minutes)}m)</span>
                <span class="mono">${used} / ${cap}</span>
              </div>
              <div class="progress" style="margin-top:6px;">
                <div class="${barClass}" style="width:${pct}%;"></div>
              </div>
            </div>
            <div class="kv">
              <div><div class="k">Wanted (Last)</div><div class="v">${safe(lrs.wanted_count ?? '-')}</div></div>
              <div><div class="k">Triggered (Last)</div><div class="v">${safe(lrs.actions_triggered ?? '-')}</div></div>
              <div><div class="k">Interval</div><div class="v">${safe(i.interval_minutes)}m</div></div>
              <div><div class="k">Retry</div><div class="v">${safe(i.item_retry_hours)}h</div></div>
              <div><div class="k">Last Sync</div><div class="v mono">${fmtUtc(s.last_sync_time) || '-'}</div></div>
              <div><div class="k">Window</div><div class="v">${safe(i.rate_window_minutes)}m</div></div>
            </div>
          </div>
        `;
      }

      // Search History: render one table per instance.
      const runsWrap = document.getElementById('runs-wrap');
      const sh = data.search_history || {};
      const instances = data.config.instances || [];
      runsWrap.innerHTML = '';
      for (const inst of instances) {
        const key = `${inst.app}:${inst.instance_id}`;
        const rows = sh[key] || [];
        let body = '';
        for (const row of rows) {
          body += `<tr>
            <td class="mono time" title="${safe(row.occurred_at) || ''}">${fmtUtc(row.occurred_at) || '-'}</td>
            <td class="title">${safe(row.title)}</td>
          </tr>`;
        }
        if (!body) {
          body = `<tr><td colspan="2" class="mono" style="color: var(--text-muted);">No searches recorded yet.</td></tr>`;
        }
        runsWrap.innerHTML += `
          <div class="card" style="margin-top:12px;">
            <h3>${safe(inst.app).toUpperCase()} - ${safe(inst.instance_name)} (#${safe(inst.instance_id)})</h3>
            <div class="table-wrap">
              <table class="history">
                <colgroup>
                  <col class="col-time" />
                  <col />
                </colgroup>
                <thead>
                  <tr>
                    <th>Time (UTC)</th><th>Title Searched</th>
                  </tr>
                </thead>
                <tbody>${body}</tbody>
              </table>
            </div>
          </div>`;
      }

      const actionsEl = document.getElementById('recent-actions');
      const actions = Array.isArray(rs.recent_actions) ? rs.recent_actions : [];
      if (!actions.length) {
        actionsEl.textContent = '-';
      } else {
        // Render newest-first.
        const lines = actions.slice().reverse().map(a => {
          const ts = fmtUtc(a.ts) || '--';
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

    async function loadSettings() {
      const r = await apiFetch('/api/settings', { cache:'no-store' });
      const data = await r.json();

      const wrap = document.getElementById('settings-instance-cards');
      wrap.innerHTML = '';
      for (const inst of (data.instances || [])) {
        const key = `${inst.app}:${inst.instance_id}`;
        const mode = String(inst.sonarr_missing_mode || 'season_packs').toLowerCase();
        const order = String(inst.search_order || 'smart').toLowerCase();
        const orderUi = `
            <div class="field" style="margin-top:10px;">
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
            <div class="two-col" style="margin-top:10px;">
              <div class="field">
                <div class="label">Quiet Start (HH:MM)</div>
                <input class="cfg mono si_quiet_start" type="text" value="${safe(inst.quiet_hours_start)}" placeholder="23:00"/>
              </div>
              <div class="field">
                <div class="label">Quiet End (HH:MM)</div>
                <input class="cfg mono si_quiet_end" type="text" value="${safe(inst.quiet_hours_end)}" placeholder="06:00"/>
              </div>
            </div>
            <div class="subline" style="margin-top:8px;">Force runs bypass quiet hours.</div>
            <div class="two-col" style="margin-top:10px;">
              <div class="field">
                <div class="label">Hours After Release</div>
                <input class="cfg si_after_release" type="number" min="0" value="${safe(inst.min_hours_after_release)}"/>
              </div>
              <div class="field">
                <div class="label">Seconds Between Actions</div>
                <input class="cfg si_between" type="number" min="0" value="${safe(inst.min_seconds_between_actions)}"/>
              </div>
            </div>
            <div class="two-col" style="margin-top:10px;">
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
            <div class="field" style="margin-top:10px;">
              <div class="label">Missing Mode</div>
              <select class="cfg si_missing_mode">
                <option value="season_packs" ${mode === 'season_packs' ? 'selected' : ''}>Season Packs (Default)</option>
                <option value="shows" ${mode === 'shows' ? 'selected' : ''}>Show Batch</option>
                <option value="episodes" ${mode === 'episodes' ? 'selected' : ''}>Episode</option>
              </select>
              <div class="subline" style="margin-top:6px;">
                Season Packs = <span class="mono">SeasonSearch</span>. Show Batch = <span class="mono">EpisodeSearch</span> for all missing episodes in a show.
              </div>
            </div>
        ` : '';
        wrap.innerHTML += `
          <div class="instance-card" data-app="${safe(inst.app)}" data-key="${safe(key)}" style="padding:14px;">
            <div class="instance-head" style="align-items:flex-start;">
              <div>
                <div class="instance-title">${safe(inst.app).toUpperCase()} - ${safe(inst.instance_name)} (#${safe(inst.instance_id)})</div>
                <div class="subline mono" style="margin-top:4px;">${safe(inst.arr_url) || '-'}</div>
              </div>
              <div class="pill-row">
                <label class="tog subline"><input type="checkbox" class="si_enabled" ${inst.enabled ? 'checked' : ''}> Enabled</label>
                <label class="tog subline"><input type="checkbox" class="si_missing" ${inst.search_missing ? 'checked' : ''}> Missing</label>
                <label class="tog subline"><input type="checkbox" class="si_cutoff" ${inst.search_cutoff_unmet ? 'checked' : ''}> Upgrades</label>
              </div>
            </div>

            <div class="two-col" style="margin-top:10px;">
              <div class="field">
                <div class="label">Interval (min)</div>
                <input class="cfg si_interval" type="number" min="1" value="${safe(inst.interval_minutes)}"/>
              </div>
              <div class="field">
                <div class="label">Retry (hours)</div>
                <input class="cfg si_retry" type="number" min="1" value="${safe(inst.item_retry_hours)}"/>
              </div>
            </div>

            <div class="two-col" style="margin-top:10px;">
              <div class="field">
                <div class="label">Rate Window (min)</div>
                <input class="cfg si_rate_window" type="number" min="1" value="${safe(inst.rate_window_minutes)}"/>
              </div>
              <div class="field">
                <div class="label">Rate Cap</div>
                <input class="cfg si_rate_cap" type="number" min="1" value="${safe(inst.rate_cap)}"/>
              </div>
            </div>

            <div class="field" style="margin-top:10px;">
              <div class="label">Arr URL</div>
              <input class="cfg mono si_url" type="text" value="${safe(inst.arr_url)}"/>
            </div>
            <div class="field" style="margin-top:10px;">
              <div class="label">API Key</div>
              <div class="inline-input">
                <input class="cfg mono si_apikey" type="password" value="" placeholder="${inst.api_key_set ? '********' : '(not set)'}"/>
                <button class="icon-btn danger" type="button" title="Delete stored API key" aria-label="Delete stored API key"
                        data-clear-key="1" data-app="${safe(inst.app)}" data-id="${safe(inst.instance_id)}" ${inst.api_key_set ? '' : 'disabled'}>
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                    <path d="M3 6h18"></path>
                    <path d="M8 6V4h8v2"></path>
                    <path d="M6 6l1 16h10l1-16"></path>
                    <path d="M10 11v6"></path>
                    <path d="M14 11v6"></path>
                  </svg>
                </button>
              </div>
              <div class="subline" style="margin-top:6px;">Leave blank to keep unchanged.</div>
            </div>
            ${orderUi}
            ${behaviorUi}
            ${modeUi}
          </div>
        `;
      }
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
          search_order: String(tr.querySelector('.si_search_order')?.value || 'smart'),
          quiet_hours_start: String(tr.querySelector('.si_quiet_start')?.value || '').trim(),
          quiet_hours_end: String(tr.querySelector('.si_quiet_end')?.value || '').trim(),
          min_hours_after_release: Number(tr.querySelector('.si_after_release')?.value || 0),
          min_seconds_between_actions: Number(tr.querySelector('.si_between')?.value || 0),
          max_missing_actions_per_instance_per_sync: Number(tr.querySelector('.si_missing_per_run')?.value || 0),
          max_cutoff_actions_per_instance_per_sync: Number(tr.querySelector('.si_upgrades_per_run')?.value || 0),
          sonarr_missing_mode: (app === 'sonarr') ? String(tr.querySelector('.si_missing_mode')?.value || 'season_packs') : undefined,
          item_retry_hours: Number(tr.querySelector('.si_retry')?.value || 0),
          rate_window_minutes: Number(tr.querySelector('.si_rate_window')?.value || 0),
          rate_cap: Number(tr.querySelector('.si_rate_cap')?.value || 0),
          arr_url: String(tr.querySelector('.si_url')?.value || '').trim(),
          arr_api_key: String(tr.querySelector('.si_apikey')?.value || '').trim(),
        });
      });

      const payload = {
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
                "config": _config_view(cfg, store),
                "sync_status": store.get_sync_statuses(),
                "recent_runs": store.get_recent_runs(20),
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
        return jsonify({"instances": _config_view(cfg, store).get("instances", [])})

    @app.post("/api/settings")
    def save_settings() -> Any:
        payload = request.get_json(silent=True) or {}
        inst_in = payload.get("instances") if isinstance(payload.get("instances"), list) else []

        try:
            raw = yaml.safe_load(Path(config_path).read_text(encoding="utf-8")) or {}
            if not isinstance(raw, dict):
                raw = {}
            # App-level settings are intentionally not editable via UI (instances only),
            # but we still read them for defaults when persisting per-instance fields.
            raw_app = raw.get("app") if isinstance(raw.get("app"), dict) else {}

            # Instances: update only fields that are safe to edit via UI.
            def _update_instances(section_key: str, arr_key: str, app_name: str) -> None:
                section = raw.get(section_key) if isinstance(raw.get(section_key), dict) else {}
                instances = section.get("instances") if isinstance(section.get("instances"), list) else []
                # Map UI payload for this app.
                ui_map: dict[int, dict[str, Any]] = {}
                for row in inst_in:
                    if not isinstance(row, dict):
                        continue
                    if str(row.get("app") or "").strip().lower() != app_name:
                        continue
                    try:
                        iid = int(row.get("instance_id") or 0)
                    except (TypeError, ValueError):
                        continue
                    if iid > 0:
                        ui_map[iid] = row

                for inst in instances:
                    if not isinstance(inst, dict):
                        continue
                    try:
                        iid = int(inst.get("instance_id") or 0)
                    except (TypeError, ValueError):
                        continue
                    ui = ui_map.get(iid)
                    if not ui:
                        continue
                    inst["enabled"] = bool(ui.get("enabled", inst.get("enabled", True)))
                    inst["search_missing"] = bool(ui.get("search_missing", inst.get("search_missing", True)))
                    inst["search_cutoff_unmet"] = bool(
                        ui.get("search_cutoff_unmet", inst.get("search_cutoff_unmet", True))
                    )
                    if ui.get("search_order") is not None:
                        inst["search_order"] = (
                            str(ui.get("search_order") or inst.get("search_order") or "newest").strip().lower()
                        )
                    # Per-instance behavior/limits.
                    if ui.get("quiet_hours_start") is not None:
                        inst["quiet_hours_start"] = str(ui.get("quiet_hours_start") or "").strip()
                    if ui.get("quiet_hours_end") is not None:
                        inst["quiet_hours_end"] = str(ui.get("quiet_hours_end") or "").strip()
                    if ui.get("min_hours_after_release") is not None:
                        try:
                            inst["min_hours_after_release"] = max(0, int(ui.get("min_hours_after_release")))
                        except (TypeError, ValueError):
                            pass
                    if ui.get("min_seconds_between_actions") is not None:
                        try:
                            inst["min_seconds_between_actions"] = max(0, int(ui.get("min_seconds_between_actions")))
                        except (TypeError, ValueError):
                            pass
                    if ui.get("max_missing_actions_per_instance_per_sync") is not None:
                        try:
                            inst["max_missing_actions_per_instance_per_sync"] = max(
                                0, int(ui.get("max_missing_actions_per_instance_per_sync"))
                            )
                        except (TypeError, ValueError):
                            pass
                    if ui.get("max_cutoff_actions_per_instance_per_sync") is not None:
                        try:
                            inst["max_cutoff_actions_per_instance_per_sync"] = max(
                                0, int(ui.get("max_cutoff_actions_per_instance_per_sync"))
                            )
                        except (TypeError, ValueError):
                            pass
                    # Sonarr-only, but safe to persist in YAML for other apps (ignored by parser).
                    if ui.get("sonarr_missing_mode") is not None:
                        inst["sonarr_missing_mode"] = (
                            str(ui.get("sonarr_missing_mode") or inst.get("sonarr_missing_mode") or "season_packs")
                            .strip()
                            .lower()
                        )
                    try:
                        interval = ui.get("interval_minutes")
                        inst["interval_minutes"] = max(1, int(interval or inst.get("interval_minutes") or 15))
                    except (TypeError, ValueError):
                        pass
                    try:
                        inst["item_retry_hours"] = max(
                            1,
                            int(
                                ui.get("item_retry_hours")
                                or inst.get("item_retry_hours")
                                or raw_app.get("item_retry_hours")
                                or 72
                            ),
                        )
                    except (TypeError, ValueError):
                        pass
                    # Per-instance rate overrides.
                    try:
                        inst["rate_window_minutes"] = max(
                            1,
                            int(
                                ui.get("rate_window_minutes")
                                or inst.get("rate_window_minutes")
                                or raw_app.get("rate_window_minutes")
                                or 60
                            ),
                        )
                    except (TypeError, ValueError):
                        pass
                    try:
                        inst["rate_cap"] = max(
                            1,
                            int(
                                ui.get("rate_cap") or inst.get("rate_cap") or raw_app.get("rate_cap_per_instance") or 25
                            ),
                        )
                    except (TypeError, ValueError):
                        pass

                    arr_block = inst.get(arr_key) if isinstance(inst.get(arr_key), dict) else {}
                    arr_block["enabled"] = bool(inst.get("enabled", True))
                    url = str(ui.get("arr_url") or "").strip()
                    if url:
                        arr_block["url"] = url
                    api_key = str(ui.get("arr_api_key") or "").strip()
                    if api_key:
                        store.set_arr_api_key(app_name, iid, api_key)
                        arr_block["api_key"] = ""
                    inst[arr_key] = arr_block

                section["instances"] = instances
                raw[section_key] = section

            _update_instances("radarr", "radarr", "radarr")
            _update_instances("sonarr", "sonarr", "sonarr")

            Path(config_path).write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
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
        return "", 204

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
