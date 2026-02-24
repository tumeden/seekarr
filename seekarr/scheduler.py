import argparse
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from .config import load_config
from .arr import ArrRequestError
from .engine import Engine
from .logging_utils import setup_logging
from .state import StateStore

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Seekarr")
    parser.add_argument(
        "--config",
        required=True,
        help="Path to YAML config file.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one cycle and exit.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Ignore due time and run enabled instances immediately.",
    )
    return parser.parse_args()


def _run_once(engine: Engine, logger: logging.Logger, force: bool = False) -> int:
    try:
        stats = engine.run_cycle(force=force)
        logger.info("Cycle complete: %s", stats.as_dict())
        return 0
    except ArrRequestError as exc:
        logger.error("%s", exc)
        return 2


def main() -> int:
    args = parse_args()
    config_path = Path(args.config).resolve()
    config = load_config(str(config_path))
    setup_logging(config.app.log_level)
    logger = logging.getLogger("seekarr")
    store = StateStore(config.app.db_path)

    if not config.radarr_instances and not config.sonarr_instances:
        logger.error("No instances configured. Add radarr.instances and/or sonarr.instances.")
        return 1

    engine = Engine(config=config, logger=logger)
    if args.once:
        return _run_once(engine, logger, force=args.force)

    # Independent per-instance scheduling: each instance sleeps until its next_sync_time.
    stop_event = threading.Event()
    run_lock = threading.Lock()  # prevent overlapping Arr calls across instances

    def _sleep_until(iso: str | None) -> None:
        if not iso:
            return
        try:
            dt = datetime.fromisoformat(str(iso))
        except ValueError:
            return
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        seconds = max(0.0, (dt.astimezone(timezone.utc) - now).total_seconds())
        if seconds <= 0:
            return
        stop_event.wait(timeout=seconds)

    def _instance_loop(app_type: str, instance_id: int) -> None:
        while not stop_event.is_set():
            try:
                store.set_scheduler_heartbeat()

                inst = engine._find_instance(app_type, instance_id)
                if not inst or not inst.enabled or not inst.arr.enabled:
                    stop_event.wait(timeout=5.0)
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

                with run_lock:
                    engine.run_instance(app_type=app_type, instance_id=instance_id, force=False)
            except Exception as exc:
                logger.exception("Instance loop failed (%s:%s): %s", app_type, instance_id, exc)
                stop_event.wait(timeout=5.0)

    threads: list[threading.Thread] = []
    for inst in config.radarr_instances:
        t = threading.Thread(
            target=_instance_loop,
            args=("radarr", int(inst.instance_id)),
            name=f"seekarr-radarr-{inst.instance_id}",
            daemon=True,
        )
        threads.append(t)
        t.start()
    for inst in config.sonarr_instances:
        t = threading.Thread(
            target=_instance_loop,
            args=("sonarr", int(inst.instance_id)),
            name=f"seekarr-sonarr-{inst.instance_id}",
            daemon=True,
        )
        threads.append(t)
        t.start()

    # Optional: run once immediately regardless of due time, then continue sleeping until due.
    if args.force:
        try:
            with run_lock:
                for inst in config.radarr_instances:
                    engine.run_instance(app_type="radarr", instance_id=int(inst.instance_id), force=True)
                for inst in config.sonarr_instances:
                    engine.run_instance(app_type="sonarr", instance_id=int(inst.instance_id), force=True)
        except Exception as exc:
            logger.exception("Forced startup run failed: %s", exc)

    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        logger.info("Stopped by user.")
        stop_event.set()
        for t in threads:
            t.join(timeout=2.0)
        return 0
