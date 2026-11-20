"""YouTube Data API v3 scraper.

Pulls fresh video candidates from the official YouTube Data API v3
(``search.list`` per query + one batched ``videos.list`` call for
statistics) and maps them into ContentItems so the rest of the Horizon
pipeline (deduplication, AI scoring, enrichment, summarization) treats
them the same way as Bilibili or RSS items.

Why Data API v3 and not channel RSS (decision, 2026-11-20):

* ``youtube.com/robots.txt`` explicitly disallows ``/feeds/videos.xml``.
  Using it would repeat the pattern this project already stopped for
  Sogou probing and Weibo ajax hotlists: the file says no, so we do
  not fetch it. Channel RSS entries stay ``enabled: false`` in
  ``data/config.renxing.json``.
* Data API v3 is the official, documented, key-based API. A free
  Google Cloud project gets 10,000 quota units/day; no card is needed
  for the key. The key lives in the GitHub Secret ``YOUTUBE_API_KEY``
  and is read from the environment at fetch time -- it is never written
  into the repo, the config, or any log.

Quota ledger (per daily run, defaults: 5 queries x maxResults 10):

* ``search.list`` costs 100 units per call: 5 x 100 = 500 units.
* ``videos.list`` costs 1 unit per call (one batched call for up to
  50 video ids): 1 x 1 = 1 unit.
* Total: 501 units/day, about 5% of the 10,000-unit daily quota.
  Growing the query list adds 100 units per query; keep it small.

What the API does NOT do (do not configure it this way):

* ``chart=mostPopular`` + ``regionCode=CN`` returns nothing useful:
  YouTube is not available in mainland China, so that combination
  yields no items. Keyword search with ``order=date`` is the source.
* ``order=relevance`` is not the default: ``date`` is what guarantees
  the 24 h window is fresh.

Design notes:

* No key -> return [] with a warning and let the other sources run.
  The radar must never fail its whole fetch because one source has
  no key yet.
* Quota exhausted (403 ``quotaExceeded``) -> return [] with a warning
  that says the quota resets tomorrow or the key can be swapped. No
  retry loop: hammering a dead quota only burns the run's time budget.
* Any other HTTP error / malformed payload degrades to [] for that
  query instead of aborting the batch.
* No full-text extraction: search snippets already carry title +
  description, and statistics come from the batched videos.list call,
  so the scraper never calls trafilatura. This keeps the fetch cheap --
  the 2026-11-18 run spent 761 s in RSS extraction, and the radar's
  60-minute Actions window is already tight.
* Video metrics ride in ``ContentItem.metadata`` (``view_count``,
  ``like_count``, ``duration_sec`` ...); ``ContentItem`` itself needs
  no model change.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, List, Optional

import httpx

from .base import BaseScraper
from ..models import ContentItem, SourceType, YoutubeConfig

logger = logging.getLogger(__name__)

# ISO 8601 durations as returned by the API, e.g. "PT15M33S", "PT1H2M3S",
# "PT45S". Days are not expected for ordinary videos; "P1DT..." is still
# handled so a long upload does not crash the parser.
_DURATION_RE = re.compile(
    r"^P(?:(?P<days>\d+)D)?"
    r"(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$"
)


def parse_iso8601_duration(value: Any) -> Optional[int]:
    """Parse an ISO 8601 duration (``PT15M33S``) to seconds.

    Returns None for anything unparseable instead of raising.
    """
    if not isinstance(value, str):
        return None
    match = _DURATION_RE.match(value.strip())
    if not match:
        return None
    parts = match.groupdict()
    if not any(parts.values()):
        return None
    days = int(parts["days"] or 0)
    hours = int(parts["hours"] or 0)
    minutes = int(parts["minutes"] or 0)
    seconds = int(parts["seconds"] or 0)
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def _to_int(value: Any) -> int:
    """API counts arrive as strings (\"1758230\"); tolerate bad values."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


class YoutubeScraper(BaseScraper):
    """Scraper backed by the YouTube Data API v3 (search + videos)."""

    SOURCE_TYPE = SourceType.YOUTUBE
    BASE_URL = "https://www.googleapis.com/youtube/v3"
    SEARCH_URL = BASE_URL + "/search"
    VIDEOS_URL = BASE_URL + "/videos"

    # Default queries: the five renxing columns. Each costs one
    # search.list call (100 quota units).
    DEFAULT_QUERIES = ["看职场", "带队伍", "做生意", "做销售", "去创业"]

    def __init__(self, config: YoutubeConfig, http_client: httpx.AsyncClient):
        """Initialize the scraper.

        Args:
            config: YouTube source configuration.
            http_client: Shared async HTTP client.
        """
        super().__init__({"youtube": config}, http_client)
        self.youtube_config = config

    @property
    def api_key(self) -> str:
        """API key from the environment; empty string when not set."""
        env_name = self.youtube_config.api_key_env or "YOUTUBE_API_KEY"
        return (os.getenv(env_name) or "").strip()

    async def fetch(self, since: datetime) -> List[ContentItem]:
        """Fetch fresh videos for each configured query.

        Args:
            since: Only fetch videos published after this time.

        Returns:
            List[ContentItem]: Fetched video items, deduplicated by
            video id. [] when disabled, when no key is configured, or
            when every query fails.
        """
        if not self.youtube_config.enabled:
            return []
        if not self.api_key:
            logger.warning(
                "YouTube Data API key not configured "
                "(env %s empty); skipping YouTube source.",
                self.youtube_config.api_key_env or "YOUTUBE_API_KEY",
            )
            return []

        since_utc = since if since.tzinfo else since.replace(tzinfo=timezone.utc)
        queries = self.youtube_config.queries or list(self.DEFAULT_QUERIES)

        results = await asyncio.gather(
            *(self._search(query, since_utc) for query in queries),
            return_exceptions=True,
        )

        # Collect search hits per query, deduplicated by video id.
        hits: dict[str, dict] = {}
        for query, result in zip(queries, results):
            if isinstance(result, Exception):
                logger.warning("YouTube search q=%s failed: %s", query, result)
                continue
            for raw in result or []:
                video_id = (raw.get("id") or {}).get("videoId", "")
                if video_id and video_id not in hits:
                    hits[video_id] = {"query": query, "snippet": raw.get("snippet") or {}}

        if not hits:
            return []

        # One batched videos.list call (1 quota unit) for statistics.
        details = await self._fetch_details(list(hits.keys()))

        items: List[ContentItem] = []
        for video_id, hit in hits.items():
            item = self._raw_to_item(video_id, hit, details.get(video_id))
            if item is None:
                continue
            if item.published_at < since_utc:
                continue
            items.append(item)
        return items

    async def _search(self, query: str, since: datetime) -> List[dict]:
        """One search.list call; an error degrades to [] for this query."""
        params = {
            "part": "snippet",
            "q": query,
            "type": "video",
            "order": "date",
            "publishedAfter": since.astimezone(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            "maxResults": self.youtube_config.max_results,
            "key": self.api_key,
        }
        try:
            response = await self.client.get(
                self.SEARCH_URL, params=params, follow_redirects=True
            )
        except httpx.HTTPError as exc:
            logger.warning("YouTube search q=%s HTTP error: %s", query, exc)
            return []
        return self._parse_response(response, context=f"search q={query}")

    async def _fetch_details(self, video_ids: List[str]) -> dict:
        """Batched videos.list call (<=50 ids, 1 quota unit)."""
        if not video_ids:
            return {}
        try:
            response = await self.client.get(
                self.VIDEOS_URL,
                params={
                    "part": "statistics,contentDetails,snippet",
                    "id": ",".join(video_ids[:50]),
                    "key": self.api_key,
                },
                follow_redirects=True,
            )
        except httpx.HTTPError as exc:
            logger.warning("YouTube videos.list HTTP error: %s", exc)
            return {}
        payload_items = self._parse_response(response, context="videos.list")
        details: dict[str, dict] = {}
        for raw in payload_items:
            video_id = (raw.get("id") or "").strip()
            if video_id:
                details[video_id] = raw
        return details

    def _parse_response(self, response: httpx.Response, context: str) -> List[dict]:
        """Parse one API response; any failure degrades to [].

        A 403 with reason ``quotaExceeded`` gets an explicit warning:
        the quota resets tomorrow (Pacific time) or the key can be
        swapped -- retrying inside this run would only burn time.
        """
        if response.status_code == 403:
            reason = ""
            try:
                error = (response.json().get("error") or {}).get("errors") or []
                reason = (error[0].get("reason") if error else "") or ""
            except (ValueError, AttributeError, IndexError, KeyError):
                reason = ""
            if reason == "quotaExceeded":
                logger.warning(
                    "YouTube Data API quota exhausted (%s): "
                    "quota resets tomorrow; swap the key or wait. "
                    "Skipping YouTube for this run.",
                    context,
                )
            else:
                logger.warning(
                    "YouTube Data API forbidden (%s): HTTP 403 reason=%s; "
                    "skipping YouTube for this run.",
                    context,
                    reason or "unknown",
                )
            return []
        if response.status_code != 200:
            logger.warning(
                "YouTube Data API %s HTTP %s; skipping.",
                context,
                response.status_code,
            )
            return []
        try:
            payload = response.json()
        except ValueError as exc:
            logger.warning("YouTube Data API %s bad JSON: %s", context, exc)
            return []
        if not isinstance(payload, dict):
            logger.warning("YouTube Data API %s unexpected payload type.", context)
            return []
        items = payload.get("items")
        if not isinstance(items, list):
            logger.warning("YouTube Data API %s no items list.", context)
            return []
        return items

    def _raw_to_item(
        self, video_id: str, hit: dict, detail: Optional[dict]
    ) -> Optional[ContentItem]:
        """Map one search hit (+ optional videos.list detail) to an item.

        A hit without title/publishedAt is skipped. Statistics are
        best-effort: when videos.list failed, counts stay 0 rather than
        dropping an otherwise valid video.
        """
        snippet = hit.get("snippet") or {}
        title = (snippet.get("title") or "").strip()
        published_raw = snippet.get("publishedAt") or ""
        if not title or not published_raw:
            return None
        try:
            published_at = datetime.fromisoformat(
                published_raw.replace("Z", "+00:00")
            )
        except ValueError:
            return None
        if published_at.tzinfo is None:
            published_at = published_at.replace(tzinfo=timezone.utc)

        channel_title = (snippet.get("channelTitle") or "").strip() or None
        channel_id = (snippet.get("channelId") or "").strip() or None
        description = (snippet.get("description") or "").strip()

        statistics = (detail or {}).get("statistics") or {}
        content_details = (detail or {}).get("contentDetails") or {}
        duration_iso = content_details.get("duration")
        duration_sec = parse_iso8601_duration(duration_iso)

        return ContentItem(
            id=self._generate_id("youtube", "video", video_id),
            source_type=SourceType.YOUTUBE,
            title=title,
            url=f"https://www.youtube.com/watch?v={video_id}",
            content=description or None,
            author=channel_title,
            published_at=published_at,
            profile=self.youtube_config.profile,
            metadata={
                "platform": "youtube",
                "is_video": True,
                "video_id": video_id,
                "channel_id": channel_id,
                "channel_title": channel_title,
                "view_count": _to_int(statistics.get("viewCount")),
                "like_count": _to_int(statistics.get("likeCount")),
                "comment_count": _to_int(statistics.get("commentCount")),
                "duration_iso": duration_iso,
                "duration_sec": duration_sec,
                "query": hit.get("query"),
                "category": self.youtube_config.category,
            },
        )
