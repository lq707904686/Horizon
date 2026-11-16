"""Tests for P318/P319 in tools/make_hotspots.py and
tools/score_distribution.py.

  - P318-3: category_5 whitelist (template placeholder value rejected).
  - P318-4: google_news source parsed (was always unknown).
  - P318-5: no parseable score -> exit 1, no legacy full-extract fallback.
  - P318-6: funnel counts (fetched/analyzed/ge_5/ge_7) in the record.
  - P318-8: column-zero alerts go to $GITHUB_STEP_SUMMARY and the record.
  - P318-16: a scored item without a hotspots block counts in the
    distribution (11-15: brief max 7.0, submitted max 6.0) but does not
    move the drought fallback line.
  - P319-1: opening_type extracted into sets and the distribution.
"""
import datetime
import importlib.util
import json
import os
import sys
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"
REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_tool_module(name):
    spec = importlib.util.spec_from_file_location(name, TOOLS_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


make_hotspots = _load_tool_module("make_hotspots")
score_distribution = _load_tool_module("score_distribution")


def _brief_md(*, heading, body_lines, topics=("话题A",)):
    lines = [
        "# Horizon Daily - 2026-11-14",
        "",
        "> 从 536 条内容中筛选出 2 条重要资讯。",
        "",
        heading,
        "",
    ]
    lines.extend(body_lines)
    lines.append("")
    if topics is not None:
        lines.append("```hotspots")
        for t in topics:
            lines.append(f"- {t}")
        lines.append("```")
    lines.append("")
    return "\n".join(lines)


def _run_main(monkeypatch, tmp_path, argv):
    monkeypatch.setattr(sys, "argv", ["make_hotspots.py"] + argv)
    return make_hotspots.main()


def _write_brief(tmp_path, name, text):
    summaries = tmp_path / "summaries"
    summaries.mkdir(exist_ok=True)
    md = summaries / name
    md.write_text(text, encoding="utf-8")
    return summaries, md


# --- P318-3 ---

def test_category_whitelist_rejects_template_placeholder(tmp_path):
    md = tmp_path / "brief.md"
    md.write_text(_brief_md(
        heading="### [条目](https://example.com/a) ⭐️ 7.0/10",
        body_lines=["归属：5 真分类之一", "",
                    "rss · 虎嗅 · 11月14日 08:43"]),
        encoding="utf-8")
    scored = make_hotspots.extract_scored_topics(str(md))
    assert scored[0][2] is None


def test_category_whitelist_rejects_verbatim_template_text(tmp_path):
    md = tmp_path / "brief.md"
    md.write_text(_brief_md(
        heading="### [条目](https://example.com/a) ⭐️ 7.0/10",
        body_lines=["归属：<5真分类之一，看职场/带队伍/做生意/做销售/去创业>",
                    "", "rss · 虎嗅 · 11月14日 08:43"]),
        encoding="utf-8")
    scored = make_hotspots.extract_scored_topics(str(md))
    assert scored[0][2] is None


def test_category_whitelist_keeps_valid_value(tmp_path):
    md = tmp_path / "brief.md"
    md.write_text(_brief_md(
        heading="### [条目](https://example.com/a) ⭐️ 7.0/10",
        body_lines=["归属：做生意", "",
                    "rss · 虎嗅 · 11月14日 08:43"]),
        encoding="utf-8")
    scored = make_hotspots.extract_scored_topics(str(md))
    assert scored[0][2] == "做生意"


def test_enrichment_template_has_no_bare_placeholder():
    text = (REPO_ROOT / "profiles" / "renxing" / "enrichment.md").read_text(
        encoding="utf-8")
    assert "归属：5真分类之一；" not in text
    assert "归属：<5真分类之一，看职场/带队伍/做生意/做销售/去创业>" in text


# --- P318-4 ---

def test_source_google_news_parsed(tmp_path):
    md = tmp_path / "brief.md"
    md.write_text(_brief_md(
        heading="### [条目](https://example.com/a) ⭐️ 7.0/10",
        body_lines=["归属：看职场", "",
                    "google_news · 某出版社 · 11月16日 08:00"]),
        encoding="utf-8")
    scored = make_hotspots.extract_scored_topics(str(md))
    assert scored[0][3] == "某出版社"


def test_source_plain_google_still_parsed(tmp_path):
    md = tmp_path / "brief.md"
    md.write_text(_brief_md(
        heading="### [条目](https://example.com/a) ⭐️ 7.0/10",
        body_lines=["归属：看职场", "",
                    "google · 某报 · 11月16日 08:00"]),
        encoding="utf-8")
    scored = make_hotspots.extract_scored_topics(str(md))
    assert scored[0][3] == "某报"


# --- P318-5 ---

def test_no_scored_sections_fails_without_legacy_fallback(monkeypatch, tmp_path):
    summaries = tmp_path / "summaries"
    summaries.mkdir()
    (summaries / "horizon-2026-11-14.md").write_text(
        "# Horizon Daily\n\n"
        "### [无分条目](https://example.com/a)\n\n"
        "归属：看职场\n\n"
        "```hotspots\n- 话题A\n- 话题B\n```\n",
        encoding="utf-8")
    out = tmp_path / "hotspots.json"
    code = _run_main(monkeypatch, tmp_path,
                     ["--summaries", str(summaries), "--out", str(out)])
    assert code == 1
    assert not out.exists()


def test_legacy_extract_still_works_for_manual_recovery(tmp_path):
    md = tmp_path / "brief.md"
    md.write_text(_brief_md(
        heading="### [无分条目](https://example.com/a)",
        body_lines=["归属：看职场"],
        topics=("话题A", "话题B")),
        encoding="utf-8")
    assert make_hotspots.legacy_extract(str(md)) == ["话题A", "话题B"]


# --- P318-16 ---

def _brief_1115_style():
    """Mimic the published 11-15 brief: 5 scored items, the 7.0 one
    (German cars) has no hotspots block."""
    sections = [
        ("### [德国本土的汽车工业正在经历一次大收缩](https://example.com/de) ⭐️ 7.0/10 🎬 8.0/12",
         ["归属：看职场", "", "rss · 虎嗅 · 11月15日 08:43"], None),
        ("### [英伟达员工喊话黄仁勋回归](https://example.com/nv) ⭐️ 6.0/10 🎬 5.0/12",
         ["归属：看职场", "", "rss · 爱范儿 · 11月15日 08:43"],
         ("英伟达员工请愿黄仁勋回归", "华为赛力斯重申分工", "Microsoft 365 故障 48 小时")),
        ("### [年入 200 亿的二手巨头要 IPO 了](https://example.com/ipo) ⭐️ 6.0/10",
         ["归属：做生意", "", "rss · 36氪24小时热榜 · 11月15日 08:43"],
         ("二手巨头 IPO",)),
        ("### [马斯克出手了](https://example.com/musk) ⭐️ 6.0/10",
         ["归属：看职场", "", "rss · 36氪24小时热榜 · 11月15日 08:43"],
         ("马斯克逼黄仁勋",)),
        ("### [双十一退货少赠品被扣钱](https://example.com/1111) ⭐️ 5.0/10",
         ["归属：看职场", "", "rss · 百度热搜 · 11月15日 08:43"],
         ("双十一退货扣钱",)),
    ]
    lines = ["# Horizon Daily - 2026-11-15", "",
             "> 从 479 条内容中筛选出 5 条重要资讯。", ""]
    for heading, body_lines, topics in sections:
        lines.append(heading)
        lines.append("")
        lines.extend(body_lines)
        lines.append("")
        if topics is not None:
            lines.append("```hotspots")
            for t in topics:
                lines.append(f"- {t}")
            lines.append("```")
        lines.append("")
    return "\n".join(lines)


def test_scored_item_without_block_counts_in_distribution(tmp_path):
    md = tmp_path / "horizon-2026-11-15.md"
    md.write_text(_brief_1115_style(), encoding="utf-8")
    scored = make_hotspots.extract_scored_topics(str(md))
    assert len(scored) == 5
    record = score_distribution.build_distribution(
        scored, md_path=str(md))
    assert record["total_items"] == 5
    assert record["max_score"] == 7.0
    assert record["count_ge_7"] == 1
    assert record["count_ge_5"] == 5


def test_scored_item_without_block_does_not_move_fallback_line(tmp_path):
    md = tmp_path / "horizon-2026-11-15.md"
    md.write_text(_brief_1115_style(), encoding="utf-8")
    scored = make_hotspots.extract_scored_topics(str(md))
    topics, _sets, stats = make_hotspots.filter_topics(scored)
    # Same output as the committed 11-15 hotspots.json: the 6.0 fallback
    # still takes the first 6.0 section, the block-less 7.0 item does not
    # lift the line to 7.0 and empty the whole day.
    assert stats["fallback_score"] == 6.0
    assert topics == ["英伟达员工请愿黄仁勋回归", "华为赛力斯重申分工",
                     "Microsoft 365 故障 48 小时"]


# --- P319-1 ---

def test_opening_type_extracted_into_sets_and_distribution(tmp_path):
    md = tmp_path / "brief.md"
    md.write_text(_brief_md(
        heading="### [条目](https://example.com/a) ⭐️ 7.0/10",
        body_lines=["开头类型：热点", "归属：看职场", "",
                    "rss · 虎嗅 · 11月14日 08:43"]),
        encoding="utf-8")
    scored = make_hotspots.extract_scored_topics(str(md))
    _topics, sets, _stats = make_hotspots.filter_topics(scored)
    assert sets[0]["opening_type"] == "热点"
    record = score_distribution.build_distribution(scored)
    assert record["opening_types"] == {"热点": 1}


def test_opening_type_missing_stays_null(tmp_path):
    md = tmp_path / "brief.md"
    md.write_text(_brief_md(
        heading="### [条目](https://example.com/a) ⭐️ 7.0/10",
        body_lines=["归属：看职场", "",
                    "rss · 虎嗅 · 11月14日 08:43"]),
        encoding="utf-8")
    scored = make_hotspots.extract_scored_topics(str(md))
    _topics, sets, _stats = make_hotspots.filter_topics(scored)
    assert sets[0]["opening_type"] is None
    record = score_distribution.build_distribution(scored)
    assert record["opening_types"] == {}


# --- P318-6 ---

def _per_column(selected):
    return {
        "看职场": {"items": 1, "ge_7": 1, "selected": selected.get("看职场", 0)},
        "带队伍": {"items": 0, "ge_7": 0, "selected": selected.get("带队伍", 0)},
        "做生意": {"items": 0, "ge_7": 0, "selected": selected.get("做生意", 0)},
        "做销售": {"items": 0, "ge_7": 0, "selected": selected.get("做销售", 0)},
        "去创业": {"items": 0, "ge_7": 0, "selected": selected.get("去创业", 0)},
        "其他": {"items": 0, "ge_7": 0, "selected": 0},
    }


def test_funnel_recorded_from_fetch_report(monkeypatch, tmp_path):
    today = datetime.date.today().isoformat()
    summaries, _md = _write_brief(
        tmp_path, f"horizon-{today}.md",
        _brief_md(
            heading="### [条目](https://example.com/a) ⭐️ 7.5/10",
            body_lines=["归属：看职场", "",
                        "rss · 虎嗅 · 11月14日 08:43"]))
    (tmp_path / "fetch_report.json").write_text(json.dumps(
        {"date": today, "status": "success", "item_count": 461,
         "analyzed": 461, "sources": []}), encoding="utf-8")
    out = tmp_path / "hotspots.json"
    code = _run_main(monkeypatch, tmp_path,
                     ["--summaries", str(summaries), "--out", str(out)])
    assert code == 0
    record = json.loads(
        (tmp_path / "score_distribution.json").read_text(encoding="utf-8")
    )["records"][0]
    assert record["funnel"] == {"fetched": 536, "analyzed": 461,
                                "ge_5": 1, "ge_7": 1}


def test_funnel_analyzed_is_null_without_fetch_report(monkeypatch, tmp_path):
    today = datetime.date.today().isoformat()
    summaries, _md = _write_brief(
        tmp_path, f"horizon-{today}.md",
        _brief_md(
            heading="### [条目](https://example.com/a) ⭐️ 7.5/10",
            body_lines=["归属：看职场", "",
                        "rss · 虎嗅 · 11月14日 08:43"]))
    out = tmp_path / "hotspots.json"
    code = _run_main(monkeypatch, tmp_path,
                     ["--summaries", str(summaries), "--out", str(out)])
    assert code == 0
    record = json.loads(
        (tmp_path / "score_distribution.json").read_text(encoding="utf-8")
    )["records"][0]
    assert record["funnel"]["analyzed"] is None
    assert record["funnel"]["fetched"] == 536


# --- P318-8 ---

def test_column_zero_alerts_written_to_summary_and_record(
        monkeypatch, tmp_path):
    today = datetime.date.today()
    history = {"records": []}
    for i, selected in [(2, {}), (1, {})]:
        day = (today - datetime.timedelta(days=i)).isoformat()
        history["records"].append({
            "date": day, "total_items": 1, "max_score": 7.0,
            "count_ge_7": 1, "count_ge_5": 1, "bands": {},
            "per_source": {"虎嗅": 1},
            "per_column": _per_column(selected),
        })
    (tmp_path / "score_distribution.json").write_text(
        json.dumps(history), encoding="utf-8")
    summaries, _md = _write_brief(
        tmp_path, f"horizon-{today.isoformat()}.md",
        _brief_md(
            heading="### [条目](https://example.com/a) ⭐️ 7.0/10",
            body_lines=["归属：看职场", "",
                        "rss · 虎嗅 · 11月14日 08:43"]))
    summary_path = tmp_path / "step_summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_path))
    out = tmp_path / "hotspots.json"
    code = _run_main(monkeypatch, tmp_path,
                     ["--summaries", str(summaries), "--out", str(out)])
    assert code == 0
    record = json.loads(
        (tmp_path / "score_distribution.json").read_text(encoding="utf-8")
    )["records"][-1]
    assert "做生意" in record["column_zero_alerts"]
    summary = summary_path.read_text(encoding="utf-8")
    assert "做生意" in summary
    assert "栏目告警" in summary


def test_no_alerts_no_summary_write(monkeypatch, tmp_path):
    today = datetime.date.today().isoformat()
    # Three topics from three sources: no source tops the 40% quota line,
    # and with no history there is no 3-day zero-column run either.
    sections = []
    for source, topic in (("虎嗅", "话题A"), ("爱范儿", "话题B"),
                          ("36氪24小时热榜", "话题C")):
        sections.append(_brief_md(
            heading="### [条目](https://example.com/a) ⭐️ 7.0/10",
            body_lines=["归属：看职场", "",
                        f"rss · {source} · 11月14日 08:43"],
            topics=(topic,)))
    summaries, _md = _write_brief(
        tmp_path, f"horizon-{today}.md", "\n".join(sections))
    summary_path = tmp_path / "step_summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_path))
    out = tmp_path / "hotspots.json"
    code = _run_main(monkeypatch, tmp_path,
                     ["--summaries", str(summaries), "--out", str(out)])
    assert code == 0
    assert not summary_path.exists()
