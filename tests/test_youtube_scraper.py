"""Tests for the YouTube Data API v3 scraper.

The tests use canned API payloads modeled on the real Data API v3
response shapes (``search.list`` + ``videos.list``) and a mocked HTTP
transport, so they run offline. We verify that:

* The scraper short-circuits to [] when disabled or when no API key
  is configured (the key lives in ``YOUTUBE_API_KEY``; the radar must
  never fail its whole fetch because one source has no key yet).
* A search + batched videos.list round trip maps into ContentItems
  with video metrics in metadata, deduplicated by video id across
  queries, and costs exactly one search call per query plus one
  videos.list call (the quota ledger: 5 x 100 + 1 x 1 = 501/day).
* A 403 ``quotaExceeded`` degrades to [] with no retry; any other
  403 or HTTP error degrades the same way.
* A videos.list failure does not drop the videos: counts stay 0.
* Stale videos (older than ``since``) are filtered out.
* ISO 8601 durations (``PT15M33S``) parse to seconds.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from src.models import SourceType, YoutubeConfig
from src.scrapers.youtube import YoutubeScraper, parse_iso8601_duration

TEST_KEY = "TEST_YOUTUBE_API_KEY"


def _search_item(*, video_id="dQw4w9WgXcQ", title="测试视频", hours_ago=1,
                 channel="测试频道", channel_id="UCtest123",
                 desc="视频简介"):
    now = datetime.now(timezone.utc)
    published = (now - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "kind": "youtube#searchResult",
        "id": {"kind": "youtube#video", "videoId": video_id},
        "snippet": {
            "publishedAt": published,
            "channelId": channel_id,
            "title": title,
            "description": desc,
            "channelTitle": channel,
        },
    }


def _detail_item(*, video_id="dQw4w9WgXcQ", view="1758230",
                 like="123456", comment="7890", duration="PT15M33S"):
    return {
        "kind": "youtube#video",
        "id": video_id,
        "statistics": {
            "viewCount": view,
            "likeCount": like,
            "commentCount": comment,
        },
        "contentDetails": {"duration": duration},
    }


def _make_scraper(handler, monkeypatch, **overrides):
    monkeypatch.setenv("YOUTUBE_API_KEY", TEST_KEY)
    cfg = {
        "enabled": True,
        "queries": ["看职场", "带队伍"],
        "max_results": 10,
        "api_key_env": "YOUTUBE_API_KEY",
        "category": "视频",
        "profile": "renxing",
    }
    cfg.update(overrides)
    config = YoutubeConfig(**cfg)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return YoutubeScraper(config, client)


def _handler_factory(search_by_query=None, details=None,
                     search_status=200, videos_status=200,
                     search_reason=None):
    """Route /search and /videos to canned payloads.

    ``search_by_query`` maps a query string to its search items.
    ``details`` maps a video id to its videos.list record.
    """
    search_by_query = search_by_query or {}
    details = details or {}
    calls = {"search": 0, "videos": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/youtube/v3/search" in url:
            calls["search"] += 1
            if search_status != 200:
                payload = {"error": {"errors": [{"reason": search_reason or ""}]}}
                return httpx.Response(search_status, json=payload)
            query = request.url.params.get("q", "")
            items = search_by_query.get(query, [])
            return httpx.Response(200, json={"items": items})
        if "/youtube/v3/videos" in url:
            calls["videos"] += 1
            if videos_status != 200:
                return httpx.Response(videos_status, json={})
            wanted = request.url.params.get("id", "").split(",")
            items = [details[v] for v in wanted if v in details]
            return httpx.Response(200, json={"items": items})
        return httpx.Response(404, json={})

    handler.calls = calls
    return handler


def _since_24h():
    return datetime.now(timezone.utc) - timedelta(hours=24)


class TestFetchGuards:
    def test_returns_empty_when_disabled(self, monkeypatch):
        scraper = _make_scraper(_handler_factory(), monkeypatch, enabled=False)
        assert asyncio.run(scraper.fetch(_since_24h())) == []

    def test_returns_empty_without_key(self, monkeypatch):
        monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
        config = YoutubeConfig(enabled=True, queries=["看职场"])
        client = httpx.AsyncClient(transport=httpx.MockTransport(_handler_factory()))
        scraper = YoutubeScraper(config, client)
        assert asyncio.run(scraper.fetch(_since_24h())) == []


class TestRoundTrip:
    def test_search_plus_videos_maps_to_items(self, monkeypatch):
        fresh_a = _search_item(video_id="AAA111", title="职场视频", hours_ago=2)
        fresh_b = _search_item(video_id="BBB222", title="带队伍视频",
                               hours_ago=3, channel="另一频道",
                               channel_id="UCother")
        handler = _handler_factory(
            search_by_query={"看职场": [fresh_a], "带队伍": [fresh_b]},
            details={
                "AAA111": _detail_item(video_id="AAA111"),
                "BBB222": _detail_item(video_id="BBB222", view="999",
                                       like="11", comment="2",
                                       duration="PT1H2M3S"),
            },
        )
        scraper = _make_scraper(handler, monkeypatch)
        result = asyncio.run(scraper.fetch(_since_24h()))
        assert len(result) == 2
        by_id = {i.metadata["video_id"]: i for i in result}
        item_a = by_id["AAA111"]
        assert item_a.source_type == SourceType.YOUTUBE
        assert item_a.title == "职场视频"
        assert str(item_a.url) == "https://www.youtube.com/watch?v=AAA111"
        assert item_a.author == "测试频道"
        assert item_a.content == "视频简介"
        assert item_a.id == "youtube:video:AAA111"
        assert item_a.metadata["platform"] == "youtube"
        assert item_a.metadata["is_video"] is True
        assert item_a.metadata["view_count"] == 1758230
        assert item_a.metadata["like_count"] == 123456
        assert item_a.metadata["comment_count"] == 7890
        assert item_a.metadata["duration_sec"] == 933  # PT15M33S
        assert item_a.metadata["query"] == "看职场"
        item_b = by_id["BBB222"]
        assert item_b.metadata["duration_sec"] == 3723  # PT1H2M3S
        assert item_b.metadata["view_count"] == 999
        # Quota ledger: 2 search calls + 1 batched videos.list call.
        assert handler.calls["search"] == 2
        assert handler.calls["videos"] == 1

    def test_deduplicates_same_video_across_queries(self, monkeypatch):
        shared = _search_item(video_id="AAA111", title="两栏都命中", hours_ago=1)
        handler = _handler_factory(
            search_by_query={"看职场": [shared], "带队伍": [shared]},
            details={"AAA111": _detail_item(video_id="AAA111")},
        )
        scraper = _make_scraper(handler, monkeypatch)
        result = asyncio.run(scraper.fetch(_since_24h()))
        assert len(result) == 1
        assert result[0].metadata["video_id"] == "AAA111"

    def test_filters_out_stale_videos(self, monkeypatch):
        stale = _search_item(video_id="OLD111", hours_ago=24 * 30)
        fresh = _search_item(video_id="NEW222", hours_ago=2)
        handler = _handler_factory(
            search_by_query={"看职场": [stale, fresh], "带队伍": []},
            details={"NEW222": _detail_item(video_id="NEW222")},
        )
        scraper = _make_scraper(handler, monkeypatch)
        result = asyncio.run(scraper.fetch(_since_24h()))
        assert [i.metadata["video_id"] for i in result] == ["NEW222"]

    def test_videos_list_failure_keeps_items_with_zero_counts(self, monkeypatch):
        fresh = _search_item(video_id="AAA111", hours_ago=1)
        handler = _handler_factory(
            search_by_query={"看职场": [fresh], "带队伍": []},
            videos_status=500,
        )
        scraper = _make_scraper(handler, monkeypatch)
        result = asyncio.run(scraper.fetch(_since_24h()))
        assert len(result) == 1
        assert result[0].metadata["view_count"] == 0
        assert result[0].metadata["duration_sec"] is None


class TestQuotaAndErrors:
    def test_quota_exceeded_degrades_to_empty(self, monkeypatch):
        handler = _handler_factory(search_status=403,
                                   search_reason="quotaExceeded")
        scraper = _make_scraper(handler, monkeypatch)
        assert asyncio.run(scraper.fetch(_since_24h())) == []

    def test_other_forbidden_degrades_to_empty(self, monkeypatch):
        handler = _handler_factory(search_status=403,
                                   search_reason="accessNotConfigured")
        scraper = _make_scraper(handler, monkeypatch)
        assert asyncio.run(scraper.fetch(_since_24h())) == []

    def test_empty_search_results(self, monkeypatch):
        handler = _handler_factory(search_by_query={})
        scraper = _make_scraper(handler, monkeypatch)
        assert asyncio.run(scraper.fetch(_since_24h())) == []

    def test_skips_malformed_hits(self, monkeypatch):
        broken = {"id": {"videoId": "BROKEN1"}, "snippet": {"title": ""}}
        fresh = _search_item(video_id="AAA111", hours_ago=1)
        handler = _handler_factory(
            search_by_query={"看职场": [broken, fresh], "带队伍": []},
            details={"AAA111": _detail_item(video_id="AAA111")},
        )
        scraper = _make_scraper(handler, monkeypatch)
        result = asyncio.run(scraper.fetch(_since_24h()))
        assert len(result) == 1


class TestDurationParser:
    def test_parse_iso8601_duration(self):
        assert parse_iso8601_duration("PT15M33S") == 933
        assert parse_iso8601_duration("PT1H2M3S") == 3723
        assert parse_iso8601_duration("PT45S") == 45
        assert parse_iso8601_duration("PT2H") == 7200
        assert parse_iso8601_duration("P1DT2H") == 93600
        assert parse_iso8601_duration("not-a-duration") is None
        assert parse_iso8601_duration("PT") is None
        assert parse_iso8601_duration(None) is None
        assert parse_iso8601_duration(123) is None
