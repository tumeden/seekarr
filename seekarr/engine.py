import logging
import random
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .arr import (
    ArrClient,
)
from .config import ArrConfig, ArrSyncInstanceConfig, RuntimeConfig
from .state import StateStore

RECENT_PRIORITY_WINDOW_DAYS = 2
RECENT_RETRY_HOURS = 6


def _parse_arr_datetime_utc(value: Any) -> datetime | None:
    """Parse Arr date strings into a timezone-aware UTC datetime (best effort)."""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    # Common: "2026-02-24T01:23:45Z"
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    # Common: "2026-02-24" (date-only)
    if len(s) == 10 and s.count("-") == 2:
        try:
            dt = datetime.fromisoformat(s + "T00:00:00+00:00")
            return dt.astimezone(timezone.utc)
        except ValueError:
            return None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_hhmm(value: str) -> tuple[int, int] | None:
    s = (value or "").strip()
    if not s:
        return None
    parts = s.split(":")
    if len(parts) != 2:
        return None
    try:
        hh = int(parts[0])
        mm = int(parts[1])
    except ValueError:
        return None
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return None
    return hh, mm


def _quiet_hours_end_utc(now_utc: datetime, start_hhmm: str, end_hhmm: str) -> datetime | None:
    """
    If now is within quiet hours (in local timezone), return the end datetime in UTC.
    Quiet hours are inclusive of the start instant and exclusive of the end instant.
    Supports windows that wrap midnight (e.g. 23:00 -> 06:00).
    """
    st = _parse_hhmm(start_hhmm)
    et = _parse_hhmm(end_hhmm)
    if not st or not et:
        return None

    local_now = now_utc.astimezone()  # system local timezone
    start_local_today = local_now.replace(hour=st[0], minute=st[1], second=0, microsecond=0)
    end_local_today = local_now.replace(hour=et[0], minute=et[1], second=0, microsecond=0)

    if start_local_today < end_local_today:
        in_window = start_local_today <= local_now < end_local_today
        end_local = end_local_today
    else:
        if local_now >= start_local_today:
            end_local = end_local_today + timedelta(days=1)
            in_window = True
        elif local_now < end_local_today:
            end_local = end_local_today
            in_window = True
        else:
            in_window = False
            end_local = end_local_today

    if not in_window:
        return None
    return end_local.astimezone(timezone.utc)


@dataclass
class CycleStats:
    instances_due: int = 0
    instances_processed: int = 0
    wanted_total: int = 0
    actions_triggered: int = 0
    actions_skipped_cooldown: int = 0
    actions_skipped_rate_limit: int = 0
    actions_skipped_not_released: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "instances_due": self.instances_due,
            "instances_processed": self.instances_processed,
            "wanted_total": self.wanted_total,
            "actions_triggered": self.actions_triggered,
            "actions_skipped_cooldown": self.actions_skipped_cooldown,
            "actions_skipped_rate_limit": self.actions_skipped_rate_limit,
            "actions_skipped_not_released": self.actions_skipped_not_released,
        }


class Engine:
    def __init__(self, config: RuntimeConfig, logger: logging.Logger) -> None:
        self.config = config
        self.logger = logger
        self.store = StateStore(config.app.db_path)
        # Shared pacing across instances (Radarr + Sonarr). External schedulers already
        # serialize runs with a global lock, but this ensures actions can't bunch up
        # when due instances run back-to-back.
        self._pacer_lock = threading.Lock()
        self._last_action_at = 0.0  # time.monotonic()

    def _wait_pace(self, seconds: int) -> None:
        if int(seconds) <= 0:
            return
        with self._pacer_lock:
            now = time.monotonic()
            wait = (self._last_action_at + float(seconds)) - now
            if wait > 0:
                time.sleep(wait)

    def _mark_action(self) -> None:
        with self._pacer_lock:
            self._last_action_at = time.monotonic()

    def _is_recent_release(self, app_type: str, release_iso: Any, now: datetime | None = None) -> bool:
        if app_type not in ("radarr", "sonarr"):
            return False
        dt = _parse_arr_datetime_utc(release_iso)
        if not dt:
            return False
        now_utc = now or datetime.now(timezone.utc)
        return (now_utc - timedelta(days=RECENT_PRIORITY_WINDOW_DAYS)) <= dt <= now_utc

    def _find_instance(self, app_type: str, instance_id: int) -> ArrSyncInstanceConfig | None:
        if app_type == "radarr":
            for inst in self.config.radarr_instances:
                if int(inst.instance_id) == int(instance_id):
                    return inst
            return None
        if app_type == "sonarr":
            for inst in self.config.sonarr_instances:
                if int(inst.instance_id) == int(instance_id):
                    return inst
            return None
        return None

    def run_instance(
        self,
        app_type: str,
        instance_id: int,
        force: bool = True,
        progress_cb: Callable[[dict[str, Any]], None] | None = None,
    ) -> CycleStats:
        """
        Run a single instance sync. Used by the Web UI "Force Run" button.
        If force=False, this respects due-time and will no-op when not due.
        """
        inst = self._find_instance(app_type, int(instance_id))
        if not inst:
            raise ValueError(f"Unknown instance: {app_type}:{instance_id}")
        if not inst.enabled or not inst.arr.enabled:
            return CycleStats()
        if (not force) and (not self._is_due(app_type, inst)):
            return CycleStats()

        stats = CycleStats()
        run_id = self.store.start_run()
        try:
            stats.instances_due = 1
            if progress_cb:
                progress_cb({"type": "cycle_started", "force": force, "instances_due": 1})
                progress_cb(
                    {
                        "type": "instance_started",
                        "app_type": app_type,
                        "instance_id": inst.instance_id,
                        "instance_name": inst.instance_name,
                    }
                )
            self._run_instance_sync(run_id, app_type, inst, stats, force=force, progress_cb=progress_cb)
            stats.instances_processed = 1
            self.store.finish_run(run_id, "success", stats.as_dict())
            if progress_cb:
                progress_cb({"type": "cycle_finished", "status": "success", "stats": stats.as_dict()})
            return stats
        except Exception as exc:
            self.store.finish_run(run_id, "error", {"error": str(exc), **stats.as_dict()})
            if progress_cb:
                progress_cb({"type": "cycle_finished", "status": "error", "error": str(exc), "stats": stats.as_dict()})
            raise

    def _is_due(self, app_type: str, instance: ArrSyncInstanceConfig) -> bool:
        if not instance.enabled:
            return False
        next_sync = self.store.get_next_sync_time(app_type, instance.instance_id)
        if not next_sync:
            return True
        try:
            dt = datetime.fromisoformat(next_sync)
        except ValueError:
            return True
        return datetime.now(timezone.utc) >= dt

    def run_cycle(
        self,
        force: bool = False,
        progress_cb: Callable[[dict[str, Any]], None] | None = None,
    ) -> CycleStats:
        stats = CycleStats()
        run_id = self.store.start_run()
        try:
            due_instances: list[tuple[str, ArrSyncInstanceConfig]] = []
            for inst in self.config.radarr_instances:
                if force:
                    if inst.enabled:
                        due_instances.append(("radarr", inst))
                elif self._is_due("radarr", inst):
                    due_instances.append(("radarr", inst))
            for inst in self.config.sonarr_instances:
                if force:
                    if inst.enabled:
                        due_instances.append(("sonarr", inst))
                elif self._is_due("sonarr", inst):
                    due_instances.append(("sonarr", inst))
            stats.instances_due = len(due_instances)
            if progress_cb:
                progress_cb(
                    {
                        "type": "cycle_started",
                        "force": force,
                        "instances_due": stats.instances_due,
                    }
                )

            for app_type, instance in due_instances:
                if progress_cb:
                    progress_cb(
                        {
                            "type": "instance_started",
                            "app_type": app_type,
                            "instance_id": instance.instance_id,
                            "instance_name": instance.instance_name,
                        }
                    )
                self._run_instance_sync(run_id, app_type, instance, stats, force=force, progress_cb=progress_cb)
                stats.instances_processed += 1
                if progress_cb:
                    progress_cb(
                        {
                            "type": "instance_finished",
                            "app_type": app_type,
                            "instance_id": instance.instance_id,
                            "instance_name": instance.instance_name,
                            "actions_triggered": stats.actions_triggered,
                            "actions_skipped_cooldown": stats.actions_skipped_cooldown,
                            "wanted_total": stats.wanted_total,
                        }
                    )

            self.store.finish_run(run_id, "success", stats.as_dict())
            if progress_cb:
                progress_cb({"type": "cycle_finished", "status": "success", "stats": stats.as_dict()})
            return stats
        except Exception as exc:
            self.store.finish_run(run_id, "error", {"error": str(exc), **stats.as_dict()})
            if progress_cb:
                progress_cb({"type": "cycle_finished", "status": "error", "error": str(exc), "stats": stats.as_dict()})
            raise

    def _run_instance_sync(
        self,
        cycle_run_id: int,
        app_type: str,
        instance: ArrSyncInstanceConfig,
        stats: CycleStats,
        force: bool = False,
        progress_cb: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        instance_started_at = datetime.now(timezone.utc).isoformat()
        before_actions = stats.actions_triggered
        before_cooldown = stats.actions_skipped_cooldown
        before_rate = stats.actions_skipped_rate_limit
        before_not_released = stats.actions_skipped_not_released
        interval = max(15, min(60, int(instance.interval_minutes)))
        retry_hours = int(instance.item_retry_hours or self.config.app.item_retry_hours)
        rate_window_minutes = int(instance.rate_window_minutes or self.config.app.rate_window_minutes)
        rate_cap = int(instance.rate_cap or self.config.app.rate_cap_per_instance)
        api_key = self.store.get_arr_api_key(app_type, instance.instance_id) or instance.arr.api_key
        arr_cfg = ArrConfig(enabled=instance.arr.enabled, url=instance.arr.url, api_key=api_key)
        client = ArrClient(
            name=app_type,
            config=arr_cfg,
            timeout_seconds=self.config.app.request_timeout_seconds,
            verify_ssl=self.config.app.verify_ssl,
            logger=self.logger,
        )
        if not instance.arr.enabled:
            self._update_sync_status(app_type, instance, interval)
            return

        wanted_count = 0
        status = "success"
        try:
            quiet_start = str(getattr(instance, "quiet_hours_start", None) or self.config.app.quiet_hours_start or "")
            quiet_end = str(getattr(instance, "quiet_hours_end", None) or self.config.app.quiet_hours_end or "")
            quiet_end_utc = _quiet_hours_end_utc(datetime.now(timezone.utc), quiet_start, quiet_end)
            if quiet_end_utc and (not force):
                self.store.set_next_sync_time(app_type, instance.instance_id, quiet_end_utc.isoformat())
                status = "quiet_hours"
                return

            wanted = (
                client.fetch_wanted_movies(
                    search_missing=bool(getattr(instance, "search_missing", True)),
                    search_cutoff_unmet=bool(getattr(instance, "search_cutoff_unmet", True)),
                )
                if app_type == "radarr"
                else client.fetch_wanted_episodes(
                    search_missing=bool(getattr(instance, "search_missing", True)),
                    search_cutoff_unmet=bool(getattr(instance, "search_cutoff_unmet", True)),
                )
            )
            # Specials (Season 00) are absolute lowest priority. Only process them when there is
            # nothing else (non-special wanted episodes) to do for this instance.
            if app_type == "sonarr":
                non_special = [w for w in wanted if getattr(w, "season_number", 0) != 0]
                if non_special:
                    wanted = non_special

            # Priority:
            # 1) missing items first (new content)
            # 2) then cutoff-unmet items (upgrades)
            # 3) within each group: newest (closest to release/air date) first
            # Items without a known date are processed last.
            if app_type == "radarr":

                def _date_key(item) -> tuple[int, float]:
                    dt = _parse_arr_datetime_utc(getattr(item, "release_date_utc", None))
                    return (1, dt.timestamp()) if dt else (0, 0.0)
            else:

                def _date_key(item) -> tuple[int, float]:
                    dt = _parse_arr_datetime_utc(getattr(item, "air_date_utc", None))
                    return (1, dt.timestamp()) if dt else (0, 0.0)

            missing_items = [w for w in wanted if str(getattr(w, "wanted_kind", "missing")).lower() == "missing"]
            cutoff_items = [w for w in wanted if str(getattr(w, "wanted_kind", "missing")).lower() != "missing"]
            search_order = str(getattr(instance, "search_order", "newest") or "newest").strip().lower()
            if search_order not in ("smart", "newest", "oldest", "random"):
                search_order = "newest"

            now_utc = datetime.now(timezone.utc)
            radarr_calendar_movie_ids: set[int] = set()
            radarr_calendar_boost_ts: dict[int, float] = {}
            sonarr_calendar_episode_ids: set[int] = set()
            sonarr_calendar_triples: set[tuple[int, int, int]] = set()
            sonarr_calendar_boost_ts: dict[tuple[int, int, int], float] = {}
            if app_type == "radarr" and search_order == "smart":
                try:
                    calendar_rows = client.fetch_calendar(
                        start=(now_utc - timedelta(days=3)).date(),
                        end=(now_utc + timedelta(days=1)).date(),
                    )
                    for row in calendar_rows:
                        movie_id = int(row.get("id") or row.get("movieId") or 0)
                        release_iso = (
                            row.get("digitalRelease")
                            or row.get("physicalRelease")
                            or row.get("inCinemas")
                            or row.get("releaseDate")
                        )
                        release_dt = _parse_arr_datetime_utc(release_iso)
                        # Calendar boost only for already-released content near "now".
                        if not release_dt or release_dt > now_utc:
                            continue
                        if movie_id > 0:
                            radarr_calendar_movie_ids.add(movie_id)
                            radarr_calendar_boost_ts[movie_id] = release_dt.timestamp()
                except Exception:
                    # Best-effort only: smart order still works without calendar.
                    pass
            if app_type == "sonarr" and search_order == "smart":
                try:
                    calendar_rows = client.fetch_calendar(
                        start=(now_utc - timedelta(days=3)).date(),
                        end=(now_utc + timedelta(days=1)).date(),
                    )
                    for row in calendar_rows:
                        sid = int(row.get("seriesId") or 0)
                        sn = int(row.get("seasonNumber") or 0)
                        en = int(row.get("episodeNumber") or 0)
                        eid = int(row.get("id") or row.get("episodeId") or 0)
                        air_iso = row.get("airDateUtc") or row.get("airDate")
                        air_dt = _parse_arr_datetime_utc(air_iso)
                        # Calendar boost only for already-aired content near "now".
                        if not air_dt or air_dt > now_utc:
                            continue
                        if eid > 0:
                            sonarr_calendar_episode_ids.add(eid)
                        if sid > 0 and en > 0:
                            key = (sid, sn, en)
                            sonarr_calendar_triples.add(key)
                            sonarr_calendar_boost_ts[key] = air_dt.timestamp()
                except Exception:
                    # Best-effort only: smart order still works without calendar.
                    pass

            def _dt(item) -> datetime | None:
                # Needed for "smart" ordering.
                if app_type == "radarr":
                    return _parse_arr_datetime_utc(getattr(item, "release_date_utc", None))
                return _parse_arr_datetime_utc(getattr(item, "air_date_utc", None))

            def _smart_order(items: list[Any]) -> list[Any]:
                """
                Smart ordering:
                - calendar-near already-released items first
                - recent (today/yesterday): newest-first
                - then random for the bulk of remaining dated items
                - then oldest dated items last (oldest-first within the tail)
                - unknown dates absolute last
                """
                recent_cutoff = now_utc - timedelta(days=2)
                calendar_boosted: list[tuple[float, Any]] = []
                candidate_items: list[Any] = []

                for it in items:
                    if app_type == "radarr":
                        movie_id = int(getattr(it, "movie_id", 0) or 0)
                        if movie_id in radarr_calendar_movie_ids:
                            ts = radarr_calendar_boost_ts.get(movie_id)
                            if ts is None:
                                dt = _dt(it)
                                ts = dt.timestamp() if dt else 0.0
                            calendar_boosted.append((ts, it))
                        else:
                            candidate_items.append(it)
                        continue
                    if app_type != "sonarr":
                        candidate_items.append(it)
                        continue
                    episode_id = int(getattr(it, "episode_id", 0) or 0)
                    sid = int(getattr(it, "series_id", 0) or 0)
                    sn = int(getattr(it, "season_number", 0) or 0)
                    en = int(getattr(it, "episode_number", 0) or 0)
                    triple = (sid, sn, en)
                    if episode_id in sonarr_calendar_episode_ids or triple in sonarr_calendar_triples:
                        ts = sonarr_calendar_boost_ts.get(triple)
                        if ts is None:
                            dt = _dt(it)
                            ts = dt.timestamp() if dt else 0.0
                        calendar_boosted.append((ts, it))
                    else:
                        candidate_items.append(it)

                calendar_boosted.sort(key=lambda x: x[0], reverse=True)

                recent: list[tuple[datetime, Any]] = []
                dated_rest: list[tuple[datetime, Any]] = []
                unknown: list[Any] = []
                for it in candidate_items:
                    dt = _dt(it)
                    if not dt:
                        unknown.append(it)
                        continue
                    if dt >= recent_cutoff:
                        recent.append((dt, it))
                    else:
                        dated_rest.append((dt, it))

                recent.sort(key=lambda x: x[0], reverse=True)
                # Identify an "oldest tail" (10% of remaining dated items, at least 1 when non-empty).
                dated_rest.sort(key=lambda x: x[0])  # oldest -> newest
                tail_n = 0
                if dated_rest:
                    tail_n = max(1, int(len(dated_rest) * 0.10))
                oldest_tail = dated_rest[:tail_n]
                middle = dated_rest[tail_n:]
                random.shuffle(middle)

                out: list[Any] = [it for _, it in calendar_boosted]
                out.extend([it for _, it in recent])
                out.extend([it for _, it in middle])
                out.extend([it for _, it in oldest_tail])
                out.extend(unknown)
                return out

            if search_order == "smart":
                missing_items = _smart_order(missing_items)
                cutoff_items = _smart_order(cutoff_items)
            elif search_order == "random":
                random.shuffle(missing_items)
                random.shuffle(cutoff_items)
            else:
                reverse = search_order == "newest"
                missing_items.sort(key=_date_key, reverse=reverse)
                cutoff_items.sort(key=_date_key, reverse=reverse)
            wanted = missing_items + cutoff_items
            wanted_count = len(wanted)
            stats.wanted_total += wanted_count

            triggered_items: set[str] = set()
            effective_min_hours_after_release = int(
                getattr(instance, "min_hours_after_release", None)
                if getattr(instance, "min_hours_after_release", None) is not None
                else self.config.app.min_hours_after_release
            )
            effective_between_actions = int(
                getattr(instance, "min_seconds_between_actions", None)
                if getattr(instance, "min_seconds_between_actions", None) is not None
                else self.config.app.min_seconds_between_actions
            )
            recent_retry_hours = min(int(retry_hours), RECENT_RETRY_HOURS)
            missing_cap = int(
                getattr(instance, "max_missing_actions_per_instance_per_sync", None)
                if getattr(instance, "max_missing_actions_per_instance_per_sync", None) is not None
                else getattr(self.config.app, "max_missing_actions_per_instance_per_sync", 0)
            )
            cutoff_cap = int(
                getattr(instance, "max_cutoff_actions_per_instance_per_sync", None)
                if getattr(instance, "max_cutoff_actions_per_instance_per_sync", None) is not None
                else getattr(self.config.app, "max_cutoff_actions_per_instance_per_sync", 0)
            )
            # Split caps are authoritative (missing vs upgrades).

            triggered_missing = 0
            triggered_cutoff = 0

            sonarr_missing_mode = str(getattr(instance, "sonarr_missing_mode", "smart") or "smart").strip().lower()
            if sonarr_missing_mode in ("seasons_packs", "seasonpacks", "seasons", "season"):
                sonarr_missing_mode = "season_packs"
            if sonarr_missing_mode in ("hybrid", "auto"):
                sonarr_missing_mode = "smart"

            next_eligible_wakeup_utc: datetime | None = None
            if app_type == "sonarr" and effective_min_hours_after_release > 0 and missing_items:
                now_utc = datetime.now(timezone.utc)
                recent_floor = now_utc - timedelta(days=RECENT_PRIORITY_WINDOW_DAYS)
                for ep in missing_items:
                    air_dt = _parse_arr_datetime_utc(getattr(ep, "air_date_utc", None))
                    if not air_dt:
                        continue
                    if air_dt < recent_floor or air_dt > now_utc:
                        continue
                    eligible_dt = air_dt + timedelta(hours=effective_min_hours_after_release)
                    if eligible_dt <= now_utc:
                        continue
                    if (next_eligible_wakeup_utc is None) or (eligible_dt < next_eligible_wakeup_utc):
                        next_eligible_wakeup_utc = eligible_dt
            if app_type == "radarr" and effective_min_hours_after_release > 0 and missing_items:
                now_utc = datetime.now(timezone.utc)
                recent_floor = now_utc - timedelta(days=RECENT_PRIORITY_WINDOW_DAYS)
                for mv in missing_items:
                    release_dt = _parse_arr_datetime_utc(getattr(mv, "release_date_utc", None))
                    if not release_dt:
                        continue
                    if release_dt < recent_floor or release_dt > now_utc:
                        continue
                    eligible_dt = release_dt + timedelta(hours=effective_min_hours_after_release)
                    if eligible_dt <= now_utc:
                        continue
                    if (next_eligible_wakeup_utc is None) or (eligible_dt < next_eligible_wakeup_utc):
                        next_eligible_wakeup_utc = eligible_dt

            def _process(items: list[Any], cap: int, kind: str) -> None:
                nonlocal triggered_missing, triggered_cutoff
                if cap <= 0:
                    return
                for it in items:
                    if kind == "missing" and triggered_missing >= cap:
                        return
                    if kind == "cutoff" and triggered_cutoff >= cap:
                        return

                    before = stats.actions_triggered
                    self._handle_wanted_item(
                        app_type=app_type,
                        instance=instance,
                        retry_hours=retry_hours,
                        recent_retry_hours=recent_retry_hours,
                        rate_window_minutes=rate_window_minutes,
                        rate_cap=rate_cap,
                        min_hours_after_release=effective_min_hours_after_release,
                        pace_seconds=effective_between_actions,
                        wanted_item=it,
                        client=client,
                        triggered_items=triggered_items,
                        stats=stats,
                        progress_cb=progress_cb,
                    )
                    if stats.actions_triggered > before:
                        if kind == "missing":
                            triggered_missing += 1
                        else:
                            triggered_cutoff += 1
                        # Pacing is enforced inside the handler so it's shared across instances.

            if (
                app_type == "sonarr"
                and missing_items
                and missing_cap > 0
                and sonarr_missing_mode in ("season_packs", "shows", "smart")
            ):
                if sonarr_missing_mode in ("season_packs", "smart"):
                    # Group by series + season and trigger SeasonSearch. This matches Huntarr's efficient default.
                    groups: dict[tuple[int, int], list[Any]] = {}
                    for ep in missing_items:
                        sid = int(getattr(ep, "series_id", 0) or 0)
                        sn = int(getattr(ep, "season_number", 0) or 0)
                        if sid <= 0:
                            continue
                        groups.setdefault((sid, sn), []).append(ep)

                    # Specials (S00) remain lowest priority across missing.
                    non_special_keys = [k for k in groups.keys() if k[1] != 0]
                    if non_special_keys:
                        groups = {k: v for k, v in groups.items() if k[1] != 0}

                    grouped = list(groups.items())
                    if search_order == "smart":
                        # Use the newest episode air date in the season to bucket recent/random/oldest.
                        recent_cutoff = now_utc - timedelta(days=2)
                        calendar_g: list[tuple[float, Any]] = []
                        recent_g: list[tuple[datetime, Any]] = []
                        rest_g: list[tuple[datetime, Any]] = []
                        unknown_g: list[Any] = []
                        for g in grouped:
                            _, eps = g
                            cal_ts = None
                            for e in eps:
                                sid = int(getattr(e, "series_id", 0) or 0)
                                sn = int(getattr(e, "season_number", 0) or 0)
                                en = int(getattr(e, "episode_number", 0) or 0)
                                triple = (sid, sn, en)
                                if triple in sonarr_calendar_triples:
                                    ts = sonarr_calendar_boost_ts.get(triple, 0.0)
                                    cal_ts = ts if cal_ts is None or ts > cal_ts else cal_ts
                            if cal_ts is not None:
                                calendar_g.append((cal_ts, g))
                                continue
                            newest = None
                            for e in eps:
                                dt = _parse_arr_datetime_utc(getattr(e, "air_date_utc", None))
                                if dt and (newest is None or dt > newest):
                                    newest = dt
                            if not newest:
                                unknown_g.append(g)
                            elif newest >= recent_cutoff:
                                recent_g.append((newest, g))
                            else:
                                rest_g.append((newest, g))
                        calendar_g.sort(key=lambda x: x[0], reverse=True)
                        recent_g.sort(key=lambda x: x[0], reverse=True)
                        rest_g.sort(key=lambda x: x[0])  # oldest -> newest
                        tail_n = max(1, int(len(rest_g) * 0.10)) if rest_g else 0
                        oldest_tail = rest_g[:tail_n]
                        middle = rest_g[tail_n:]
                        random.shuffle(middle)
                        grouped = (
                            [g for _, g in calendar_g]
                            + [g for _, g in recent_g]
                            + [g for _, g in middle]
                            + [g for _, g in oldest_tail]
                            + unknown_g
                        )
                    elif search_order == "random":
                        random.shuffle(grouped)
                    else:

                        def _group_sort_key(item: tuple[tuple[int, int], list[Any]]) -> tuple[int, float, int]:
                            _, eps = item
                            best = None
                            for e in eps:
                                dt = _parse_arr_datetime_utc(getattr(e, "air_date_utc", None))
                                if not dt:
                                    continue
                                if best is None:
                                    best = dt
                                else:
                                    if search_order == "newest" and dt > best:
                                        best = dt
                                    if search_order == "oldest" and dt < best:
                                        best = dt
                            return (1 if best else 0, best.timestamp() if best else 0.0, len(eps))

                        grouped.sort(key=_group_sort_key, reverse=(search_order == "newest"))

                    season_inventory_cache: dict[int, dict[int, dict[str, int]]] = {}

                    def _smart_mode_action(series_id: int, season_number: int, eps: list[Any]) -> str:
                        """
                        Returns one of: "season_pack", "episodes", "skip"
                        """
                        if sonarr_missing_mode != "smart":
                            return "season_pack"
                        season_key = f"season:{int(series_id)}:{int(season_number)}"
                        season_cooldown_hours = int(retry_hours)
                        if any(
                            self._is_recent_release("sonarr", getattr(e, "air_date_utc", None)) for e in (eps or [])
                        ):
                            season_cooldown_hours = min(season_cooldown_hours, int(recent_retry_hours))
                        if self.store.item_on_cooldown(
                            "sonarr",
                            instance.instance_id,
                            season_key,
                            season_cooldown_hours,
                        ):
                            # Smart mode should move on to other seasons/shows when a season pack
                            # was already attempted recently.
                            return "skip"
                        inv = season_inventory_cache.get(int(series_id))
                        if inv is None:
                            inv = client.fetch_series_season_inventory(int(series_id))
                            season_inventory_cache[int(series_id)] = inv
                        season_stats = inv.get(int(season_number)) or {}
                        aired_total = int(season_stats.get("aired_total") or 0)
                        aired_downloaded = int(season_stats.get("aired_downloaded") or 0)
                        # Truly empty season in library: prefer season pack.
                        if aired_total > 0 and aired_downloaded == 0:
                            return "season_pack"
                        episode_numbers = {
                            int(getattr(e, "episode_number", 0) or 0)
                            for e in eps
                            if int(getattr(e, "episode_number", 0) or 0) > 0
                        }
                        if not episode_numbers:
                            return "season_pack" if len(eps) >= 3 else "episodes"
                        highest_episode = max(episode_numbers)
                        coverage = float(len(episode_numbers)) / float(highest_episode) if highest_episode > 0 else 0.0
                        # "Mostly empty season" heuristic:
                        # - enough missing episodes to justify a pack search
                        # - the missing episodes cover a large portion of the aired season
                        if (len(episode_numbers) >= 3 and coverage >= 0.6) or len(episode_numbers) >= 6:
                            return "season_pack"
                        return "episodes"

                    for (sid, sn), eps in grouped:
                        if triggered_missing >= missing_cap:
                            break
                        action = _smart_mode_action(sid, sn, eps)
                        if action == "skip":
                            continue
                        if action == "season_pack":
                            before = stats.actions_triggered
                            self._handle_sonarr_season_search(
                                instance=instance,
                                retry_hours=retry_hours,
                                recent_retry_hours=recent_retry_hours,
                                rate_window_minutes=rate_window_minutes,
                                rate_cap=rate_cap,
                                min_hours_after_release=effective_min_hours_after_release,
                                pace_seconds=effective_between_actions,
                                series_id=sid,
                                series_title=str(getattr(eps[0], "series_title", "") or ""),
                                season_number=sn,
                                season_air_dates_utc=[getattr(e, "air_date_utc", None) for e in eps],
                                client=client,
                                triggered_items=triggered_items,
                                stats=stats,
                                progress_cb=progress_cb,
                            )
                            if stats.actions_triggered > before:
                                triggered_missing += 1
                                # Pacing is enforced inside the handler so it's shared across instances.
                        else:
                            for ep in eps:
                                if triggered_missing >= missing_cap:
                                    break
                                before = stats.actions_triggered
                                self._handle_wanted_item(
                                    app_type="sonarr",
                                    instance=instance,
                                    retry_hours=retry_hours,
                                    recent_retry_hours=recent_retry_hours,
                                    rate_window_minutes=rate_window_minutes,
                                    rate_cap=rate_cap,
                                    min_hours_after_release=effective_min_hours_after_release,
                                    pace_seconds=effective_between_actions,
                                    wanted_item=ep,
                                    client=client,
                                    triggered_items=triggered_items,
                                    stats=stats,
                                    progress_cb=progress_cb,
                                )
                                if stats.actions_triggered > before:
                                    triggered_missing += 1
                                    # Pacing is enforced inside the handler so it's shared across instances.
                else:
                    # Group by show and trigger EpisodeSearch for all eligible missing episodes in that show.
                    shows: dict[int, list[Any]] = {}
                    for ep in missing_items:
                        sid = int(getattr(ep, "series_id", 0) or 0)
                        if sid <= 0:
                            continue
                        shows.setdefault(sid, []).append(ep)

                    grouped = list(shows.items())
                    if search_order == "smart":
                        recent_cutoff = now_utc - timedelta(days=2)
                        calendar_g: list[tuple[float, Any]] = []
                        recent_g: list[tuple[datetime, Any]] = []
                        rest_g: list[tuple[datetime, Any]] = []
                        unknown_g: list[Any] = []
                        for g in grouped:
                            _, eps = g
                            cal_ts = None
                            for e in eps:
                                sid = int(getattr(e, "series_id", 0) or 0)
                                sn = int(getattr(e, "season_number", 0) or 0)
                                en = int(getattr(e, "episode_number", 0) or 0)
                                triple = (sid, sn, en)
                                if triple in sonarr_calendar_triples:
                                    ts = sonarr_calendar_boost_ts.get(triple, 0.0)
                                    cal_ts = ts if cal_ts is None or ts > cal_ts else cal_ts
                            if cal_ts is not None:
                                calendar_g.append((cal_ts, g))
                                continue
                            newest = None
                            for e in eps:
                                dt = _parse_arr_datetime_utc(getattr(e, "air_date_utc", None))
                                if dt and (newest is None or dt > newest):
                                    newest = dt
                            if not newest:
                                unknown_g.append(g)
                            elif newest >= recent_cutoff:
                                recent_g.append((newest, g))
                            else:
                                rest_g.append((newest, g))
                        calendar_g.sort(key=lambda x: x[0], reverse=True)
                        recent_g.sort(key=lambda x: x[0], reverse=True)
                        rest_g.sort(key=lambda x: x[0])  # oldest -> newest
                        tail_n = max(1, int(len(rest_g) * 0.10)) if rest_g else 0
                        oldest_tail = rest_g[:tail_n]
                        middle = rest_g[tail_n:]
                        random.shuffle(middle)
                        grouped = (
                            [g for _, g in calendar_g]
                            + [g for _, g in recent_g]
                            + [g for _, g in middle]
                            + [g for _, g in oldest_tail]
                            + unknown_g
                        )
                    elif search_order == "random":
                        random.shuffle(grouped)
                    else:

                        def _show_sort_key(item: tuple[int, list[Any]]) -> tuple[int, float, int]:
                            _, eps = item
                            best = None
                            for e in eps:
                                dt = _parse_arr_datetime_utc(getattr(e, "air_date_utc", None))
                                if not dt:
                                    continue
                                if best is None:
                                    best = dt
                                else:
                                    if search_order == "newest" and dt > best:
                                        best = dt
                                    if search_order == "oldest" and dt < best:
                                        best = dt
                            return (1 if best else 0, best.timestamp() if best else 0.0, len(eps))

                        grouped.sort(key=_show_sort_key, reverse=(search_order == "newest"))
                    for sid, eps in grouped:
                        if triggered_missing >= missing_cap:
                            break
                        before = stats.actions_triggered
                        self._handle_sonarr_show_search(
                            instance=instance,
                            retry_hours=retry_hours,
                            recent_retry_hours=recent_retry_hours,
                            rate_window_minutes=rate_window_minutes,
                            rate_cap=rate_cap,
                            min_hours_after_release=effective_min_hours_after_release,
                            pace_seconds=effective_between_actions,
                            series_id=sid,
                            series_title=str(getattr(eps[0], "series_title", "") or ""),
                            episode_ids=[
                                int(getattr(e, "episode_id", 0) or 0)
                                for e in eps
                                if int(getattr(e, "episode_id", 0) or 0) > 0
                            ],
                            episode_air_dates_utc=[getattr(e, "air_date_utc", None) for e in eps],
                            client=client,
                            triggered_items=triggered_items,
                            stats=stats,
                            progress_cb=progress_cb,
                        )
                        if stats.actions_triggered > before:
                            triggered_missing += 1
                            # Pacing is enforced inside the handler so it's shared across instances.
            else:
                _process(missing_items, missing_cap, "missing")
            _process(cutoff_items, cutoff_cap, "cutoff")
            wakeup_iso = None
            if next_eligible_wakeup_utc:
                now_utc = datetime.now(timezone.utc)
                if next_eligible_wakeup_utc > now_utc:
                    wakeup_dt = max(next_eligible_wakeup_utc, now_utc + timedelta(seconds=30))
                    scheduled_dt = now_utc + timedelta(minutes=interval)
                    if wakeup_dt < scheduled_dt:
                        wakeup_iso = wakeup_dt.isoformat()
            self._update_sync_status(app_type, instance, interval, next_sync_override_iso=wakeup_iso)
        except Exception:
            status = "error"
            raise
        finally:
            finished_at = datetime.now(timezone.utc).isoformat()
            inst_stats = {
                "wanted_count": wanted_count,
                "actions_triggered": stats.actions_triggered - before_actions,
                "actions_skipped_cooldown": stats.actions_skipped_cooldown - before_cooldown,
                "actions_skipped_rate_limit": stats.actions_skipped_rate_limit - before_rate,
                "actions_skipped_not_released": stats.actions_skipped_not_released - before_not_released,
            }
            # Record per-instance stats for the Web UI "Recent Runs" page.
            self.store.record_instance_run(
                cycle_run_id=cycle_run_id,
                hunt_type=app_type,
                instance_id=instance.instance_id,
                instance_name=instance.instance_name,
                started_at=instance_started_at,
                finished_at=finished_at,
                status=status,
                stats=inst_stats,
            )

    def _update_sync_status(
        self,
        app_type: str,
        instance: ArrSyncInstanceConfig,
        interval_minutes: int,
        next_sync_override_iso: str | None = None,
    ) -> None:
        now = datetime.now(timezone.utc)
        next_sync = next_sync_override_iso or (now + timedelta(minutes=interval_minutes)).isoformat()
        self.store.upsert_sync_status(
            hunt_type=app_type,
            instance_id=instance.instance_id,
            last_sync_time=now.isoformat(),
            next_sync_time=next_sync,
        )

    def _handle_wanted_item(
        self,
        app_type: str,
        instance: ArrSyncInstanceConfig,
        retry_hours: int,
        recent_retry_hours: int,
        rate_window_minutes: int,
        rate_cap: int,
        min_hours_after_release: int,
        pace_seconds: int,
        wanted_item,
        client: ArrClient,
        triggered_items: set[str],
        stats: CycleStats,
        progress_cb: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        item_key = wanted_item.item_key
        if item_key in triggered_items:
            return

        # Release gate: don't waste searches on unreleased content.
        now = datetime.now(timezone.utc)
        if min_hours_after_release > 0:
            release_iso = None
            if app_type == "radarr":
                release_iso = getattr(wanted_item, "release_date_utc", None)
            else:
                release_iso = getattr(wanted_item, "air_date_utc", None)
            release_dt = _parse_arr_datetime_utc(release_iso)
            if release_dt:
                eligible_dt = release_dt + timedelta(hours=min_hours_after_release)
                if now < eligible_dt:
                    stats.actions_skipped_not_released += 1
                    if progress_cb:
                        progress_cb(
                            {
                                "type": "item_skipped_not_released",
                                "app_type": app_type,
                                "instance_id": instance.instance_id,
                                "instance_name": instance.instance_name,
                                "item_key": item_key,
                                "release_time_utc": release_dt.isoformat(),
                                "eligible_time_utc": eligible_dt.isoformat(),
                                "actions_skipped_not_released": stats.actions_skipped_not_released,
                            }
                        )
                    return

        # Rate limit: cap number of search triggers in the configured window.
        window_start = now - timedelta(minutes=rate_window_minutes)
        used = self.store.count_search_events_since(app_type, instance.instance_id, window_start.isoformat())
        if used >= rate_cap:
            stats.actions_skipped_rate_limit += 1
            if progress_cb:
                progress_cb(
                    {
                        "type": "item_skipped_rate_limit",
                        "app_type": app_type,
                        "instance_id": instance.instance_id,
                        "instance_name": instance.instance_name,
                        "item_key": item_key,
                        "rate_used": used,
                        "rate_cap": rate_cap,
                        "rate_window_minutes": rate_window_minutes,
                        "actions_skipped_rate_limit": stats.actions_skipped_rate_limit,
                    }
                )
            return

        cooldown_hours = int(retry_hours)
        release_iso = (
            getattr(wanted_item, "release_date_utc", None)
            if app_type == "radarr"
            else getattr(wanted_item, "air_date_utc", None)
        )
        if self._is_recent_release(app_type, release_iso, now=now):
            cooldown_hours = min(cooldown_hours, int(recent_retry_hours))

        if self.store.item_on_cooldown(app_type, instance.instance_id, item_key, cooldown_hours):
            stats.actions_skipped_cooldown += 1
            if progress_cb:
                progress_cb(
                    {
                        "type": "item_skipped_cooldown",
                        "app_type": app_type,
                        "instance_id": instance.instance_id,
                        "instance_name": instance.instance_name,
                        "item_key": item_key,
                        "actions_triggered": stats.actions_triggered,
                        "actions_skipped_cooldown": stats.actions_skipped_cooldown,
                    }
                )
            return

        # Shared pacing across instances: wait right before sending a trigger.
        self._wait_pace(pace_seconds)

        if app_type == "radarr":
            success = client.trigger_movie_search(wanted_item.movie_id)
            title = wanted_item.title
        else:
            success = client.trigger_episode_search(wanted_item.episode_id)
            title = f"{wanted_item.series_title} S{wanted_item.season_number:02d}E{wanted_item.episode_number:02d}"

        if success:
            self._mark_action()
            stats.actions_triggered += 1
            triggered_items.add(item_key)
            self.store.mark_item_action(app_type, instance.instance_id, item_key, "", title)
            self.store.record_search_event(app_type, instance.instance_id)
            self.store.record_search_action(
                hunt_type=app_type,
                instance_id=instance.instance_id,
                instance_name=instance.instance_name,
                item_key=item_key,
                title=title,
            )
            self.logger.info("Triggered %s search: %s (%s)", app_type, title, instance.instance_name)
            if progress_cb:
                progress_cb(
                    {
                        "type": "item_triggered",
                        "app_type": app_type,
                        "instance_id": instance.instance_id,
                        "instance_name": instance.instance_name,
                        "item_key": item_key,
                        "title": title,
                        "actions_triggered": stats.actions_triggered,
                        "actions_skipped_cooldown": stats.actions_skipped_cooldown,
                        "actions_skipped_rate_limit": stats.actions_skipped_rate_limit,
                    }
                )

    def _handle_sonarr_season_search(
        self,
        *,
        instance: ArrSyncInstanceConfig,
        retry_hours: int,
        recent_retry_hours: int,
        rate_window_minutes: int,
        rate_cap: int,
        min_hours_after_release: int,
        pace_seconds: int,
        series_id: int,
        series_title: str,
        season_number: int,
        season_air_dates_utc: list[str | None],
        client: ArrClient,
        triggered_items: set[str],
        stats: CycleStats,
        progress_cb: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        item_key = f"season:{int(series_id)}:{int(season_number)}"
        if item_key in triggered_items:
            return

        now = datetime.now(timezone.utc)

        # Release gate: for season packs, require at least one episode in the season to be eligible.
        if min_hours_after_release > 0:
            eligible = False
            newest_dt: datetime | None = None
            newest_eligible_dt: datetime | None = None
            for iso in season_air_dates_utc or []:
                dt = _parse_arr_datetime_utc(iso)
                if not dt:
                    # Unknown dates: don't block to avoid starving older series.
                    eligible = True
                    continue
                if not newest_dt or dt > newest_dt:
                    newest_dt = dt
                e_dt = dt + timedelta(hours=min_hours_after_release)
                if not newest_eligible_dt or e_dt > newest_eligible_dt:
                    newest_eligible_dt = e_dt
                if now >= e_dt:
                    eligible = True
            if not eligible:
                stats.actions_skipped_not_released += 1
                if progress_cb:
                    progress_cb(
                        {
                            "type": "item_skipped_not_released",
                            "app_type": "sonarr",
                            "instance_id": instance.instance_id,
                            "instance_name": instance.instance_name,
                            "item_key": item_key,
                            "release_time_utc": newest_dt.isoformat() if newest_dt else None,
                            "eligible_time_utc": newest_eligible_dt.isoformat() if newest_eligible_dt else None,
                            "actions_skipped_not_released": stats.actions_skipped_not_released,
                        }
                    )
                return

        # Rate limit.
        window_start = now - timedelta(minutes=rate_window_minutes)
        used = self.store.count_search_events_since("sonarr", instance.instance_id, window_start.isoformat())
        if used >= rate_cap:
            stats.actions_skipped_rate_limit += 1
            if progress_cb:
                progress_cb(
                    {
                        "type": "item_skipped_rate_limit",
                        "app_type": "sonarr",
                        "instance_id": instance.instance_id,
                        "instance_name": instance.instance_name,
                        "item_key": item_key,
                        "rate_used": used,
                        "rate_cap": rate_cap,
                        "rate_window_minutes": rate_window_minutes,
                        "actions_skipped_rate_limit": stats.actions_skipped_rate_limit,
                    }
                )
            return

        cooldown_hours = int(retry_hours)
        if any(self._is_recent_release("sonarr", iso, now=now) for iso in (season_air_dates_utc or [])):
            cooldown_hours = min(cooldown_hours, int(recent_retry_hours))

        if self.store.item_on_cooldown("sonarr", instance.instance_id, item_key, cooldown_hours):
            stats.actions_skipped_cooldown += 1
            if progress_cb:
                progress_cb(
                    {
                        "type": "item_skipped_cooldown",
                        "app_type": "sonarr",
                        "instance_id": instance.instance_id,
                        "instance_name": instance.instance_name,
                        "item_key": item_key,
                        "actions_triggered": stats.actions_triggered,
                        "actions_skipped_cooldown": stats.actions_skipped_cooldown,
                    }
                )
            return

        self._wait_pace(pace_seconds)
        success = client.trigger_season_search(series_id, season_number)
        title = f"{series_title} Season {int(season_number):02d} (Pack)"
        if success:
            self._mark_action()
            stats.actions_triggered += 1
            triggered_items.add(item_key)
            self.store.mark_item_action("sonarr", instance.instance_id, item_key, "", title)
            self.store.record_search_event("sonarr", instance.instance_id)
            self.store.record_search_action(
                hunt_type="sonarr",
                instance_id=instance.instance_id,
                instance_name=instance.instance_name,
                item_key=item_key,
                title=title,
            )
            self.logger.info("Triggered sonarr season search: %s (%s)", title, instance.instance_name)
            if progress_cb:
                progress_cb(
                    {
                        "type": "item_triggered",
                        "app_type": "sonarr",
                        "instance_id": instance.instance_id,
                        "instance_name": instance.instance_name,
                        "item_key": item_key,
                        "title": title,
                        "actions_triggered": stats.actions_triggered,
                        "actions_skipped_cooldown": stats.actions_skipped_cooldown,
                        "actions_skipped_rate_limit": stats.actions_skipped_rate_limit,
                    }
                )

    def _handle_sonarr_show_search(
        self,
        *,
        instance: ArrSyncInstanceConfig,
        retry_hours: int,
        recent_retry_hours: int,
        rate_window_minutes: int,
        rate_cap: int,
        min_hours_after_release: int,
        pace_seconds: int,
        series_id: int,
        series_title: str,
        episode_ids: list[int],
        episode_air_dates_utc: list[str | None],
        client: ArrClient,
        triggered_items: set[str],
        stats: CycleStats,
        progress_cb: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        item_key = f"series:{int(series_id)}"
        if item_key in triggered_items:
            return
        if not episode_ids:
            return

        now = datetime.now(timezone.utc)

        # Release gate: require at least one eligible episode.
        if min_hours_after_release > 0:
            eligible = False
            newest_dt: datetime | None = None
            newest_eligible_dt: datetime | None = None
            for iso in episode_air_dates_utc or []:
                dt = _parse_arr_datetime_utc(iso)
                if not dt:
                    eligible = True
                    continue
                if not newest_dt or dt > newest_dt:
                    newest_dt = dt
                e_dt = dt + timedelta(hours=min_hours_after_release)
                if not newest_eligible_dt or e_dt > newest_eligible_dt:
                    newest_eligible_dt = e_dt
                if now >= e_dt:
                    eligible = True
            if not eligible:
                stats.actions_skipped_not_released += 1
                if progress_cb:
                    progress_cb(
                        {
                            "type": "item_skipped_not_released",
                            "app_type": "sonarr",
                            "instance_id": instance.instance_id,
                            "instance_name": instance.instance_name,
                            "item_key": item_key,
                            "release_time_utc": newest_dt.isoformat() if newest_dt else None,
                            "eligible_time_utc": newest_eligible_dt.isoformat() if newest_eligible_dt else None,
                            "actions_skipped_not_released": stats.actions_skipped_not_released,
                        }
                    )
                return

        window_start = now - timedelta(minutes=rate_window_minutes)
        used = self.store.count_search_events_since("sonarr", instance.instance_id, window_start.isoformat())
        if used >= rate_cap:
            stats.actions_skipped_rate_limit += 1
            if progress_cb:
                progress_cb(
                    {
                        "type": "item_skipped_rate_limit",
                        "app_type": "sonarr",
                        "instance_id": instance.instance_id,
                        "instance_name": instance.instance_name,
                        "item_key": item_key,
                        "rate_used": used,
                        "rate_cap": rate_cap,
                        "rate_window_minutes": rate_window_minutes,
                        "actions_skipped_rate_limit": stats.actions_skipped_rate_limit,
                    }
                )
            return

        cooldown_hours = int(retry_hours)
        if any(self._is_recent_release("sonarr", iso, now=now) for iso in (episode_air_dates_utc or [])):
            cooldown_hours = min(cooldown_hours, int(recent_retry_hours))

        if self.store.item_on_cooldown("sonarr", instance.instance_id, item_key, cooldown_hours):
            stats.actions_skipped_cooldown += 1
            if progress_cb:
                progress_cb(
                    {
                        "type": "item_skipped_cooldown",
                        "app_type": "sonarr",
                        "instance_id": instance.instance_id,
                        "instance_name": instance.instance_name,
                        "item_key": item_key,
                        "actions_triggered": stats.actions_triggered,
                        "actions_skipped_cooldown": stats.actions_skipped_cooldown,
                    }
                )
            return

        self._wait_pace(pace_seconds)
        success = client.trigger_episode_search_bulk(episode_ids)
        title = f"{series_title} ({len(episode_ids)} eps) (Show Batch)"
        if success:
            self._mark_action()
            stats.actions_triggered += 1
            triggered_items.add(item_key)
            self.store.mark_item_action("sonarr", instance.instance_id, item_key, "", title)
            self.store.record_search_event("sonarr", instance.instance_id)
            self.store.record_search_action(
                hunt_type="sonarr",
                instance_id=instance.instance_id,
                instance_name=instance.instance_name,
                item_key=item_key,
                title=title,
            )
            self.logger.info("Triggered sonarr show batch search: %s (%s)", title, instance.instance_name)
            if progress_cb:
                progress_cb(
                    {
                        "type": "item_triggered",
                        "app_type": "sonarr",
                        "instance_id": instance.instance_id,
                        "instance_name": instance.instance_name,
                        "item_key": item_key,
                        "title": title,
                        "actions_triggered": stats.actions_triggered,
                        "actions_skipped_cooldown": stats.actions_skipped_cooldown,
                        "actions_skipped_rate_limit": stats.actions_skipped_rate_limit,
                    }
                )
