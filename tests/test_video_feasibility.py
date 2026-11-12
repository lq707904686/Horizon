"""Unit tests for the video_score feasibility tier.

Covers the full chain (全盘打磨方案_20261113 §七 联动断点 2):
  assess_video_feasibility (pure) -> ContentAnalysis.video_feasibility
  (analyzer) -> digest heading (summarizer) -> hotspots.json sets[]
  (make_hotspots). The R4 lesson applies throughout: a field that is
  computed but never lands in the output does not exist.
"""

import asyncio
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from src.ai.analyzer import ContentAnalyzer
from src.ai.summarizer import DailySummarizer
from src.ai.video_feasibility import assess_video_feasibility, extract_article_numbers
from src.models import (
    ClassificationResult,
    ContentAnalysis,
    ContentItem,
    ProcessingResult,
    SourceType,
)
from src.processing import ProfileRegistry

PROFILES = ProfileRegistry.load(
    Path(__file__).resolve().parents[1] / "profiles", "tech-news"
)

# --- 040 轮真实数字（打磨方案 §七）---------------------------------------

# 2401 正文数字极富：290.92万、65.92万、120万、16万、9万。
TEXT_2401 = (
    "一笔 290.92 万的转账先到账，接着又一笔 65.92 万，对方账户里还躺着 "
    "120 万，中间人抽走 16 万，只剩 9 万没追回来。"
)
# 2403 正文数字：2万 / 4000 / 20万 / 17万。
TEXT_2403 = (
    "每月多交 2 万，补缴基数按 4000 算，追回 20 万，剩下的 17 万还在走流程。"
)
# REC-29 式零数字反例：旁白 10 行零数据数字 —— 文章本身也没有可上画面的数。
TEXT_ZERO_NUMBERS = (
    "他站在门口，看着远方，手里攥着一张旧车票。风很大，没有人说话，"
    "也没有人知道接下来会发生什么。"
)


def test_2401_number_rich_article_is_feasible():
    result = assess_video_feasibility(TEXT_2401)
    assert result["level"] == "可视化可行"
    assert result["number_count"] == 5
    assert result["numbers"] == ["290.92万", "65.92万", "120万", "16万", "9万"]
    assert result["template_count"] >= 2
    assert "数字对比" in result["placeable_templates"]
    assert "账本对账" in result["placeable_templates"]


def test_2403_four_numbers_are_feasible():
    result = assess_video_feasibility(TEXT_2403)
    assert result["level"] == "可视化可行"
    assert result["number_count"] == 4
    assert result["numbers"] == ["2万", "4000", "20万", "17万"]
    assert result["template_count"] >= 2


def test_rec29_style_zero_number_text_is_infeasible():
    result = assess_video_feasibility(TEXT_ZERO_NUMBERS)
    assert result["level"] == "不可行"
    assert result["number_count"] == 0
    assert result["template_count"] == 0
    assert result["placeable_templates"] == []


def test_one_or_two_numbers_are_partial():
    result = assess_video_feasibility("月薪 5000 元，房租 2000 元，剩下的不多。")
    assert result["level"] == "部分可行"
    assert result["number_count"] == 2


def test_many_numbers_but_one_template_is_partial():
    # 3 个不同数字，但只有数字对比一个槽可落位（无金额、无百分比、无日期锚点、
    # 无大数字）—— 模板数 1 也判部分可行。
    result = assess_video_feasibility(
        "直播间互动来了 1500 次，接着 2500 次，最后冲到 3500 次。"
    )
    assert result["number_count"] == 3
    assert result["template_count"] == 1
    assert result["placeable_templates"] == ["数字对比"]
    assert result["level"] == "部分可行"


def test_years_are_not_data_numbers():
    # 只有年份锚点、没有数据数字：时间线槽存在也改变不了"不可行"，
    # 数字 0 的判定优先于模板数。
    result = assess_video_feasibility("2019年亮相，2023年交付，中间隔了四年。")
    assert result["number_count"] == 0
    assert result["level"] == "不可行"


def test_repeated_number_counts_once():
    numbers = extract_article_numbers("追回 20 万，又追回 20 万，总共还是 20 万。")
    assert numbers == ["20万"]


def test_percent_number_fills_ratio_slot():
    result = assess_video_feasibility("毛利率 80%，成本 20%，剩下的是人工。")
    assert result["numbers"] == ["80%", "20%"]
    assert "比例占比" in result["placeable_templates"]


# --- analyzer：字段落到 ContentAnalysis，video_score 本身不动 -------------


def _fixed_client(payload):
    async def complete(**kwargs):
        return json.dumps(payload)

    return SimpleNamespace(complete=complete, config=SimpleNamespace())


def test_analyzer_sets_feasibility_and_keeps_video_score():
    client = _fixed_client(
        {
            "score": 7.5,
            "reason": "r",
            "summary": "s",
            "tags": [],
            "evidence_level": "verified",
            "video_score": 11.5,
            "video_reason": "钩子强",
        }
    )
    item = ContentItem(
        id="rss:test:feas",
        source_type=SourceType.RSS,
        title="鲸鱼转账",
        url="https://example.com/item",
        content=TEXT_2401,
        published_at=datetime(2026, 4, 26, tzinfo=timezone.utc),
        profile="tech-news",
        processing=ProcessingResult(
            classification=ClassificationResult(
                profile="tech-news", method="source_override"
            )
        ),
    )
    asyncio.run(ContentAnalyzer(client, PROFILES)._analyze_item(item))
    analysis = item.processing.analysis
    assert analysis.video_score == 11.5  # 分数本身一字未动
    assert analysis.video_feasibility["level"] == "可视化可行"
    assert analysis.video_feasibility["number_count"] == 5


# --- summarizer：档位进标题行，紧跟 🎬 -------------------------------------


def _summarizer_item(video_score, feasibility):
    return ContentItem(
        id="rss:test:heading",
        source_type=SourceType.RSS,
        title="鲸鱼转账",
        url="https://example.com/item",
        content="content",
        author="tester",
        published_at=datetime(2026, 4, 25, 8, 0, tzinfo=timezone.utc),
        profile="tech-news",
        processing=ProcessingResult(
            classification=ClassificationResult(
                profile="tech-news", method="source_override"
            ),
            analysis=ContentAnalysis(
                score=7.5,
                reason="test",
                summary="Summary.",
                tags=[],
                video_score=video_score,
                video_feasibility=feasibility,
            ),
        ),
    )


def test_summarizer_heading_carries_feasibility_next_to_video_score():
    summarizer = DailySummarizer()
    text = asyncio.run(
        summarizer.generate_summary(
            [_summarizer_item(11.5, assess_video_feasibility(TEXT_2401))],
            date="2026-11-13",
            total_fetched=1,
            language="zh",
        )
    )
    assert "\U0001f3ac\ufe0f 11.5/12 · 可视化可行（5数/3模板）" in text


def test_summarizer_heading_omits_feasibility_when_unassessed():
    summarizer = DailySummarizer()
    text = asyncio.run(
        summarizer.generate_summary(
            [_summarizer_item(8.0, None)],
            date="2026-11-13",
            total_fetched=1,
            language="zh",
        )
    )
    assert "\U0001f3ac\ufe0f 8.0/12" in text
    assert "可视化可行" not in text
    assert "部分可行" not in text
    assert "不可行" not in text


# --- make_hotspots：标题行 -> sets[]，字段不丢 -----------------------------

def _load_make_hotspots():
    path = Path(__file__).resolve().parents[1] / "tools" / "make_hotspots.py"
    spec = importlib.util.spec_from_file_location("make_hotspots", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _digest_md(heading_suffix: str) -> str:
    return (
        "# Horizon Daily - 2026-11-13\n\n"
        "> 从 514 条内容中筛选出 1 条重要资讯。\n\n"
        f"### [鲸鱼转账](https://example.com/item) ⭐️ 7.5/10 🎬 11.5/12{heading_suffix}\n\n"
        "归属：做生意\n\n"
        "rss · 36氪24小时热榜 · 11月13日 08:43\n\n"
        "```hotspots\n- 鲸鱼转账 290 万\n```\n"
    )


def test_make_hotspots_carries_feasibility_into_sets(tmp_path):
    mh = _load_make_hotspots()
    md = tmp_path / "horizon-2026-11-13.md"
    md.write_text(_digest_md(" · 可视化可行（5数/3模板）"), encoding="utf-8")
    scored = mh.extract_scored_topics(str(md))
    topics, sets, stats = mh.filter_topics(scored)
    assert len(sets) == 1
    assert sets[0]["video_score"] == 11.5
    assert sets[0]["score"] == 7.5
    assert sets[0]["video_feasibility"] == "可视化可行"
    assert sets[0]["video_feasibility_reason"] == "文章数字 5 个，可落位模板 3 种"


def test_make_hotspots_feasibility_none_for_old_briefs(tmp_path):
    mh = _load_make_hotspots()
    md = tmp_path / "horizon-2026-11-13.md"
    md.write_text(_digest_md(""), encoding="utf-8")
    scored = mh.extract_scored_topics(str(md))
    _, sets, _ = mh.filter_topics(scored)
    assert sets[0]["video_feasibility"] is None
    assert sets[0]["video_feasibility_reason"] is None
    assert sets[0]["video_score"] == 11.5  # 旧字段不受影响


def test_make_hotspots_parses_partial_and_infeasible_levels(tmp_path):
    mh = _load_make_hotspots()
    assert mh._extract_video_feasibility("### 标题 ⭐️ 7.0/10 🎬 8/12 · 部分可行（2数/2模板）") == (
        "部分可行",
        "文章数字 2 个，可落位模板 2 种",
    )
    assert mh._extract_video_feasibility("### 标题 ⭐️ 7.0/10 🎬 5/12 · 不可行（0数/0模板）") == (
        "不可行",
        "文章数字 0 个，可落位模板 0 种",
    )
    assert mh._extract_video_feasibility("### 标题 ⭐️ 7.0/10") == (None, None)
