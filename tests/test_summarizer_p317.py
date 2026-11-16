"""Tests for P317 in src/ai/summarizer.py.

  - Placeholder-titled items are dropped from the brief body and TOC
    (not only from hotspots), the drop is counted, and the header count
    matches the rendered items.
  - score_rounds / dimension_scores / evidence_level render into the
    brief body; capped items get an explicit rumor-stance line and an
    event_group line; verified items get neither rumor line.
"""
import asyncio
from datetime import datetime, timezone

from src.ai.summarizer import DailySummarizer
from src.models import (
    ContentAnalysis,
    ContentArtifact,
    ContentBlock,
    ContentItem,
    EvidenceLevel,
    ClassificationResult,
    ProcessingResult,
    SourceType,
)


def _run_async(coro):
    return asyncio.run(coro)


def _make_item(idx, title, *, score=7.5, score_rounds=None, dual_eval=None,
               dimension_scores=None, evidence_level=None,
               cap_triggered=None, video_score=None, event_group=None,
               event_relation=None, opening_type=None):
    analysis = ContentAnalysis(
        score=score,
        reason="test",
        summary=f"Summary for item {idx}.",
        tags=["AI"],
        score_rounds=score_rounds,
        dual_eval=dual_eval,
        dimension_scores=dimension_scores,
        evidence_level=evidence_level,
        cap_triggered=cap_triggered,
        video_score=video_score,
        opening_type=opening_type,
    )
    item = ContentItem(
        id=f"rss:item-{idx}",
        source_type=SourceType.RSS,
        title=title,
        url=f"https://example.com/items/{idx}",
        content="content",
        author="tester",
        published_at=datetime(2026, 4, 25, 8, 0, tzinfo=timezone.utc),
        profile="tech-news",
        processing=ProcessingResult(
            classification=ClassificationResult(
                profile="tech-news", method="source_override"
            ),
            analysis=analysis,
            artifacts={
                language: ContentArtifact(
                    language=language,
                    title=title,
                    blocks=[
                        ContentBlock(
                            id="summary",
                            title="Summary",
                            content=f"Summary for item {idx}.",
                            primary=True,
                        )
                    ],
                )
                for language in ("en", "zh")
            },
        ),
    )
    if event_group:
        item.metadata["event_group"] = event_group
    if event_relation:
        item.metadata["event_relation"] = event_relation
    return item


def _render(items, language="zh"):
    summarizer = DailySummarizer()
    return _run_async(summarizer.generate_summary(
        items, date="2026-11-14", total_fetched=10, language=language))


def test_p317_placeholder_titles_dropped_from_body_and_toc():
    items = [
        _make_item(1, "正常条目一"),
        _make_item(2, "人性台账 · 选题单"),
        _make_item(3, "正常条目三"),
    ]
    result = _render(items)

    assert "正常条目一" in result
    assert "正常条目三" in result
    assert "选题单" not in result
    assert "未进简报" in result
    assert "1 条占位标题条目未进简报" in result
    # 头行按拦后实数写：2 条，不是 3 条。
    assert "筛选出 2 条重要资讯" in result


def test_p317_placeholder_drop_renumbers_and_keeps_totals():
    items = [
        _make_item(1, "人性台账 · 选题单"),
        _make_item(2, "唯一正常条目"),
    ]
    summarizer = DailySummarizer()
    view = summarizer.build_view(items, language="zh")

    assert view.placeholder_dropped == 1
    assert len(view.groups) == 1
    assert len(view.groups[0].items) == 1
    assert view.groups[0].items[0].index == 1
    assert view.groups[0].items[0].group_count == 1


def test_p317_dual_eval_dimensions_and_evidence_rendered():
    item = _make_item(
        1, "双评条目",
        score=8.0, score_rounds=[7.0, 9.0], dual_eval="dual",
        dimension_scores={"证据": 2.0, "机制": 1.5, "决策": 2.0,
                          "张力": 1.0, "金句": 1.5},
        evidence_level=EvidenceLevel.UNCONFIRMED,
        cap_triggered="未证实7分", video_score=8.0,
    )
    result = _render([item])

    assert "双评：7 + 9（dual）" in result
    assert "五维：证据 2 / 机制 1.5 / 决策 2 / 张力 1 / 金句 1.5" in result
    assert "证据等级：unconfirmed" in result
    assert "封顶：未证实7分" in result


def test_p317_rumor_line_only_on_capped_items():
    capped = _make_item(1, "被封顶条目", score=7.0, video_score=8.0,
                        cap_triggered="未证实7分",
                        evidence_level=EvidenceLevel.UNCONFIRMED)
    verified = _make_item(2, "已证实条目", score=8.0, video_score=8.0,
                          evidence_level=EvidenceLevel.VERIFIED)
    result = _render([capped, verified])

    assert result.count("视频口径：传闻/待核实") == 1


def test_p317_event_group_line_rendered():
    item = _make_item(1, "跟进条目", score=6.0,
                      event_group="evt:rss:item-6",
                      event_relation="follow_up")
    result = _render([item])

    assert "事件组：evt:rss:item-6（follow_up）" in result


def test_p317_no_new_lines_without_structured_fields():
    item = _make_item(1, "普通条目", score=7.0)
    result = _render([item])

    assert "双评：" not in result
    assert "五维：" not in result
    assert "证据等级：" not in result
    assert "视频口径：" not in result
    assert "事件组：" not in result


def test_p319_opening_type_line_rendered():
    """P319-1: opening_type renders as its own line for make_hotspots."""
    item = _make_item(1, "热点开头条目", score=7.0, opening_type="热点")
    result = _render([item])

    assert "开头类型：热点" in result


def test_p319_no_opening_type_line_without_field():
    item = _make_item(1, "普通条目", score=7.0)
    result = _render([item])

    assert "开头类型：" not in result
