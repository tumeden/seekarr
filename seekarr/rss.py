import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from urllib.parse import quote

import requests

from .config import IndexerConfig

NEWZNAB_NS = "http://www.newznab.com/DTD/2010/feeds/attributes/"


@dataclass(frozen=True)
class Release:
    guid: str
    title: str
    link: str
    indexer: str
    media_type: str
    tmdb_id: int
    tvdb_id: int
    imdb_id: str
    season: int
    episode: int
    year: int


def _safe_int(value: str | None) -> int:
    try:
        return int(str(value or "").strip())
    except (TypeError, ValueError):
        return 0


def _extract_year(text: str) -> int:
    match = re.search(r"\b(19\d{2}|20\d{2})\b", text or "")
    return int(match.group(1)) if match else 0


def _extract_sxe(title: str) -> tuple[int, int]:
    match = re.search(r"[Ss](\d{1,2})[Ee](\d{1,3})", title or "")
    if not match:
        return 0, 0
    return int(match.group(1)), int(match.group(2))


def _parse_newznab_attrs(item: ET.Element) -> dict[str, str]:
    attrs: dict[str, str] = {}
    for child in item:
        tag = child.tag.split("}", 1)[-1].lower()
        if tag != "attr":
            continue
        name = (child.get("name") or "").strip().lower()
        value = (child.get("value") or "").strip()
        if name and value:
            attrs[name] = value
    return attrs


def _parse_xml(xml_text: str, indexer: IndexerConfig) -> list[Release]:
    root = ET.fromstring(xml_text)
    items = root.findall(".//item")
    out: list[Release] = []
    for item in items:
        title = (item.findtext("title") or "").strip()
        if not title:
            continue
        guid = (item.findtext("guid") or "").strip()
        link = (item.findtext("link") or "").strip()
        if not guid:
            guid = link or title

        attrs = _parse_newznab_attrs(item)
        season = _safe_int(attrs.get("season"))
        episode = _safe_int(attrs.get("episode"))
        if not season and not episode:
            season, episode = _extract_sxe(title)

        release = Release(
            guid=guid,
            title=title,
            link=link,
            indexer=indexer.name,
            media_type=indexer.media_type,
            tmdb_id=_safe_int(attrs.get("tmdbid") or attrs.get("tmdb")),
            tvdb_id=_safe_int(attrs.get("tvdbid") or attrs.get("tvdb")),
            imdb_id=(attrs.get("imdbid") or attrs.get("imdb") or "").lower(),
            season=season,
            episode=episode,
            year=_extract_year(title),
        )
        out.append(release)
    return out


def fetch_indexer_releases(
    indexer: IndexerConfig,
    timeout_seconds: int,
    verify_ssl: bool,
    logger: logging.Logger,
) -> list[Release]:
    if not indexer.enabled or not indexer.enable_rss:
        return []

    if indexer.feed_url:
        candidate_urls = [indexer.feed_url]
    else:
        api_key = indexer.api_key.strip()
        if not api_key or not indexer.url:
            logger.warning("Indexer %s missing api config", indexer.name)
            return []
        cat_list = indexer.categories
        if not cat_list:
            cat_list = (
                ["5000", "5010", "5020", "5030", "5040", "5045", "5050", "5060", "5070"]
                if indexer.media_type == "tv"
                else ["2000", "2010", "2020", "2030", "2040", "2045", "2050", "2060"]
            )
        cat_str = ",".join(cat_list)
        base = indexer.url.rstrip("/") + indexer.api_path
        t_param = "tvsearch" if indexer.media_type == "tv" else "movie"
        candidate_urls = [
            f"{base}?t={t_param}&cat={cat_str}&extended=1&apikey={quote(api_key)}&limit=100",
            f"{base}?t=search&cat={cat_str}&extended=1&apikey={quote(api_key)}&limit=100",
        ]

    for url in candidate_urls:
        try:
            response = requests.get(
                url,
                timeout=timeout_seconds,
                verify=verify_ssl,
            )
            if response.status_code != 200:
                logger.warning("RSS fetch failed for %s: HTTP %s", indexer.name, response.status_code)
                continue
            if not response.text.strip():
                continue
            releases = _parse_xml(response.text, indexer)
            if releases:
                return releases
        except ET.ParseError:
            logger.warning("RSS XML parse failed for %s", indexer.name)
        except requests.RequestException as exc:
            logger.warning("RSS request failed for %s: %s", indexer.name, exc)
    return []
