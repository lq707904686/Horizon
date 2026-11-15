"""Tests for P317 in tools/make_hotspots.py and tools/score_distribution.py.

  - score_rounds / dual_eval / dimension_scores / evidence_level /
    event_group are extracted from the brief body into sets[]; capped
    items get an explicit rumor_stance field (not only video_reason text).
  - Empty hotspots is a hard failure: main() returns 1 and writes nothing
    (no empty file, no distribution overwrite, no commit downstream).
  - Latest brief is picked by write time (mtime), not filename sort.
  - Distribution history keeps the best record of the day (late reruns
    must not overwrite a good brief with zeros).
  - per_source (items) and kept_source_shares (topics) carry unit labels.
"""
import importlib.util
import json
import os
import sys
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"


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
    lines.append("```hotspots")
    for t in topics:
        lines.append(f"- {t}")
    lines.append("```")
    lines.append("")
    return "\n".join(lines)


def _capped_heading():
    return ("### [别为洋马甲买单](https://example.com/a) ⭐️ 7.5/10 "
            "🎬 8.0/12 · 部分可行（1数/2模板）")


def _capped_body_lines():
    return [
        "封顶：未证实7分",
        "双评：7 + 9（dual）",
        "五维：证据 2 / 机制 1.5 / 决策 2 / 张力 1 / 金句 1.5",
        "证据等级：unconfirmed",
        "视频口径：传闻/待核实（只拍冲突本身，不坐实未证实数字）",
        "事件组：evt:rss:item-1（follow_up）",
        "归属：看职场",
        "",
        "rss · 虎嗅 · 11月14日 08:43",
    ]


def test_p317_fields_extracted_into_sets(tmp_path):
    md = tmp_path / "horizon-2026-11-14.md"
    md.write_text(_brief_md(heading=_capped_heading(),
                            body_lines=_capped_body_lines()),
                  encoding="utf-8")
    scored = make_hotspots.extract_scored_topics(str(md))
    _topics, sets, _stats = make_hotspots.filter_topics(scored)

    assert len(sets) == 1
    s = sets[0]
    assert s["score"] == 7.5
    assert s["video_score"] == 8.0
    assert s["score_rounds"] == [7.0, 9.0]
    assert s["dual_eval"] == "dual"
    assert s["dimension_scores"] == {
        "证据": 2.0, "机制": 1.5, "决策": 2.0, "张力": 1.0, "金句": 1.5}
    assert s["evidence_level"] == "unconfirmed"
    assert s["event_group"] == "evt:rss:item-1"


def test_p317_rumor_stance_only_on_capped_items(tmp_path):
    md = tmp_path / "horizon-2026-11-14.md"
    capped = _brief_md(heading=_capped_heading(),
                        body_lines=_capped_body_lines())
    verified = _brief_md(
        heading="### [ verified item ](https://example.com/b) ⭐️ 7.0/10 🎬 8.0/12",
        body_lines=["双评：7 + 7（dual）", "证据等级：verified",
                    "归属：做生意", "",
                    "rss · 虎嗅 · 11月14日 08:43"],
        topics=("话题B",))
    md.write_text(capped + "\n" + verified, encoding="utf-8")
    scored = make_hotspots.extract_scored_topics(str(md))
    _topics, sets, _stats = make_hotspots.filter_topics(scored)

    by_topic = {s["topic"]: s for s in sets}
    assert by_topic["话题A"]["verified"] is False
    assert by_topic["话题A"]["cap_triggered"] == "未证实7分"
    assert by_topic["话题A"]["rumor_stance"] == "传闻/待核实"
    assert by_topic["话题B"]["verified"] is True
    assert by_topic["话题B"]["rumor_stance"] is None


def test_p317_old_briefs_get_null_new_fields(tmp_path):
    md = tmp_path / "horizon-2026-11-13.md"
    md.write_text(_brief_md(
        heading="### [旧条目](https://example.com/c) ⭐️ 7.0/10 🎬 8.0/12",
        body_lines=["归属：看职场", "",
                    "rss · 虎嗅 · 11月13日 08:43"]),
        encoding="utf-8")
    scored = make_hotspots.extract_scored_topics(str(md))
    _topics, sets, _stats = make_hotspots.filter_topics(scored)

    s = sets[0]
    assert s["score_rounds"] is None
    assert s["dual_eval"] is None
    assert s["dimension_scores"] is None
    assert s["evidence_level"] is None
    assert s["event_group"] is None
    assert s["rumor_stance"] is None


def test_p317_single_eval_round_kept(tmp_path):
    md = tmp_path / "horizon-2026-11-14.md"
    md.write_text(_brief_md(
        heading="### [单轮条目](https://example.com/d) ⭐️ 7.0/10",
        body_lines=["双评：7（single_eval）", "归属：看职场", "",
                    "rss · 虎嗅 · 11月14日 08:43"],
        topics=("话题D",)),
        encoding="utf-8")
    scored = make_hotspots.extract_scored_topics(str(md))
    _topics, sets, _stats = make_hotspots.filter_topics(scored)
    assert sets[0]["score_rounds"] == [7.0]
    assert sets[0]["dual_eval"] == "single_eval"


# --- empty hard gate ---

def _run_main(monkeypatch, tmp_path, argv):
    monkeypatch.setattr(sys, "argv", ["make_hotspots.py"] + argv)
    return make_hotspots.main()


def test_p317_empty_summaries_dir_fails_without_writing(monkeypatch, tmp_path):
    summaries = tmp_path / "summaries"
    summaries.mkdir()
    out = tmp_path / "hotspots.json"
    code = _run_main(monkeypatch, tmp_path,
                     ["--summaries", str(summaries), "--out", str(out)])
    assert code == 1
    assert not out.exists()


def test_p317_all_placeholder_topics_fail_without_writing(monkeypatch, tmp_path):
    summaries = tmp_path / "summaries"
    summaries.mkdir()
    md = summaries / "horizon-2026-11-14.md"
    md.write_text(_brief_md(heading=_capped_heading(),
                            body_lines=_capped_body_lines(),
                            topics=("选题单·待填",)),
                  encoding="utf-8")
    out = tmp_path / "hotspots.json"
    code = _run_main(monkeypatch, tmp_path,
                     ["--summaries", str(summaries), "--out", str(out)])
    assert code == 1
    assert not out.exists()


def test_p317_normal_run_writes_and_returns_zero(monkeypatch, tmp_path):
    summaries = tmp_path / "summaries"
    summaries.mkdir()
    (summaries / "horizon-2026-11-14.md").write_text(
        _brief_md(heading=_capped_heading(),
                  body_lines=_capped_body_lines()),
        encoding="utf-8")
    out = tmp_path / "hotspots.json"
    code = _run_main(monkeypatch, tmp_path,
                     ["--summaries", str(summaries), "--out", str(out)])
    assert code == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["topics"] == ["话题A"]
    assert data["sets"][0]["rumor_stance"] == "传闻/待核实"


# --- mtime ordering ---

def test_p317_latest_brief_picked_by_mtime_not_filename(monkeypatch, tmp_path):
    summaries = tmp_path / "summaries"
    summaries.mkdir()
    old = summaries / "horizon-2026-11-15.md"
    old.write_text(_brief_md(
        heading="### [旧文件新日期](https://example.com/e) ⭐️ 7.0/10",
        body_lines=["归属：看职场", "",
                    "rss · 虎嗅 · 11月15日 08:43"],
        topics=("旧话题",)),
        encoding="utf-8")
    new = summaries / "horizon-2026-11-14.md"
    new.write_text(_brief_md(
        heading="### [新文件旧日期](https://example.com/f) ⭐️ 7.0/10",
        body_lines=["归属：看职场", "",
                    "rss · 虎嗅 · 11月14日 08:43"],
        topics=("新话题",)),
        encoding="utf-8")
    # 文件名排序会取 11-15（旧话题）；写入时间排序取后写入的 11-14（新话题）。
    os.utime(old, (1_700_000_000, 1_700_000_000))
    os.utime(new, (1_700_000_100, 1_700_000_100))
    out = tmp_path / "hotspots.json"
    code = _run_main(monkeypatch, tmp_path,
                     ["--summaries", str(summaries), "--out", str(out)])
    assert code == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["topics"] == ["新话题"]


# --- distribution: keep best of day + unit labels ---

def _record(date, total_items, count_ge_7, max_score):
    return {"date": date, "total_items": total_items,
            "count_ge_7": count_ge_7, "max_score": max_score,
            "count_ge_5": total_items, "bands": {},
            "per_source": {}}


def test_p317_distribution_keeps_best_record_of_day(tmp_path):
    path = tmp_path / "score_distribution.json"
    good = _record("2026-11-13", 15, 5, 8.0)
    score_distribution.update_history(str(path), good)
    # 迟到重跑的 0 条记录不得覆盖好记录。
    score_distribution.update_history(str(path), _record("2026-11-13", 0, 0, None))
    history = json.loads(path.read_text(encoding="utf-8"))
    assert history["records"] == [good]


def test_p317_distribution_better_record_replaces(tmp_path):
    path = tmp_path / "score_distribution.json"
    score_distribution.update_history(str(path), _record("2026-11-13", 5, 1, 7.0))
    better = _record("2026-11-13", 15, 5, 8.0)
    score_distribution.update_history(str(path), better)
    history = json.loads(path.read_text(encoding="utf-8"))
    assert history["records"] == [better]


def test_p317_distribution_per_source_unit_label(monkeypatch, tmp_path):
    summaries = tmp_path / "summaries"
    summaries.mkdir()
    (summaries / "horizon-2026-11-14.md").write_text(
        _brief_md(heading=_capped_heading(),
                  body_lines=_capped_body_lines()),
        encoding="utf-8")
    out = tmp_path / "hotspots.json"
    code = _run_main(monkeypatch, tmp_path,
                     ["--summaries", str(summaries), "--out", str(out)])
    assert code == 0
    dist = json.loads((tmp_path / "score_distribution.json").read_text(
        encoding="utf-8"))
    record = dist["records"][0]
    assert record["per_source_unit"] == "条目数（简报内带分条目，按条目计）"
    assert record["per_source"] == {"虎嗅": 1}
    assert record["kept_source_shares"][0]["unit"] == "话题数（入选 hotspots 话题，按话题计）"
