"""Tests for P318-9 in src/orchestrator.py (cross-run event memory).

  - The history file round-trips; matches use the same 0.10 character
    n-gram threshold as the in-run proposer; entries older than 48h
    do not match.
  - A new item matching a 48h entry is tagged follow_up and loses 1 point.
  - Surviving items are appended to the history (deduped by title+day).
  - An orchestrator without storage silently skips history (test doubles).
"""
import asyncio
import io
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from rich.console import Console

import src.orchestrator as orchestrator_module
from src.models import (
    ClassificationResult,
    ContentAnalysis,
    ContentItem,
    ProcessingResult,
    SourceType,
)
from src.orchestrator import (
    HorizonOrchestrator,
    find_history_match,
    load_event_history,
    save_event_history,
)

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)

SUMMARY = (
    "国家税务总局 11 月 11 日披露，离岸信托收益征税过渡期已平稳结束，"
    "累计申报个税超 1600 亿元，其中 10 月单月入库超 1200 亿元。数字很大，"
    "但官方只给了总量，没有披露具体纳税人结构、补税人群画像和征管细节。"
)


def _item(item_id, title, summary, *, score=7.0, url="https://example.com/x"):
    return ContentItem(
        id=item_id,
        source_type=SourceType.RSS,
        title=title,
        url=url,
        content=summary,
        published_at=NOW,
        processing=ProcessingResult(
            classification=ClassificationResult(
                profile="renxing", method="source_override"
            ),
            analysis=ContentAnalysis(score=score, reason="r", summary=summary),
        ),
    )


def _entry(title, summary, *, hours_ago=0, event_group="evt:seed"):
    return {
        "title": title,
        "summary": summary,
        "event_group": event_group,
        "time_anchor": "2026-11-11",
        "date": (NOW - timedelta(hours=hours_ago)).isoformat(),
    }


def test_load_save_history_roundtrip_and_corrupt(tmp_path):
    path = tmp_path / "event_history.json"
    assert load_event_history(path) == []
    save_event_history(path, [_entry("标题", SUMMARY)])
    loaded = load_event_history(path)
    assert len(loaded) == 1
    assert loaded[0]["title"] == "标题"
    path.write_text("{not json", encoding="utf-8")
    assert load_event_history(path) == []


def test_find_history_match_above_threshold():
    item = _item("rss:a:1", "离岸信托补税 1600 亿：谁在补", SUMMARY)
    match = find_history_match(item, [_entry("离岸信托补税 1600 亿", SUMMARY)],
                               NOW)
    assert match is not None
    assert match["event_group"] == "evt:seed"


def test_find_history_match_ignores_unrelated_and_expired():
    item = _item("rss:a:1", "短剧三次洗牌：谁在扩产，谁在退场",
                 "短剧行业经历三次洗牌，头部公司扩产，中小公司退场。")
    assert find_history_match(
        item, [_entry("离岸信托补税 1600 亿", SUMMARY)], NOW) is None
    fresh_item = _item("rss:a:2", "离岸信托补税 1600 亿：谁在补", SUMMARY)
    assert find_history_match(
        fresh_item, [_entry("离岸信托补税 1600 亿", SUMMARY, hours_ago=49)],
        NOW) is None


def _orchestrator_with_storage(tmp_path):
    orch = object.__new__(HorizonOrchestrator)
    orch.console = Console(file=io.StringIO(), width=100)
    orch.config = SimpleNamespace(ai=SimpleNamespace())
    orch.storage = SimpleNamespace(data_dir=tmp_path)
    return orch


def test_cluster_events_marks_history_follow_up_and_deducts_one(
        monkeypatch, tmp_path):
    orch = _orchestrator_with_storage(tmp_path)
    save_event_history(
        tmp_path / "event_history.json",
        [_entry("离岸信托补税 1600 亿：谁在补，补的是什么", SUMMARY)])
    monkeypatch.setattr(
        orchestrator_module, "create_ai_client",
        lambda cfg: (_ for _ in ()).throw(AssertionError("no AI call expected")))
    item = _item("rss:baidu:9", "离岸信托补税 1600 亿：谁在补，补的是什么",
                 SUMMARY, score=7.0)
    result = asyncio.run(orch.cluster_events([item], log=False))
    assert len(result) == 1
    assert result[0].processing.analysis.score == 6.0
    assert result[0].metadata["event_relation"] == "follow_up"
    assert result[0].metadata["event_group"] == "evt:seed"
    assert result[0].metadata["event_history_match"] == \
        "离岸信托补税 1600 亿：谁在补，补的是什么"
    # Same title + same day: the append dedupes, history stays at 1 entry.
    assert len(load_event_history(tmp_path / "event_history.json")) == 1


def test_cluster_events_appends_new_items_to_history(monkeypatch, tmp_path):
    orch = _orchestrator_with_storage(tmp_path)
    monkeypatch.setattr(
        orchestrator_module, "create_ai_client",
        lambda cfg: (_ for _ in ()).throw(AssertionError("no AI call expected")))
    items = [
        _item("rss:a:1", "店主代买彩票中 825 万私自兑奖", "店主代买彩票中 825 万。"),
        _item("rss:b:2", "短剧三次洗牌：谁在扩产，谁在退场",
              "短剧行业经历三次洗牌，头部公司扩产，中小公司退场。"),
    ]
    result = asyncio.run(orch.cluster_events(items, log=False))
    assert [i.id for i in result] == ["rss:a:1", "rss:b:2"]
    history = load_event_history(tmp_path / "event_history.json")
    assert [e["title"] for e in history] == [
        "店主代买彩票中 825 万私自兑奖", "短剧三次洗牌：谁在扩产，谁在退场"]


def test_cluster_events_without_storage_ignores_history(monkeypatch):
    orch = object.__new__(HorizonOrchestrator)
    orch.console = Console(file=io.StringIO(), width=100)
    orch.config = SimpleNamespace(ai=SimpleNamespace())
    monkeypatch.setattr(
        orchestrator_module, "create_ai_client",
        lambda cfg: (_ for _ in ()).throw(AssertionError("no AI call expected")))
    item = _item("rss:a:1", "离岸信托补税 1600 亿：谁在补", SUMMARY, score=7.0)
    result = asyncio.run(orch.cluster_events([item], log=False))
    assert result[0].processing.analysis.score == 7.0
    assert "event_relation" not in result[0].metadata
