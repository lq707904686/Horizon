"""Event clustering: text proposes candidates, the model disposes.

The real-pair fixture is verbatim from the 11-12 brief (gh-pages
``_posts/2026-11-12-summary-zh.md`` items 9/10, "离岸信托补税 1600 亿"),
the pair the old semantic dedup let through twice at 5.5.
"""

import asyncio
import io
import json
from datetime import datetime, timezone
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
from src.orchestrator import HorizonOrchestrator
from src.processing.events import CANDIDATE_THRESHOLD, propose_event_candidates, text_similarity

NOW = datetime(2026, 11, 11, 23, 0, tzinfo=timezone.utc)

SUMMARY_9 = (
    "国家税务总局 11 月 11 日披露，离岸信托收益征税过渡期已平稳结束，"
    "累计申报个税超 1600 亿元，其中 10 月单月入库超 1200 亿元。数字很大，"
    "但官方只给了总量，没有披露具体纳税人结构、补税人群画像和征管细节。"
    "对普通打工人来说，这事离得远；对做跨境资产配置的高净值人群来说，"
    "这是一次实打实的规则落地。"
)
SUMMARY_10 = (
    "离岸信托个税申报累计超 1600 亿元，10 月单月入库超 1200 亿，"
    "直接拉动当月全国个税收入增长 90%。数字很猛，但只有标题级信息，"
    "没有官方通报原文、没有具体纳税人案例、没有一手信源。核心事实是："
    "高净值人群通过离岸信托避税的老路，正在被税务监管追缴。"
)


def _item(item_id, title, summary, *, score=5.5, url="https://example.com/x"):
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


def _orchestrator_with_fake_client(monkeypatch, verdict_json):
    calls = []

    class FakeClient:
        async def complete(self, **kwargs):
            calls.append(kwargs)
            return json.dumps(verdict_json)

    orch = object.__new__(HorizonOrchestrator)
    orch.console = Console(file=io.StringIO(), width=100)
    orch.config = SimpleNamespace(ai=SimpleNamespace())
    monkeypatch.setattr(orchestrator_module, "create_ai_client", lambda cfg: FakeClient())
    return orch, calls


def test_real_1112_pair_is_proposed_as_candidate():
    items = [
        _item("rss:baidu:9", "离岸信托补税 1600 亿：谁在补，补的是什么", SUMMARY_9),
        _item("rss:zhihu:10", "离岸信托补税 1600 亿：高净值人群的避税通道被堵", SUMMARY_10),
    ]
    sim = text_similarity(items[0].title, items[0].content, items[1].title, items[1].content)
    assert sim >= CANDIDATE_THRESHOLD
    assert propose_event_candidates(items) == [[0, 1]]


def test_unrelated_items_are_not_proposed():
    items = [
        _item("rss:a:1", "店主代买彩票中 825 万私自兑奖：选题角度", "店主代买彩票中 825 万，引发关于代买彩票法律责任的讨论。"),
        _item("rss:b:2", "短剧三次洗牌：谁在扩产，谁在退场", "短剧行业经历三次洗牌，头部公司扩产，中小公司退场。"),
        _item("rss:c:3", "赵丽颖起诉红果短剧侵犯肖像权", "赵丽颖起诉红果短剧侵犯肖像权，索赔金额未披露。"),
    ]
    assert propose_event_candidates(items) == []


def test_same_topic_different_figures_is_proposed_for_model_review():
    # Recall-oriented proposal: the model, not the text score, decides that
    # different figures mean follow-up rather than the same event.
    items = [
        _item("rss:baidu:9", "离岸信托补税 1600 亿：谁在补，补的是什么", SUMMARY_9),
        _item(
            "rss:baidu:x",
            "离岸信托补税 800 亿：谁在补，补的是什么",
            "国家税务总局披露，离岸信托收益征税累计申报个税超 800 亿元，"
            "其中 10 月单月入库超 600 亿元。",
        ),
    ]
    assert propose_event_candidates(items) == [[0, 1]]


def test_same_event_verdict_merges_into_highest_scored(monkeypatch):
    items = [
        _item("rss:baidu:9", "离岸信托补税 1600 亿：谁在补，补的是什么", SUMMARY_9, score=6.5),
        _item("rss:zhihu:10", "离岸信托补税 1600 亿：高净值人群的避税通道被堵", SUMMARY_10, score=5.5),
    ]
    orch, calls = _orchestrator_with_fake_client(
        monkeypatch,
        {"verdicts": [{"group": [0, 1], "verdict": "same_event", "reason": "same disclosure"}]},
    )
    result = asyncio.run(orch.cluster_events(items, log=False))
    assert len(calls) == 1
    assert len(result) == 1
    assert result[0].id == "rss:baidu:9"
    assert result[0].metadata["event_group"] == "evt:rss:baidu:9"
    assert result[0].metadata["merged_event_items"] == [
        "离岸信托补税 1600 亿：高净值人群的避税通道被堵"
    ]
    # Heat counted once: one surviving item carries the event.
    assert SUMMARY_10[:20] in (result[0].content or "")


def test_follow_up_verdict_keeps_both_and_tags_group(monkeypatch):
    items = [
        _item("rss:baidu:9", "离岸信托补税 1600 亿：谁在补，补的是什么", SUMMARY_9, score=6.5),
        _item("rss:zhihu:10", "离岸信托补税 1600 亿：高净值人群的避税通道被堵", SUMMARY_10, score=5.5),
    ]
    orch, _ = _orchestrator_with_fake_client(
        monkeypatch,
        {"verdicts": [{"group": [0, 1], "verdict": "follow_up", "reason": "new angle"}]},
    )
    result = asyncio.run(orch.cluster_events(items, log=False))
    assert len(result) == 2
    assert {i.metadata.get("event_group") for i in result} == {"evt:rss:baidu:9"}
    assert all(i.metadata.get("event_relation") == "follow_up" for i in result)


def test_different_verdict_keeps_both_untagged(monkeypatch):
    items = [
        _item("rss:baidu:9", "离岸信托补税 1600 亿：谁在补，补的是什么", SUMMARY_9),
        _item("rss:zhihu:10", "离岸信托补税 1600 亿：高净值人群的避税通道被堵", SUMMARY_10),
    ]
    orch, _ = _orchestrator_with_fake_client(
        monkeypatch,
        {"verdicts": [{"group": [0, 1], "verdict": "different", "reason": "different events"}]},
    )
    result = asyncio.run(orch.cluster_events(items, log=False))
    assert len(result) == 2
    assert all("event_group" not in i.metadata for i in result)


def test_model_failure_keeps_items_unchanged(monkeypatch):
    items = [
        _item("rss:baidu:9", "离岸信托补税 1600 亿：谁在补，补的是什么", SUMMARY_9),
        _item("rss:zhihu:10", "离岸信托补税 1600 亿：高净值人群的避税通道被堵", SUMMARY_10),
    ]

    class FailingClient:
        async def complete(self, **kwargs):
            raise RuntimeError("boom")

    orch = object.__new__(HorizonOrchestrator)
    orch.console = Console(file=io.StringIO(), width=100)
    orch.config = SimpleNamespace(ai=SimpleNamespace())
    monkeypatch.setattr(orchestrator_module, "create_ai_client", lambda cfg: FailingClient())
    result = asyncio.run(orch.cluster_events(items, log=False))
    assert [i.id for i in result] == [i.id for i in items]


def test_no_candidates_makes_no_ai_call(monkeypatch):
    items = [
        _item("rss:a:1", "店主代买彩票中 825 万私自兑奖：选题角度", "店主代买彩票中 825 万。"),
    ]
    orch, calls = _orchestrator_with_fake_client(monkeypatch, {"verdicts": []})
    result = asyncio.run(orch.cluster_events(items, log=False))
    assert calls == []
    assert result == items
