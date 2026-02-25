import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any
from urllib.parse import urlparse

import requests

from .config import ArrConfig


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", (text or "").lower())).strip()


def _as_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("true", "1", "yes", "on"):
            return True
        if v in ("false", "0", "no", "off"):
            return False
    return None


@dataclass(frozen=True)
class WantedMovie:
    movie_id: int
    title: str
    year: int
    tmdb_id: int
    imdb_id: str
    release_date_utc: str | None = None
    wanted_kind: str = "missing"  # "missing" or "cutoff"

    @property
    def item_key(self) -> str:
        return f"movie:{self.movie_id}"


@dataclass(frozen=True)
class WantedEpisode:
    episode_id: int
    series_id: int
    series_title: str
    series_tvdb_id: int
    season_number: int
    episode_number: int
    air_date_utc: str | None = None
    wanted_kind: str = "missing"  # "missing" or "cutoff"

    @property
    def item_key(self) -> str:
        return f"episode:{self.episode_id}"


class ArrRequestError(RuntimeError):
    def __init__(
        self,
        app: str,
        base_url: str,
        method: str,
        path: str,
        message: str,
        hint: str | None = None,
    ) -> None:
        super().__init__(message)
        self.app = app
        self.base_url = base_url
        self.method = method
        self.path = path
        self.message = message
        self.hint = hint or ""

    def __str__(self) -> str:
        loc = ""
        try:
            u = urlparse(self.base_url)
            if u.hostname:
                loc = u.hostname
                if u.port:
                    loc = f"{loc}:{u.port}"
        except Exception:
            loc = ""
        where = loc or self.base_url
        extra = f" Hint: {self.hint}" if self.hint else ""
        return f"{self.app} request failed ({where} {self.method} {self.path}): {self.message}.{extra}"


class ArrClient:
    def __init__(
        self,
        name: str,
        config: ArrConfig,
        timeout_seconds: int,
        verify_ssl: bool,
        logger: logging.Logger,
    ) -> None:
        self.name = name
        self.config = config
        self.timeout_seconds = timeout_seconds
        self.verify_ssl = verify_ssl
        self.logger = logger

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json_data: dict[str, Any] | None = None,
    ) -> dict[str, Any] | list[Any]:
        base = self.config.url.rstrip("/")
        url = f"{base}{path}"
        headers = {"X-Api-Key": self.config.api_key}
        try:
            resp = requests.request(
                method,
                url,
                headers=headers,
                params=params,
                json=json_data,
                timeout=self.timeout_seconds,
                verify=self.verify_ssl,
            )
        except requests.exceptions.ConnectionError as exc:
            raise ArrRequestError(
                app=self.name,
                base_url=base,
                method=method,
                path=path,
                message="Cannot connect (connection refused/unreachable)",
                hint="Check the instance URL/port and that the service is running.",
            ) from exc
        except requests.exceptions.Timeout as exc:
            raise ArrRequestError(
                app=self.name,
                base_url=base,
                method=method,
                path=path,
                message=f"Request timed out after {self.timeout_seconds}s",
                hint="Increase request_timeout_seconds or check network latency.",
            ) from exc
        except requests.exceptions.RequestException as exc:
            raise ArrRequestError(
                app=self.name,
                base_url=base,
                method=method,
                path=path,
                message=exc.__class__.__name__,
            ) from exc

        try:
            resp.raise_for_status()
        except requests.exceptions.HTTPError as exc:
            snippet = (resp.text or "").strip().replace("\n", " ")
            if len(snippet) > 200:
                snippet = snippet[:200] + "..."
            msg = f"HTTP {resp.status_code}"
            if snippet:
                msg = f"{msg} ({snippet})"
            raise ArrRequestError(
                app=self.name,
                base_url=base,
                method=method,
                path=path,
                message=msg,
                hint="Check API key permissions and that the endpoint exists for your Arr version.",
            ) from exc

        if not resp.text:
            return {}
        try:
            return resp.json()
        except ValueError as exc:
            raise ArrRequestError(
                app=self.name,
                base_url=base,
                method=method,
                path=path,
                message="Invalid JSON response",
            ) from exc

    def _fetch_paged_records(self, path: str) -> list[dict[str, Any]]:
        page = 1
        page_size = 250
        records: list[dict[str, Any]] = []
        while True:
            payload = self._request(
                "GET",
                path,
                params={"page": page, "pageSize": page_size},
            )
            if isinstance(payload, dict) and "records" in payload:
                chunk = payload.get("records") or []
            elif isinstance(payload, list):
                chunk = payload
            else:
                chunk = []
            if not chunk:
                break
            records.extend([r for r in chunk if isinstance(r, dict)])
            if len(chunk) < page_size:
                break
            page += 1
        return records

    def fetch_calendar(self, start: date, end: date) -> list[dict[str, Any]]:
        """
        Fetch Arr calendar records in a date window.
        Works for both Sonarr and Radarr.
        """
        payload = self._request(
            "GET",
            "/api/v3/calendar",
            params={
                "start": start.isoformat(),
                "end": end.isoformat(),
            },
        )
        if not isinstance(payload, list):
            return []
        return [row for row in payload if isinstance(row, dict)]

    def _fetch_series_lookup(self) -> dict[int, tuple[str, int, bool]]:
        """Return {series_id: (title, tvdb_id, monitored)} for Sonarr mapping."""
        lookup: dict[int, tuple[str, int, bool]] = {}
        try:
            payload = self._request("GET", "/api/v3/series")
            if not isinstance(payload, list):
                return lookup
            for row in payload:
                if not isinstance(row, dict):
                    continue
                series_id = int(row.get("id") or 0)
                if not series_id:
                    continue
                title = str(row.get("title") or "").strip()
                tvdb_id = int(row.get("tvdbId") or 0)
                monitored = bool(_as_bool(row.get("monitored")) is not False)
                lookup[series_id] = (title, tvdb_id, monitored)
        except (ArrRequestError, requests.RequestException):
            return lookup
        return lookup

    def _fetch_movie_meta_lookup(self) -> dict[int, dict[str, Any]]:
        """Return {movie_id: {monitored: bool, release_date_utc: str|None}} for Radarr filtering."""
        lookup: dict[int, dict[str, Any]] = {}
        try:
            payload = self._request("GET", "/api/v3/movie")
            if not isinstance(payload, list):
                return lookup
            for row in payload:
                if not isinstance(row, dict):
                    continue
                movie_id = int(row.get("id") or 0)
                if not movie_id:
                    continue
                monitored = bool(_as_bool(row.get("monitored")) is not False)
                # Prefer digitalRelease if present, otherwise physicalRelease, otherwise inCinemas.
                release_utc = row.get("digitalRelease") or row.get("physicalRelease") or row.get("inCinemas") or None
                lookup[movie_id] = {
                    "monitored": monitored,
                    "release_date_utc": str(release_utc) if release_utc else None,
                }
        except (ArrRequestError, requests.RequestException):
            return lookup
        return lookup

    def fetch_wanted_movies(self, search_missing: bool = True, search_cutoff_unmet: bool = True) -> list[WantedMovie]:
        if not self.config.enabled:
            return []
        movie_meta = self._fetch_movie_meta_lookup()
        missing_rows = self._fetch_paged_records("/api/v3/wanted/missing") if search_missing else []
        cutoff_rows = self._fetch_paged_records("/api/v3/wanted/cutoff") if search_cutoff_unmet else []
        out: dict[int, WantedMovie] = {}
        # "missing" items should win when an item appears in both lists.
        for wanted_kind, rows in (("missing", missing_rows), ("cutoff", cutoff_rows)):
            for row in rows:
                movie_id = int(row.get("id") or row.get("movieId") or 0)
                if not movie_id:
                    continue
                if wanted_kind == "cutoff" and movie_id in out:
                    continue
                # Only act on monitored movies.
                meta = movie_meta.get(movie_id) or {}
                monitored = meta.get("monitored")
                if monitored is None:
                    movie = row.get("movie") if isinstance(row.get("movie"), dict) else {}
                    monitored = _as_bool(row.get("monitored"))
                    if monitored is None:
                        monitored = _as_bool(movie.get("monitored"))
                if monitored is False:
                    continue
                release_utc = meta.get("release_date_utc")
                if not release_utc:
                    movie = row.get("movie") if isinstance(row.get("movie"), dict) else {}
                    release_utc = (
                        row.get("digitalRelease")
                        or row.get("physicalRelease")
                        or row.get("inCinemas")
                        or movie.get("digitalRelease")
                        or movie.get("physicalRelease")
                        or movie.get("inCinemas")
                        or None
                    )
                item = WantedMovie(
                    movie_id=movie_id,
                    title=str(row.get("title") or ""),
                    year=int(row.get("year") or 0),
                    tmdb_id=int(row.get("tmdbId") or 0),
                    imdb_id=str(row.get("imdbId") or "").lower(),
                    release_date_utc=str(release_utc) if release_utc else None,
                    wanted_kind=wanted_kind,
                )
                out[movie_id] = item
        return list(out.values())

    def fetch_wanted_episodes(
        self, search_missing: bool = True, search_cutoff_unmet: bool = True
    ) -> list[WantedEpisode]:
        if not self.config.enabled:
            return []
        missing_rows = self._fetch_paged_records("/api/v3/wanted/missing") if search_missing else []
        cutoff_rows = self._fetch_paged_records("/api/v3/wanted/cutoff") if search_cutoff_unmet else []
        series_lookup = self._fetch_series_lookup()
        out: dict[int, WantedEpisode] = {}
        # "missing" items should win when an item appears in both lists.
        for wanted_kind, rows in (("missing", missing_rows), ("cutoff", cutoff_rows)):
            for row in rows:
                episode_id = int(row.get("id") or row.get("episodeId") or 0)
                if not episode_id:
                    continue
                if wanted_kind == "cutoff" and episode_id in out:
                    continue
                # Episode/series monitoring flags differ by Sonarr version and endpoint.
                # We enforce:
                # - series must be monitored (if we can determine it)
                # - episode must be monitored (if provided by the endpoint)
                series = row.get("series") if isinstance(row.get("series"), dict) else {}
                series_id = int(row.get("seriesId") or series.get("id") or 0)
                fallback_title, fallback_tvdb, fallback_monitored = series_lookup.get(series_id, ("", 0, True))
                series_monitored = _as_bool(series.get("monitored"))
                if series_monitored is None:
                    series_monitored = fallback_monitored
                if series_monitored is False:
                    continue
                ep_monitored = _as_bool(row.get("monitored"))
                if ep_monitored is False:
                    continue
                item = WantedEpisode(
                    episode_id=episode_id,
                    series_id=series_id,
                    series_title=str(series.get("title") or row.get("seriesTitle") or fallback_title or ""),
                    series_tvdb_id=int(series.get("tvdbId") or row.get("seriesTvdbId") or fallback_tvdb or 0),
                    season_number=int(row.get("seasonNumber") or 0),
                    episode_number=int(row.get("episodeNumber") or 0),
                    air_date_utc=(
                        str(row.get("airDateUtc") or row.get("airDate")).strip()
                        if (row.get("airDateUtc") or row.get("airDate"))
                        else None
                    ),
                    wanted_kind=wanted_kind,
                )
                out[episode_id] = item
        return list(out.values())

    def trigger_movie_search(self, movie_id: int) -> bool:
        try:
            self._request(
                "POST",
                "/api/v3/command",
                json_data={"name": "MoviesSearch", "movieIds": [movie_id]},
            )
            return True
        except ArrRequestError as exc:
            self.logger.warning("Radarr command failed for movie %s: %s", movie_id, exc)
            return False

    def trigger_episode_search(self, episode_id: int) -> bool:
        try:
            self._request(
                "POST",
                "/api/v3/command",
                json_data={"name": "EpisodeSearch", "episodeIds": [episode_id]},
            )
            return True
        except ArrRequestError as exc:
            self.logger.warning("Sonarr command failed for episode %s: %s", episode_id, exc)
            return False

    def trigger_episode_search_bulk(self, episode_ids: list[int]) -> bool:
        episode_ids = [int(e) for e in (episode_ids or []) if int(e) > 0]
        if not episode_ids:
            return False
        try:
            self._request(
                "POST",
                "/api/v3/command",
                json_data={"name": "EpisodeSearch", "episodeIds": episode_ids},
            )
            return True
        except ArrRequestError as exc:
            self.logger.warning("Sonarr command failed for %s episodes: %s", len(episode_ids), exc)
            return False

    def trigger_season_search(self, series_id: int, season_number: int) -> bool:
        try:
            self._request(
                "POST",
                "/api/v3/command",
                json_data={"name": "SeasonSearch", "seriesId": int(series_id), "seasonNumber": int(season_number)},
            )
            return True
        except ArrRequestError as exc:
            self.logger.warning(
                "Sonarr command failed for series %s season %s: %s",
                series_id,
                season_number,
                exc,
            )
            return False

    def fetch_series_season_inventory(self, series_id: int) -> dict[int, dict[str, int]]:
        """
        Sonarr helper for Smart mode.
        Returns:
            {
              season_number: {
                "aired_total": int,      # aired episodes known to Sonarr
                "aired_downloaded": int, # aired episodes with hasFile=true
              }
            }
        """
        out: dict[int, dict[str, int]] = {}
        try:
            payload = self._request("GET", "/api/v3/episode", params={"seriesId": int(series_id)})
        except ArrRequestError:
            return out
        if not isinstance(payload, list):
            return out

        now_utc = datetime.now(timezone.utc)

        for row in payload:
            if not isinstance(row, dict):
                continue
            season_number = int(row.get("seasonNumber") or 0)
            if season_number <= 0:
                continue
            air_iso = row.get("airDateUtc") or row.get("airDate")
            aired = True
            if air_iso:
                s = str(air_iso).strip()
                if s.endswith("Z"):
                    s = s[:-1] + "+00:00"
                try:
                    dt = datetime.fromisoformat(s)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    aired = dt.astimezone(timezone.utc) <= now_utc
                except ValueError:
                    aired = True
            if not aired:
                continue

            slot = out.setdefault(season_number, {"aired_total": 0, "aired_downloaded": 0})
            slot["aired_total"] += 1
            if bool(row.get("hasFile")):
                slot["aired_downloaded"] += 1
        return out


def movie_matches_release(movie: WantedMovie, release_title: str, tmdb_id: int, imdb_id: str, year: int) -> bool:
    if movie.tmdb_id and tmdb_id and movie.tmdb_id == tmdb_id:
        return True
    if movie.imdb_id and imdb_id and movie.imdb_id == imdb_id:
        return True
    left = _normalize(movie.title)
    right = _normalize(release_title)
    if left and left in right:
        if movie.year and year:
            return movie.year == year
        return True
    return False


def episode_matches_release(
    ep: WantedEpisode,
    release_title: str,
    tvdb_id: int,
    season: int,
    episode: int,
) -> bool:
    if season and episode:
        if ep.season_number != season or ep.episode_number != episode:
            return False
    if ep.series_tvdb_id and tvdb_id:
        return ep.series_tvdb_id == tvdb_id
    return _normalize(ep.series_title) in _normalize(release_title)
