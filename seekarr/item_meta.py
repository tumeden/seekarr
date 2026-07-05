import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests

MEDIA_CACHE_ROUTE = "/media_cache"
MAX_COVER_IMAGE_BYTES = 8 * 1024 * 1024


def media_cache_dir(db_path: str) -> Path:
    return Path(db_path).parent / "media_cache"


def media_cache_stats(db_path: str) -> dict[str, int]:
    root = media_cache_dir(db_path)
    if not root.exists():
        return {"files": 0, "bytes": 0}
    files = 0
    total = 0
    for path in root.iterdir():
        if not path.is_file():
            continue
        files += 1
        try:
            total += path.stat().st_size
        except OSError:
            pass
    return {"files": files, "bytes": total}


def prune_media_cache(
    db_path: str,
    referenced_urls: set[str],
    retention_days: int,
    force_unreferenced: bool = False,
) -> dict[str, int]:
    root = media_cache_dir(db_path)
    if not root.exists():
        return {"files_removed": 0, "bytes_removed": 0}
    referenced_names = set()
    for url in referenced_urls:
        value = str(url or "").strip()
        if not value.startswith(f"{MEDIA_CACHE_ROUTE}/"):
            continue
        name = value.rsplit("/", 1)[-1]
        if name:
            referenced_names.add(name)

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=max(1, int(retention_days or 30)))
    unreferenced_cutoff = now - timedelta(hours=1)
    files_removed = 0
    bytes_removed = 0
    for path in root.iterdir():
        if not path.is_file():
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        modified = datetime.fromtimestamp(stat.st_mtime, timezone.utc)
        stale = modified < cutoff
        unreferenced = path.name not in referenced_names
        if not (stale or (unreferenced and (force_unreferenced or modified < unreferenced_cutoff))):
            continue
        try:
            path.unlink()
            files_removed += 1
            bytes_removed += stat.st_size
        except OSError:
            pass
    return {"files_removed": files_removed, "bytes_removed": bytes_removed}


def _media_cache_url(filename: str) -> str:
    return f"{MEDIA_CACHE_ROUTE}/{filename}"


def _cover_extension(cover_url: str, content_type: str) -> str:
    ct = str(content_type or "").split(";", 1)[0].strip().lower()
    if ct == "image/jpeg":
        return ".jpg"
    if ct == "image/png":
        return ".png"
    if ct == "image/webp":
        return ".webp"
    if ct == "image/gif":
        return ".gif"
    suffix = Path(urlsplit(str(cover_url or "")).path).suffix.lower()
    if suffix in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
        return ".jpg" if suffix == ".jpeg" else suffix
    return ".jpg"


def _same_origin(url_a: str, url_b: str) -> bool:
    try:
        a = urlsplit(str(url_a or ""))
        b = urlsplit(str(url_b or ""))
    except ValueError:
        return False
    return bool(a.scheme and a.netloc and a.scheme == b.scheme and a.netloc == b.netloc)


def _cache_digest_url(cover_url: str) -> str:
    try:
        parsed = urlsplit(str(cover_url or "").strip())
    except ValueError:
        return str(cover_url or "").strip()
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    path = parsed.path or ""
    query = parsed.query or ""
    normalized = parsed._replace(scheme=scheme, netloc=netloc, path=path, query=query, fragment="")
    return normalized.geturl()


def cache_cover_image(
    db_path: str,
    cover_url: str,
    *,
    app_type: str,
    instance_id: int,
    item_key: str,
    timeout_seconds: int,
    verify_ssl: bool,
    base_url: str = "",
    api_key: str = "",
) -> str:
    url = str(cover_url or "").strip()
    if not url or url.startswith(f"{MEDIA_CACHE_ROUTE}/"):
        return url
    cache_root = media_cache_dir(db_path)
    cache_root.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(_cache_digest_url(url).encode("utf-8")).hexdigest()
    existing = sorted(cache_root.glob(f"{digest}.*"))
    if existing:
        return _media_cache_url(existing[0].name)

    headers = {}
    if api_key and base_url and _same_origin(url, base_url):
        headers["X-Api-Key"] = str(api_key)
    try:
        resp = requests.get(url, headers=headers, timeout=timeout_seconds, verify=verify_ssl, stream=True)
        resp.raise_for_status()
        content_type = str(resp.headers.get("Content-Type") or "").strip()
        if content_type and not content_type.lower().startswith("image/"):
            return url
        ext = _cover_extension(url, content_type)
        target = cache_root / f"{digest}{ext}"
        tmp = cache_root / f"{digest}.tmp"
        total = 0
        with tmp.open("wb") as fh:
            for chunk in resp.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > MAX_COVER_IMAGE_BYTES:
                    try:
                        tmp.unlink()
                    except OSError:
                        pass
                    return url
                fh.write(chunk)
        tmp.replace(target)
        return _media_cache_url(target.name)
    except (OSError, requests.RequestException, ValueError):
        return url


def _arr_json_request(
    base_url: str, api_key: str, timeout_seconds: int, verify_ssl: bool, path: str
) -> dict[str, Any] | list[Any] | None:
    try:
        resp = requests.get(
            f"{base_url.rstrip('/')}{path}",
            headers={"X-Api-Key": api_key},
            timeout=timeout_seconds,
            verify=verify_ssl,
        )
        resp.raise_for_status()
        return resp.json()
    except (requests.RequestException, ValueError):
        return None


def pick_cover_url(base_url: str, payload: dict[str, Any]) -> str | None:
    direct = str(payload.get("remotePoster") or "").strip()
    if direct:
        return direct
    images = payload.get("images") if isinstance(payload.get("images"), list) else []
    poster: dict[str, Any] | None = None
    for row in images:
        if not isinstance(row, dict):
            continue
        if str(row.get("coverType") or "").strip().lower() == "poster":
            poster = row
            break
    if poster is None:
        for row in images:
            if isinstance(row, dict) and (row.get("url") or row.get("remoteUrl")):
                poster = row
                break
    if poster is None:
        return None
    for key in ("remoteUrl", "url"):
        value = str(poster.get(key) or "").strip()
        if value:
            if value.startswith("http://") or value.startswith("https://"):
                return value
            return f"{base_url.rstrip('/')}{value if value.startswith('/') else '/' + value}"
    return None


def resolve_movie_item_meta(
    base_url: str,
    api_key: str,
    timeout_seconds: int,
    verify_ssl: bool,
    movie_id: int,
) -> dict[str, str]:
    iid = int(movie_id or 0)
    if iid <= 0:
        return {"cover_url": "", "item_url": ""}
    raw = _arr_json_request(base_url, api_key, timeout_seconds, verify_ssl, f"/api/v3/movie/{iid}")
    payload = raw if isinstance(raw, dict) else None
    if not payload:
        return {"cover_url": "", "item_url": ""}
    title_slug = str(payload.get("titleSlug") or "").strip()
    return {
        "cover_url": pick_cover_url(base_url, payload) or "",
        "item_url": f"{base_url.rstrip('/')}/movie/{title_slug}"
        if title_slug
        else f"{base_url.rstrip('/')}/movie/{iid}",
    }


def resolve_series_item_meta(
    base_url: str,
    api_key: str,
    timeout_seconds: int,
    verify_ssl: bool,
    series_id: int,
) -> dict[str, str]:
    iid = int(series_id or 0)
    if iid <= 0:
        return {"cover_url": "", "item_url": ""}
    raw = _arr_json_request(base_url, api_key, timeout_seconds, verify_ssl, f"/api/v3/series/{iid}")
    payload = raw if isinstance(raw, dict) else None
    if not payload:
        return {"cover_url": "", "item_url": ""}
    title_slug = str(payload.get("titleSlug") or "").strip()
    return {
        "cover_url": pick_cover_url(base_url, payload) or "",
        "item_url": f"{base_url.rstrip('/')}/series/{title_slug}"
        if title_slug
        else f"{base_url.rstrip('/')}/series/{iid}",
    }


def resolve_item_meta_by_key(
    base_url: str,
    api_key: str,
    timeout_seconds: int,
    verify_ssl: bool,
    app_type: str,
    item_key: str,
) -> dict[str, str]:
    app = str(app_type or "").strip().lower()
    key = str(item_key or "").strip().lower()
    if app == "radarr" and key.startswith("movie:"):
        try:
            movie_id = int(key.split(":", 1)[1] or 0)
        except (TypeError, ValueError):
            return {"cover_url": "", "item_url": ""}
        return resolve_movie_item_meta(base_url, api_key, timeout_seconds, verify_ssl, movie_id)
    if app != "sonarr":
        return {"cover_url": "", "item_url": ""}
    if key.startswith("series:") or key.startswith("season:"):
        parts = key.split(":")
        if len(parts) < 2:
            return {"cover_url": "", "item_url": ""}
        try:
            series_id = int(parts[1] or 0)
        except (TypeError, ValueError):
            return {"cover_url": "", "item_url": ""}
        return resolve_series_item_meta(base_url, api_key, timeout_seconds, verify_ssl, series_id)
    if not key.startswith("episode:"):
        return {"cover_url": "", "item_url": ""}
    try:
        episode_id = int(key.split(":", 1)[1] or 0)
    except (TypeError, ValueError):
        return {"cover_url": "", "item_url": ""}
    if episode_id <= 0:
        return {"cover_url": "", "item_url": ""}
    episode_raw = _arr_json_request(base_url, api_key, timeout_seconds, verify_ssl, f"/api/v3/episode/{episode_id}")
    episode = episode_raw if isinstance(episode_raw, dict) else None
    series_id = int((episode or {}).get("seriesId") or 0)
    if series_id <= 0:
        return {"cover_url": "", "item_url": ""}
    return resolve_series_item_meta(base_url, api_key, timeout_seconds, verify_ssl, series_id)
