"""Tests for P317 in tools/load_feedback.py.

  - Stale-log alert when the newest row is more than 2 days old.
  - Video cold start at 9/10 prints the one-more-record checklist.
  - Article coverage counts article rows only (a video-only date must
    not inflate it); the message states how many days are still missing.
"""
import importlib.util
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"


def _load_tool_module(name):
    spec = importlib.util.spec_from_file_location(name, TOOLS_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


load_feedback = _load_tool_module("load_feedback")


def _record(days_ago, *, kind="article", rec_id=None, score=1):
    d = (datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%d")
    rec = {
        "date": d,
        "kind": kind,
        "title": rec_id or "选题",
        "category_5": ["做生意"],
        "motifs": [],
        "formulas": [],
        "result_score": score,
    }
    if kind == "video":
        rec["rec_id"] = rec_id
    return rec


def _run(monkeypatch, tmp_path, records, capsys, window=14):
    log_path = tmp_path / "feedback_log.jsonl"
    log_path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records),
        encoding="utf-8",
    )
    prompt_path = tmp_path / "analysis.md"
    prompt_path.write_text("# 评分标准\n", encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "load_feedback.py",
            "--window",
            str(window),
            "--log",
            str(log_path),
            "--prompt",
            str(prompt_path),
        ],
    )
    assert load_feedback.main() == 0
    return capsys.readouterr().out


def test_p317_staleness_alert_after_gap(monkeypatch, tmp_path, capsys):
    # Newest row 3 days ago: gap > 2 days -> alert names the missing dates.
    records = [_record(3), _record(4)]
    out = _run(monkeypatch, tmp_path, records, capsys)
    assert "反馈缺失告警" in out
    assert "距今天已 3 天" in out


def test_p317_no_staleness_alert_when_logged_yesterday(monkeypatch, tmp_path, capsys):
    records = [_record(1), _record(2)]
    out = _run(monkeypatch, tmp_path, records, capsys)
    assert "反馈缺失告警" not in out


def test_p317_video_one_more_checklist_at_9_of_10(monkeypatch, tmp_path, capsys):
    records = [_record(days_ago, kind="video", rec_id=f"REC-{i:02d}")
               for i, days_ago in enumerate(range(1, 10))]
    out = _run(monkeypatch, tmp_path, records, capsys)
    assert "只差 1 条即通" in out
    assert "REC-00" in out
    assert "record_feedback.py --kind video" in out


def test_p317_video_no_checklist_at_8_of_10(monkeypatch, tmp_path, capsys):
    records = [_record(days_ago, kind="video", rec_id=f"REC-{i:02d}")
               for i, days_ago in enumerate(range(1, 9))]
    out = _run(monkeypatch, tmp_path, records, capsys)
    assert "只差 1 条即通" not in out


def test_p317_article_coverage_ignores_video_only_dates(monkeypatch, tmp_path, capsys):
    # 4 article dates + 1 video-only date: coverage is 4, not 5.
    records = [_record(days_ago) for days_ago in (1, 2, 3, 4)]
    records.append(_record(5, kind="video", rec_id="REC-99"))
    out = _run(monkeypatch, tmp_path, records, capsys)
    assert "仅 4 天" in out
    assert "还差 17 天" in out
