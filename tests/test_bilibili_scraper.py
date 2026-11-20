"""Tests for the Bilibili ranking/popular scraper.

The tests use canned API payloads modeled on the real responses captured
2026-11-20 (see ``~/workspace/radar-lines-1120/raw/bili-rank0-r1.json``
and ``bili-popular-r1.json``) and a mocked HTTP transport, so they run
offline. We verify that:

* The scraper short-circuits to [] when disabled.
* It correctly maps ranking/popular video records into ContentItems,
  with video metrics in metadata.
* It filters out videos older than ``since`` (the per-partition ranking
  lists froze 2025-04-02, so the filter is what keeps them out).
* It deduplicates the same BV id across ranking + popular.
* A bad code (-400, e.g. rid=201) or a malformed record degrades to []
  for that call instead of crashing the run.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from src.models import BilibiliConfig, SourceType
from src.scrapers.bilibili import BilibiliScraper


def _video(*, bvid="BV1soZJYUEKd", title="测试视频", hours_ago=1,
           view=5164591, like=707486, duration=45, tid=124,
           tname="社科·法律·心理", author="央视新闻", desc="简介"):
    now = datetime.now(timezone.utc)
    return {
        "aid": 114256531955521,
        "bvid": bvid,
        "title": title,
        "desc": desc,
        "pic": "https://example.com/cover.jpg",
        "pubdate": int((now - timedelta(hours=hours_ago)).timestamp()),
        "duration": duration,
        "tid": tid,
        "tname": tname,
        "owner": {"mid": 456664753, "name": author, "face": ""},
        "stat": {
            "view": view,
            "danmaku": 21605,
            "reply": 12631,
            "favorite": 33590,
            "coin": 17688,
            "share": 27845,
            "like": like,
        },
    }


def _ranking_payload(videos):
    return {"code": 0, "message": "0", "ttl": 1, "data": {"list": videos}}


def _popular_payload(videos):
    return {"code": 0, "message": "0", "ttl": 1, "data": {"list": videos}}


def _make_scraper(handler, **overrides):
    cfg = {
        "enabled": True,
        "rids": [0],
        "include_popular": True,
        "popular_page_size": 20,
        "min_view": 0,
        "profile": "renxing",
    }
    cfg.update(overrides)
    config = BilibiliConfig(**cfg)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return BilibiliScraper(config, client)


def _handler_factory(rank_videos=None, popular_videos=None, rank_code=0):
    rank_videos = rank_videos if rank_videos is not None else []
    popular_videos = popular_videos if popular_videos is not None else []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "ranking/v2" in url:
            payload = _ranking_payload(rank_videos)
            if rank_code != 0:
                payload = {"code": rank_code, "message": "请求错误", "data": None}
            return httpx.Response(200, json=payload)
        if "popular" in url:
            return httpx.Response(200, json=_popular_payload(popular_videos))
        return httpx.Response(404, json={})

    return handler


class TestFetchGuards:
    def test_returns_empty_when_disabled(self):
        scraper = _make_scraper(_handler_factory(), enabled=False)
        since = datetime.now(timezone.utc) - timedelta(hours=24)
        assert asyncio.run(scraper.fetch(since)) == []

    def test_bad_ranking_code_degrades_to_popular_only(self):
        fresh = _video()
        scraper = _make_scraper(
            _handler_factory(rank_videos=[fresh], popular_videos=[fresh],
                             rank_code=-400)
        )
        since = datetime.now(timezone.utc) - timedelta(hours=24)
        result = asyncio.run(scraper.fetch(since))
        # Same BV in both lists -> deduplicated to one item.
        assert len(result) == 1
        assert result[0].metadata["bvid"] == "BV1soZJYUEKd"


class TestMapping:
    def test_maps_video_fields_to_content_item(self):
        fresh = _video()
        scraper = _make_scraper(
            _handler_factory(rank_videos=[fresh], popular_videos=[])
        )
        since = datetime.now(timezone.utc) - timedelta(hours=24)
        result = asyncio.run(scraper.fetch(since))
        assert len(result) == 1
        item = result[0]
        assert item.source_type == SourceType.BILIBILI
        assert item.title == "测试视频"
        assert str(item.url) == "https://www.bilibili.com/video/BV1soZJYUEKd"
        assert item.author == "央视新闻"
        assert item.content == "简介"
        assert item.id == "bilibili:video:BV1soZJYUEKd"
        assert item.metadata["platform"] == "bilibili"
        assert item.metadata["is_video"] is True
        assert item.metadata["view_count"] == 5164591
        assert item.metadata["like_count"] == 707486
        assert item.metadata["duration_sec"] == 45
        assert item.metadata["partition_name"] == "社科·法律·心理"

    def test_filters_out_stale_videos(self):
        # Per-partition ranking froze 2025-04-02: old items must not pass.
        stale = _video(bvid="BVOLD1234567", hours_ago=24 * 400)
        fresh = _video(bvid="BVFRESH12345", hours_ago=2)
        scraper = _make_scraper(
            _handler_factory(rank_videos=[stale, fresh], popular_videos=[])
        )
        since = datetime.now(timezone.utc) - timedelta(hours=24)
        result = asyncio.run(scraper.fetch(since))
        assert [i.metadata["bvid"] for i in result] == ["BVFRESH12345"]

    def test_skips_malformed_records(self):
        broken = {"title": "no bvid, no pubdate"}
        fresh = _video()
        scraper = _make_scraper(
            _handler_factory(rank_videos=[broken, fresh], popular_videos=[])
        )
        since = datetime.now(timezone.utc) - timedelta(hours=24)
        result = asyncio.run(scraper.fetch(since))
        assert len(result) == 1

    def test_min_view_filter(self):
        low = _video(bvid="BVLOW0000001", view=100)
        high = _video(bvid="BVHIGH000001", view=500000)
        scraper = _make_scraper(
            _handler_factory(rank_videos=[low, high], popular_videos=[]),
            min_view=10000,
        )
        since = datetime.now(timezone.utc) - timedelta(hours=24)
        result = asyncio.run(scraper.fetch(since))
        assert [i.metadata["bvid"] for i in result] == ["BVHIGH000001"]

    def test_http_error_degrades_to_empty(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={})

        scraper = _make_scraper(handler)
        since = datetime.now(timezone.utc) - timedelta(hours=24)
        assert asyncio.run(scraper.fetch(since)) == []
