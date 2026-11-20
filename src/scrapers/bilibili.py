"""Bilibili ranking/popular scraper.

Pulls video candidates from the key-less Bilibili public APIs
(``/x/web-interface/ranking/v2`` per分区 + ``/x/web-interface/popular``)
and maps them into ContentItems so the rest of the Horizon pipeline
(deduplication, AI scoring, enrichment, summarization) treats them the
same way as RSS or Google News items.

Real-measurement evidence (2026-11-20, two runs, no login, no key;
see ``~/workspace/radar-lines-1120/video-sources-evidence.md`` §4,
plus the follow-up freshness audit in the radar-lines research report):

* ``ranking/v2`` rid=0 answers code 0 with 100 items, 4 of them published
  within 24 h on 2026-11-20. rid=36/160/188 also answer code 0 with
  94-97 items each -- but their newest item is 2025-04-02, i.e. the
  per-partition lists are frozen snapshots, and the 24 h ``since`` filter
  yields 0 from them. ``ranking/region`` (day=3) is stale the same way.
  Only rid=0 is configured by default; other rids stay configurable
  for the day Bilibili unfreezes them.
* ``popular`` returns code 0 with 20 items per page, 8 of them published
  within 24 h on 2026-11-20 -- it is the main daily video source.
* rid=201 （财经） returns code -400 "请求错误" -- do NOT configure it.
* Every item carries full video metrics: view/like/coin/favorite/share/
  danmaku/reply counts, duration in seconds, pubdate as Unix seconds,
  owner name, tid/tname partition. No extra detail call is needed, and
  unlike the video detail endpoint (``/x/web-interface/view``, observed
  412 from the sandbox on 2026-11-17) these two endpoints answer 200.
* ``bilibili.com/robots.txt`` does not disallow the API paths.

Design notes:

* No API key, no login, no cookies. A single malformed video is skipped,
  not allowed to abort the batch; an HTTP error on one rid degrades to
  the other rids instead of failing the whole source.
* No full-text extraction: ranking items already carry title + desc, so
  the scraper never calls trafilatura. This keeps the fetch cheap --
  the 2026-11-18 run spent 761 s in RSS extraction, and the radar's
  60-minute Actions window is already tight.
* Video metrics ride in ``ContentItem.metadata`` (``view_count``,
  ``like_count``, ``duration_sec`` ...); ``ContentItem`` itself needs no
  model change. ``pubdate`` filters the 24 h window; ranking lists are
  heat-ordered, not keyword-ordered, so the AI scorer still decides
  relevance -- the scraper only guarantees the items are real videos
  with real play counts.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, List, Optional

import httpx

from .base import BaseScraper
from ..models import BilibiliConfig, ContentItem, SourceType

logger = logging.getLogger(__name__)


class BilibiliScraper(BaseScraper):
    """Scraper backed by the Bilibili ranking + popular APIs."""

    SOURCE_TYPE = SourceType.BILIBILI
    RANKING_URL = "https://api.bilibili.com/x/web-interface/ranking/v2"
    POPULAR_URL = "https://api.bilibili.com/x/web-interface/popular"

    # Bilibili answers 412 to httpx's default UA ("python-httpx/...") from
    # the sandbox (verified 2026-11-20); a desktop-browser UA + Referer
    # answers 200 on the same calls. This is plain client identification,
    # not a login, cookie, or anti-bot bypass.
    REQUEST_HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0 Safari/537.36"
        ),
        "Referer": "https://www.bilibili.com/",
    }

    # rid=201 （财经） returns code -400; verified 2026-11-20. Only rid=0
    # is fresh enough for a daily run (rid=36/160/188 froze 2025-04-02);
    # keep the default list to rids that yield items inside 24 h.
    DEFAULT_RIDS = (0,)

    def __init__(self, config: BilibiliConfig, http_client: httpx.AsyncClient):
        """Initialize the scraper.

        Args:
            config: Bilibili source configuration.
            http_client: Shared async HTTP client.
        """
        super().__init__({"bilibili": config}, http_client)
        self.bilibili_config = config

    async def fetch(self, since: datetime) -> List[ContentItem]:
        """Fetch video candidates from ranking lists + popular.

        Args:
            since: Only fetch videos published after this time.

        Returns:
            List[ContentItem]: Fetched video items, deduplicated by BV id.
        """
        if not self.bilibili_config.enabled:
            return []

        since_utc = since if since.tzinfo else since.replace(tzinfo=timezone.utc)

        tasks: List[Any] = []
        for rid in self.bilibili_config.rids or list(self.DEFAULT_RIDS):
            tasks.append(self._fetch_ranking(rid))
        if self.bilibili_config.include_popular:
            tasks.append(self._fetch_popular())

        results = await asyncio.gather(*tasks, return_exceptions=True)

        items: List[ContentItem] = []
        seen_bvid: set = set()
        for result in results:
            if isinstance(result, Exception):
                logger.warning("Bilibili fetch task failed: %s", result)
                continue
            for raw in result or []:
                item = self._raw_to_item(raw)
                if item is None:
                    continue
                if item.published_at < since_utc:
                    continue
                bvid = item.metadata.get("bvid")
                if bvid in seen_bvid:
                    continue
                seen_bvid.add(bvid)
                items.append(item)
        return items

    async def _fetch_ranking(self, rid: int) -> List[dict]:
        """Fetch one ranking list; an error degrades to [] for this rid."""
        try:
            response = await self.client.get(
                self.RANKING_URL,
                params={"rid": rid, "type": "all"},
                headers=self.REQUEST_HEADERS,
                follow_redirects=True,
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("Bilibili ranking rid=%s failed: %s", rid, exc)
            return []
        if not isinstance(payload, dict) or payload.get("code") != 0:
            logger.warning(
                "Bilibili ranking rid=%s bad code: %s",
                rid,
                payload.get("code") if isinstance(payload, dict) else type(payload),
            )
            return []
        data = payload.get("data") or {}
        return data.get("list") or []

    async def _fetch_popular(self) -> List[dict]:
        """Fetch the popular list; an error degrades to []."""
        try:
            response = await self.client.get(
                self.POPULAR_URL,
                params={
                    "ps": self.bilibili_config.popular_page_size,
                    "pn": 1,
                },
                headers=self.REQUEST_HEADERS,
                follow_redirects=True,
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("Bilibili popular failed: %s", exc)
            return []
        if not isinstance(payload, dict) or payload.get("code") != 0:
            logger.warning(
                "Bilibili popular bad code: %s",
                payload.get("code") if isinstance(payload, dict) else type(payload),
            )
            return []
        data = payload.get("data") or {}
        return data.get("list") or []

    def _raw_to_item(self, raw: dict) -> Optional[ContentItem]:
        """Map one ranking/popular video record to a ContentItem.

        A record without bvid/title/pubdate is skipped.
        """
        if not isinstance(raw, dict):
            return None
        bvid = (raw.get("bvid") or "").strip()
        title = (raw.get("title") or "").strip()
        pubdate = raw.get("pubdate")
        if not bvid or not title or not isinstance(pubdate, (int, float)):
            return None
        try:
            published_at = datetime.fromtimestamp(pubdate, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None

        stat = raw.get("stat") or {}
        owner = raw.get("owner") or {}
        desc = (raw.get("desc") or "").strip()
        duration = raw.get("duration")
        min_view = self.bilibili_config.min_view or 0
        view_count = stat.get("view") or 0
        if min_view and view_count < min_view:
            return None

        return ContentItem(
            id=self._generate_id("bilibili", "video", bvid),
            source_type=SourceType.BILIBILI,
            title=title,
            url=f"https://www.bilibili.com/video/{bvid}",
            content=desc,
            author=(owner.get("name") or "").strip() or None,
            published_at=published_at,
            profile=self.bilibili_config.profile,
            metadata={
                "platform": "bilibili",
                "is_video": True,
                "bvid": bvid,
                "aid": raw.get("aid"),
                "view_count": view_count,
                "like_count": stat.get("like") or 0,
                "coin_count": stat.get("coin") or 0,
                "favorite_count": stat.get("favorite") or 0,
                "share_count": stat.get("share") or 0,
                "danmaku_count": stat.get("danmaku") or 0,
                "reply_count": stat.get("reply") or 0,
                "duration_sec": duration if isinstance(duration, (int, float)) else None,
                "partition_id": raw.get("tid"),
                "partition_name": raw.get("tname"),
                "category": self.bilibili_config.category,
            },
        )
