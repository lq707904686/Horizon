"""Tests for P315 (Top10 R4/R5/R8) in tools/make_hotspots.py.

  - R8: drought-day fallback keeps only the first section of the top band
    (max MAX_PER_ITEM topics); the rest are counted separately, never as
    below-line drops, and normal days are untouched.
  - R4: per-source quota check on kept topics (>=40% warns, never deletes).
  - R5: per-column split stats + consecutive zero-selected-day alerts.

Follows the loader pattern of tests/test_feedback_tools.py (tools are
loaded from file so no package install is needed).
"""
import datetime
import importlib.util
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"


def _load_tool_module(name):
    spec = importlib.util.spec_from_file_location(name, TOOLS_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


make_hotspots = _load_tool_module("make_hotspots")


def _item(score, category, source, topics):
    return (score, None, category, source, None, None, None, None, topics)


# --- R8: drought-day fallback converges to the top band's first section ---

def test_drought_fallback_keeps_only_first_top_section():
    scored = [
        _item(6.5, "做生意", "百度热搜", ["A1", "A2", "A3"]),
        _item(6.5, "带队伍", "36氪24小时热榜", ["B1", "B2"]),
        _item(6.0, "看职场", "百度热搜", ["C1"]),
    ]
    topics, sets, stats = make_hotspots.filter_topics(scored)
    assert topics == ["A1", "A2", "A3"]
    assert len(topics) <= make_hotspots.MAX_PER_ITEM
    assert stats["fallback_score"] == 6.5
    assert stats["kept_sections"] == 1
    assert stats["fallback_capped_sections"] == 1
    assert stats["dropped_sections"] == 1  # only the 6.0 section, not the capped one


def test_drought_single_section_capped_by_max_per_item():
    scored = [_item(6.5, "做生意", "百度热搜", ["A1", "A2", "A3", "A4", "A5"])]
    topics, _sets, stats = make_hotspots.filter_topics(scored)
    assert topics == ["A1", "A2", "A3"]
    assert stats["fallback_capped_sections"] == 0


def test_drought_placeholder_first_section_falls_through():
    scored = [
        _item(6.5, "做生意", "百度热搜", ["选题单·待填"]),
        _item(6.5, "带队伍", "虎嗅", ["B1", "B2"]),
    ]
    topics, _sets, stats = make_hotspots.filter_topics(scored)
    assert topics == ["B1", "B2"]
    assert stats["kept_sections"] == 1
    assert stats["fallback_capped_sections"] == 0
    assert stats["placeholder_dropped"] == 1


def test_normal_day_not_capped():
    scored = [
        _item(7.5, "做生意", "百度热搜", ["A1"]),
        _item(7.5, "带队伍", "虎嗅", ["B1"]),
        _item(6.0, "看职场", "百度热搜", ["C1"]),
    ]
    topics, _sets, stats = make_hotspots.filter_topics(scored)
    assert topics == ["A1", "B1"]
    assert stats["fallback_score"] is None
    assert stats["fallback_capped_sections"] == 0


# --- R4: source quota (warn only, never delete) ---

def test_source_quota_warn_over_threshold():
    assert make_hotspots.SOURCE_QUOTA_WARN_PCT == 0.40
    shares = make_hotspots.source_shares(["百度热搜"] * 5 + ["虎嗅"] * 3)
    assert shares[0] == ("百度热搜", 5, 0.625)
    assert make_hotspots.quota_warnings(shares) == ["百度热搜"]


def test_source_quota_no_warn_when_balanced():
    shares = make_hotspots.source_shares(
        ["百度热搜"] * 3 + ["虎嗅"] * 3 + ["36氪24小时热榜"] * 2)
    assert make_hotspots.quota_warnings(shares) == []


def test_source_quota_empty_and_unknown():
    assert make_hotspots.source_shares([]) == []
    assert make_hotspots.quota_warnings([]) == []
    assert make_hotspots.source_shares([None, ""]) == [("unknown", 2, 1.0)]


# --- R5: per-column split stats ---

def test_column_stats_counts():
    scored = [
        _item(7.5, "做生意", "百度热搜", ["A1"]),
        _item(7.5, "带队伍", "虎嗅", ["B1"]),
        _item(6.0, "看职场", "百度热搜", ["C1"]),
        _item(None, "做销售", "虎嗅", ["D1"]),
        _item(8.0, None, "虎嗅", ["E1"]),
    ]
    _topics, sets, _stats = make_hotspots.filter_topics(scored)
    stats = make_hotspots.column_stats(scored, sets)
    assert stats["做生意"] == {"items": 1, "ge_7": 1, "selected": 1}
    assert stats["带队伍"] == {"items": 1, "ge_7": 1, "selected": 1}
    assert stats["看职场"] == {"items": 1, "ge_7": 0, "selected": 0}
    assert stats["做销售"] == {"items": 0, "ge_7": 0, "selected": 0}
    assert stats["其他"] == {"items": 1, "ge_7": 1, "selected": 1}


# --- R5: consecutive zero-selected-day alerts ---

def _record(date, selected_by_col):
    per_column = {
        c: {"items": 1, "ge_7": 0, "selected": selected_by_col.get(c, 0)}
        for c in make_hotspots.COLUMNS_5
    }
    return {"date": date, "per_column": per_column}


def test_column_zero_alerts_three_consecutive_days():
    assert make_hotspots.COLUMN_ZERO_ALERT_DAYS == 3
    records = [_record("2026-11-12", {}), _record("2026-11-13", {}),
               _record("2026-11-14", {})]
    alerts = make_hotspots.column_zero_alerts(
        records, today=datetime.date(2026, 11, 14))
    assert alerts == list(make_hotspots.COLUMNS_5)


def test_column_zero_alerts_two_days_no_alert():
    records = [_record("2026-11-13", {}), _record("2026-11-14", {})]
    assert make_hotspots.column_zero_alerts(
        records, today=datetime.date(2026, 11, 14)) == []


def test_column_zero_alerts_gap_breaks_run():
    records = [_record("2026-11-12", {}), _record("2026-11-14", {})]
    assert make_hotspots.column_zero_alerts(
        records, today=datetime.date(2026, 11, 14)) == []


def test_column_zero_alerts_old_record_without_per_column_breaks_run():
    records = [{"date": "2026-11-12"},
               _record("2026-11-13", {}), _record("2026-11-14", {})]
    assert make_hotspots.column_zero_alerts(
        records, today=datetime.date(2026, 11, 14)) == []


def test_column_zero_alerts_selected_day_breaks_only_that_column():
    records = [_record("2026-11-12", {}),
               _record("2026-11-13", {"带队伍": 2}),
               _record("2026-11-14", {})]
    alerts = make_hotspots.column_zero_alerts(
        records, today=datetime.date(2026, 11, 14))
    assert "带队伍" not in alerts
    assert alerts == ["看职场", "做生意", "做销售", "去创业"]
