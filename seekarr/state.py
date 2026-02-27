import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class StateStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._fernet: Fernet | None = None
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS processed_guid (
                    hunt_type TEXT NOT NULL,
                    instance_id INTEGER NOT NULL,
                    guid TEXT NOT NULL,
                    processed_at TEXT NOT NULL,
                    PRIMARY KEY (hunt_type, instance_id, guid)
                );
                CREATE TABLE IF NOT EXISTS item_action (
                    hunt_type TEXT NOT NULL,
                    instance_id INTEGER NOT NULL,
                    item_key TEXT NOT NULL,
                    last_action_at TEXT NOT NULL,
                    last_guid TEXT,
                    title TEXT,
                    PRIMARY KEY (hunt_type, instance_id, item_key)
                );
                CREATE TABLE IF NOT EXISTS cycle_run (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    status TEXT NOT NULL,
                    stats_json TEXT
                );
                CREATE TABLE IF NOT EXISTS sync_status (
                    hunt_type TEXT NOT NULL,
                    instance_id INTEGER NOT NULL,
                    last_sync_time TEXT,
                    next_sync_time TEXT,
                    PRIMARY KEY (hunt_type, instance_id)
                );
                CREATE TABLE IF NOT EXISTS search_event (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    hunt_type TEXT NOT NULL,
                    instance_id INTEGER NOT NULL,
                    occurred_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_search_event_lookup
                ON search_event(hunt_type, instance_id, occurred_at);
                CREATE TABLE IF NOT EXISTS search_action (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    hunt_type TEXT NOT NULL,
                    instance_id INTEGER NOT NULL,
                    instance_name TEXT,
                    item_key TEXT,
                    title TEXT NOT NULL,
                    occurred_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_search_action_lookup
                ON search_action(hunt_type, instance_id, id DESC);
                CREATE TABLE IF NOT EXISTS instance_run (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    cycle_run_id INTEGER NOT NULL,
                    hunt_type TEXT NOT NULL,
                    instance_id INTEGER NOT NULL,
                    instance_name TEXT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    status TEXT NOT NULL,
                    stats_json TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_instance_run_lookup
                ON instance_run(hunt_type, instance_id, id DESC);
                CREATE TABLE IF NOT EXISTS scheduler_heartbeat (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS arr_credentials (
                    app_type TEXT NOT NULL,
                    instance_id INTEGER NOT NULL,
                    api_key_enc TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (app_type, instance_id)
                );
                CREATE TABLE IF NOT EXISTS webui_auth (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    password_hash TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ui_app_settings (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    quiet_hours_timezone TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ui_instance_settings (
                    app_type TEXT NOT NULL,
                    instance_id INTEGER NOT NULL,
                    enabled INTEGER,
                    interval_minutes INTEGER,
                    search_missing INTEGER,
                    search_cutoff_unmet INTEGER,
                    search_order TEXT,
                    quiet_hours_start TEXT,
                    quiet_hours_end TEXT,
                    min_hours_after_release INTEGER,
                    min_seconds_between_actions INTEGER,
                    max_missing_actions_per_instance_per_sync INTEGER,
                    max_cutoff_actions_per_instance_per_sync INTEGER,
                    sonarr_missing_mode TEXT,
                    item_retry_hours INTEGER,
                    rate_window_minutes INTEGER,
                    rate_cap INTEGER,
                    arr_url TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (app_type, instance_id)
                );
                """
            )

    def get_webui_password_hash(self) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT password_hash FROM webui_auth WHERE id = 1").fetchone()
        if not row:
            return None
        value = str(row["password_hash"] or "").strip()
        return value or None

    def set_webui_password_hash(self, password_hash: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO webui_auth(id, password_hash, updated_at)
                VALUES(1, ?, ?)
                ON CONFLICT(id) DO UPDATE SET password_hash=excluded.password_hash, updated_at=excluded.updated_at
                """,
                (str(password_hash), _utc_now()),
            )

    def is_guid_processed(self, hunt_type: str, instance_id: int, guid: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM processed_guid WHERE hunt_type = ? AND instance_id = ? AND guid = ?",
                (hunt_type, instance_id, guid),
            ).fetchone()
        return row is not None

    def mark_guid_processed(self, hunt_type: str, instance_id: int, guid: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO processed_guid(hunt_type, instance_id, guid, processed_at)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(hunt_type, instance_id, guid) DO UPDATE SET processed_at=excluded.processed_at
                """,
                (hunt_type, instance_id, guid, _utc_now()),
            )

    def _key_path(self) -> Path:
        return self.db_path.parent / "seekarr.masterkey"

    def _get_fernet(self) -> Fernet:
        if self._fernet is not None:
            return self._fernet
        key_path = self._key_path()
        if key_path.exists():
            key = key_path.read_text(encoding="utf-8").strip().encode("ascii", "ignore")
        else:
            key = Fernet.generate_key()
            key_path.write_text(key.decode("ascii"), encoding="utf-8")
            try:
                key_path.chmod(0o600)
            except OSError:
                pass
        self._fernet = Fernet(key)
        return self._fernet

    def has_arr_api_key(self, app_type: str, instance_id: int) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM arr_credentials WHERE app_type = ? AND instance_id = ?",
                (str(app_type), int(instance_id)),
            ).fetchone()
        return row is not None

    def set_arr_api_key(self, app_type: str, instance_id: int, api_key: str) -> None:
        token = self._get_fernet().encrypt(str(api_key).encode("utf-8")).decode("ascii")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO arr_credentials(app_type, instance_id, api_key_enc, updated_at)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(app_type, instance_id) DO UPDATE SET api_key_enc=excluded.api_key_enc, updated_at=excluded.updated_at
                """,
                (str(app_type), int(instance_id), token, _utc_now()),
            )

    def get_arr_api_key(self, app_type: str, instance_id: int) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT api_key_enc FROM arr_credentials WHERE app_type = ? AND instance_id = ?",
                (str(app_type), int(instance_id)),
            ).fetchone()
        if not row:
            return None
        token = str(row["api_key_enc"] or "").strip()
        if not token:
            return None
        try:
            return self._get_fernet().decrypt(token.encode("ascii"), ttl=None).decode("utf-8", "ignore").strip() or None
        except (InvalidToken, ValueError):
            return None

    def clear_arr_api_key(self, app_type: str, instance_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM arr_credentials WHERE app_type = ? AND instance_id = ?",
                (str(app_type), int(instance_id)),
            )

    def get_ui_app_settings(self) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT quiet_hours_timezone FROM ui_app_settings WHERE id = 1").fetchone()
        if not row:
            return {}
        return {
            "quiet_hours_timezone": (str(row["quiet_hours_timezone"]).strip() if row["quiet_hours_timezone"] else ""),
        }

    def set_ui_app_settings(self, quiet_hours_timezone: str | None = None) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO ui_app_settings(id, quiet_hours_timezone, updated_at)
                VALUES(1, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    quiet_hours_timezone=excluded.quiet_hours_timezone,
                    updated_at=excluded.updated_at
                """,
                (str(quiet_hours_timezone or "").strip(), _utc_now()),
            )

    def upsert_ui_instance_settings(self, app_type: str, instance_id: int, values: dict[str, Any]) -> None:
        if str(app_type).strip().lower() not in ("radarr", "sonarr"):
            raise ValueError("Invalid app_type")
        iid = int(instance_id)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO ui_instance_settings(
                    app_type, instance_id,
                    enabled, interval_minutes, search_missing, search_cutoff_unmet, search_order,
                    quiet_hours_start, quiet_hours_end,
                    min_hours_after_release, min_seconds_between_actions,
                    max_missing_actions_per_instance_per_sync, max_cutoff_actions_per_instance_per_sync,
                    sonarr_missing_mode, item_retry_hours, rate_window_minutes, rate_cap, arr_url, updated_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(app_type, instance_id) DO UPDATE SET
                    enabled=excluded.enabled,
                    interval_minutes=excluded.interval_minutes,
                    search_missing=excluded.search_missing,
                    search_cutoff_unmet=excluded.search_cutoff_unmet,
                    search_order=excluded.search_order,
                    quiet_hours_start=excluded.quiet_hours_start,
                    quiet_hours_end=excluded.quiet_hours_end,
                    min_hours_after_release=excluded.min_hours_after_release,
                    min_seconds_between_actions=excluded.min_seconds_between_actions,
                    max_missing_actions_per_instance_per_sync=excluded.max_missing_actions_per_instance_per_sync,
                    max_cutoff_actions_per_instance_per_sync=excluded.max_cutoff_actions_per_instance_per_sync,
                    sonarr_missing_mode=excluded.sonarr_missing_mode,
                    item_retry_hours=excluded.item_retry_hours,
                    rate_window_minutes=excluded.rate_window_minutes,
                    rate_cap=excluded.rate_cap,
                    arr_url=excluded.arr_url,
                    updated_at=excluded.updated_at
                """,
                (
                    str(app_type).strip().lower(),
                    iid,
                    values.get("enabled"),
                    values.get("interval_minutes"),
                    values.get("search_missing"),
                    values.get("search_cutoff_unmet"),
                    values.get("search_order"),
                    values.get("quiet_hours_start"),
                    values.get("quiet_hours_end"),
                    values.get("min_hours_after_release"),
                    values.get("min_seconds_between_actions"),
                    values.get("max_missing_actions_per_instance_per_sync"),
                    values.get("max_cutoff_actions_per_instance_per_sync"),
                    values.get("sonarr_missing_mode"),
                    values.get("item_retry_hours"),
                    values.get("rate_window_minutes"),
                    values.get("rate_cap"),
                    values.get("arr_url"),
                    _utc_now(),
                ),
            )

    def get_all_ui_instance_settings(self) -> dict[tuple[str, int], dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    app_type, instance_id,
                    enabled, interval_minutes, search_missing, search_cutoff_unmet, search_order,
                    quiet_hours_start, quiet_hours_end,
                    min_hours_after_release, min_seconds_between_actions,
                    max_missing_actions_per_instance_per_sync, max_cutoff_actions_per_instance_per_sync,
                    sonarr_missing_mode, item_retry_hours, rate_window_minutes, rate_cap, arr_url
                FROM ui_instance_settings
                """
            ).fetchall()
        out: dict[tuple[str, int], dict[str, Any]] = {}
        for row in rows:
            app_type = str(row["app_type"] or "").strip().lower()
            try:
                instance_id = int(row["instance_id"])
            except (TypeError, ValueError):
                continue
            out[(app_type, instance_id)] = {
                "enabled": row["enabled"],
                "interval_minutes": row["interval_minutes"],
                "search_missing": row["search_missing"],
                "search_cutoff_unmet": row["search_cutoff_unmet"],
                "search_order": row["search_order"],
                "quiet_hours_start": row["quiet_hours_start"],
                "quiet_hours_end": row["quiet_hours_end"],
                "min_hours_after_release": row["min_hours_after_release"],
                "min_seconds_between_actions": row["min_seconds_between_actions"],
                "max_missing_actions_per_instance_per_sync": row["max_missing_actions_per_instance_per_sync"],
                "max_cutoff_actions_per_instance_per_sync": row["max_cutoff_actions_per_instance_per_sync"],
                "sonarr_missing_mode": row["sonarr_missing_mode"],
                "item_retry_hours": row["item_retry_hours"],
                "rate_window_minutes": row["rate_window_minutes"],
                "rate_cap": row["rate_cap"],
                "arr_url": row["arr_url"],
            }
        return out

    def prune_old_guids(self, ttl_hours: int) -> int:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=ttl_hours)
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM processed_guid WHERE processed_at < ?",
                (cutoff.isoformat(),),
            )
            return cur.rowcount

    def item_on_cooldown(self, hunt_type: str, instance_id: int, item_key: str, retry_hours: int) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT last_action_at FROM item_action WHERE hunt_type = ? AND instance_id = ? AND item_key = ?",
                (hunt_type, instance_id, item_key),
            ).fetchone()
        if row is None:
            return False
        try:
            last = datetime.fromisoformat(row["last_action_at"])
        except ValueError:
            return False
        return datetime.now(timezone.utc) < (last + timedelta(hours=retry_hours))

    def mark_item_action(self, hunt_type: str, instance_id: int, item_key: str, guid: str, title: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO item_action(hunt_type, instance_id, item_key, last_action_at, last_guid, title)
                VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(hunt_type, instance_id, item_key) DO UPDATE SET
                    last_action_at=excluded.last_action_at,
                    last_guid=excluded.last_guid,
                    title=excluded.title
                """,
                (hunt_type, instance_id, item_key, _utc_now(), guid, title),
            )

    def get_next_sync_time(self, hunt_type: str, instance_id: int) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT next_sync_time FROM sync_status WHERE hunt_type = ? AND instance_id = ?",
                (hunt_type, instance_id),
            ).fetchone()
        return str(row["next_sync_time"]) if row and row["next_sync_time"] else None

    def upsert_sync_status(self, hunt_type: str, instance_id: int, last_sync_time: str, next_sync_time: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sync_status(hunt_type, instance_id, last_sync_time, next_sync_time)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(hunt_type, instance_id) DO UPDATE SET
                    last_sync_time=excluded.last_sync_time,
                    next_sync_time=excluded.next_sync_time
                """,
                (hunt_type, instance_id, last_sync_time, next_sync_time),
            )

    def set_next_sync_time(self, hunt_type: str, instance_id: int, next_sync_time: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sync_status(hunt_type, instance_id, next_sync_time)
                VALUES(?, ?, ?)
                ON CONFLICT(hunt_type, instance_id) DO UPDATE SET
                    next_sync_time=excluded.next_sync_time
                """,
                (hunt_type, instance_id, next_sync_time),
            )

    def record_search_event(self, hunt_type: str, instance_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO search_event(hunt_type, instance_id, occurred_at) VALUES(?, ?, ?)",
                (hunt_type, instance_id, _utc_now()),
            )

    def record_search_action(
        self,
        hunt_type: str,
        instance_id: int,
        instance_name: str,
        item_key: str,
        title: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO search_action(hunt_type, instance_id, instance_name, item_key, title, occurred_at)
                VALUES(?, ?, ?, ?, ?, ?)
                """,
                (
                    str(hunt_type),
                    int(instance_id),
                    str(instance_name or ""),
                    str(item_key or ""),
                    str(title or ""),
                    _utc_now(),
                ),
            )

    def get_recent_search_actions(self, hunt_type: str, instance_id: int, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, hunt_type, instance_id, instance_name, item_key, title, occurred_at
                FROM search_action
                WHERE hunt_type = ? AND instance_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (str(hunt_type), int(instance_id), max(1, int(limit))),
            ).fetchall()
        return [
            {
                "id": int(r["id"]),
                "app_type": r["hunt_type"],
                "instance_id": int(r["instance_id"]),
                "instance_name": r["instance_name"],
                "item_key": r["item_key"],
                "title": r["title"],
                "occurred_at": r["occurred_at"],
            }
            for r in rows
        ]

    def get_recent_search_actions_global(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, hunt_type, instance_id, instance_name, item_key, title, occurred_at
                FROM search_action
                ORDER BY id DESC
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        return [
            {
                "id": int(r["id"]),
                "app_type": r["hunt_type"],
                "instance_id": int(r["instance_id"]),
                "instance_name": r["instance_name"],
                "item_key": r["item_key"],
                "title": r["title"],
                "occurred_at": r["occurred_at"],
            }
            for r in rows
        ]

    def count_search_events_since(self, hunt_type: str, instance_id: int, since_iso: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS c
                FROM search_event
                WHERE hunt_type = ? AND instance_id = ? AND occurred_at >= ?
                """,
                (hunt_type, instance_id, since_iso),
            ).fetchone()
        return int(row["c"] or 0) if row else 0

    def count_search_actions_for_item(self, hunt_type: str, instance_id: int, item_key: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS c
                FROM search_action
                WHERE hunt_type = ? AND instance_id = ? AND item_key = ?
                """,
                (str(hunt_type), int(instance_id), str(item_key)),
            ).fetchone()
        return int(row["c"] or 0) if row else 0

    def set_scheduler_heartbeat(self) -> None:
        now = _utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO scheduler_heartbeat(id, updated_at)
                VALUES(1, ?)
                ON CONFLICT(id) DO UPDATE SET
                    updated_at=excluded.updated_at
                """,
                (now,),
            )

    def get_scheduler_heartbeat(self) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT updated_at FROM scheduler_heartbeat WHERE id = 1").fetchone()
        if not row:
            return None
        updated_at = row["updated_at"]
        return str(updated_at) if updated_at else None

    def start_run(self) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO cycle_run(started_at, status) VALUES(?, ?)",
                (_utc_now(), "running"),
            )
            return int(cur.lastrowid)

    def finish_run(self, run_id: int, status: str, stats: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE cycle_run
                SET finished_at = ?, status = ?, stats_json = ?
                WHERE id = ?
                """,
                (_utc_now(), status, json.dumps(stats), run_id),
            )

    def get_recent_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, started_at, finished_at, status, stats_json
                FROM cycle_run
                ORDER BY id DESC
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            stats = {}
            try:
                stats = json.loads(row["stats_json"] or "{}")
            except (TypeError, ValueError):
                stats = {}
            out.append(
                {
                    "id": int(row["id"]),
                    "started_at": row["started_at"],
                    "finished_at": row["finished_at"],
                    "status": row["status"],
                    "stats": stats,
                }
            )
        return out

    def record_instance_run(
        self,
        cycle_run_id: int,
        hunt_type: str,
        instance_id: int,
        instance_name: str,
        started_at: str,
        finished_at: str,
        status: str,
        stats: dict[str, Any],
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO instance_run(
                    cycle_run_id, hunt_type, instance_id, instance_name,
                    started_at, finished_at, status, stats_json
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(cycle_run_id),
                    str(hunt_type),
                    int(instance_id),
                    str(instance_name or ""),
                    str(started_at),
                    str(finished_at),
                    str(status),
                    json.dumps(stats or {}),
                ),
            )

    def get_recent_instance_runs(self, hunt_type: str, instance_id: int, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, cycle_run_id, hunt_type, instance_id, instance_name, started_at, finished_at, status, stats_json
                FROM instance_run
                WHERE hunt_type = ? AND instance_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (str(hunt_type), int(instance_id), max(1, int(limit))),
            ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            stats = {}
            try:
                stats = json.loads(row["stats_json"] or "{}")
            except (TypeError, ValueError):
                stats = {}
            out.append(
                {
                    "id": int(row["id"]),
                    "cycle_run_id": int(row["cycle_run_id"]),
                    "app_type": row["hunt_type"],
                    "instance_id": int(row["instance_id"]),
                    "instance_name": row["instance_name"],
                    "started_at": row["started_at"],
                    "finished_at": row["finished_at"],
                    "status": row["status"],
                    "stats": stats,
                }
            )
        return out

    def get_last_instance_run(self, hunt_type: str, instance_id: int) -> dict[str, Any] | None:
        rows = self.get_recent_instance_runs(hunt_type, instance_id, limit=1)
        return rows[0] if rows else None

    def get_sync_statuses(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT hunt_type, instance_id, last_sync_time, next_sync_time
                FROM sync_status
                ORDER BY hunt_type, instance_id
                """
            ).fetchall()
        return [
            {
                "app_type": row["hunt_type"],
                "instance_id": int(row["instance_id"]),
                "last_sync_time": row["last_sync_time"],
                "next_sync_time": row["next_sync_time"],
            }
            for row in rows
        ]
