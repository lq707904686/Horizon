"""Tests for the feedback-loop tools: thresholds and --date guardrails.

Covers 打磨方案 §五/§八 P0-10:
  - video cold-start threshold 20 records / 14 days -> 10 records / 30 days
    (load_feedback.py: VIDEO_MIN_RECORDS / VIDEO_WINDOW_DAYS)
  - record_feedback.py --date only accepts today or the past 7 days
    (补记日 = 发布日；--date must not be used to brush the window)
"""
import importlib.util
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"


def _load_tool_module(name):
    spec = importlib.util.spec_from_file_location(name, TOOLS_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


load_feedback = _load_tool_module("load_feedback")
record_feedback = _load_tool_module("record_feedback")


# --- load_feedback: video threshold 10 records / 30 days ---

def test_video_threshold_constants():
    assert load_feedback.VIDEO_MIN_RECORDS == 10
    assert load_feedback.VIDEO_WINDOW_DAYS == 30


def _video_record(days_ago, rec_id, score=1):
    d = (datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%d")
    return {
        "date": d,
        "kind": "video",
        "rec_id": rec_id,
        "title": rec_id,
        "category_5": ["做生意"],
        "motifs": [],
        "formulas": [],
        "result_score": score,
    }


def _run_load_feedback(monkeypatch, tmp_path, records, window=14):
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
    return prompt_path.read_text(encoding="utf-8")


def test_video_path_starts_with_10_records_in_30_days(monkeypatch, tmp_path):
    # 10 video records 20-29 days ago: outside the 14-day article window,
    # inside the fixed 30-day video window -> video section must be injected.
    records = [_video_record(days_ago, f"REC-{i:02d}")
               for i, days_ago in enumerate(range(20, 30))]
    text = _run_load_feedback(monkeypatch, tmp_path, records)
    assert "视频反馈加权" in text
    assert '"video_record_count": 10' in text
    assert '"window_days": 30' in text


def test_video_path_still_cold_below_10_records(monkeypatch, tmp_path):
    records = [_video_record(days_ago, f"REC-{i:02d}")
               for i, days_ago in enumerate(range(20, 29))]
    text = _run_load_feedback(monkeypatch, tmp_path, records)
    assert "视频反馈加权" not in text


def test_video_path_counts_only_30_day_window(monkeypatch, tmp_path):
    # 9 records inside 30 days + 1 record 31 days old -> only 9 count -> cold.
    records = [_video_record(days_ago, f"REC-{i:02d}")
               for i, days_ago in enumerate(range(20, 29))]
    records.append(_video_record(31, "REC-OLD"))
    text = _run_load_feedback(monkeypatch, tmp_path, records)
    assert "视频反馈加权" not in text


# --- record_feedback: --date guardrail ---

TODAY = date(2026, 11, 12)


def test_validate_record_date():
    validate = record_feedback.validate_record_date
    assert validate("2026-11-12", today=TODAY) is None
    assert validate("2026-11-05", today=TODAY) is None  # 7 days ago: allowed
    assert validate("2026-11-04", today=TODAY) is not None  # 8 days ago
    assert "补记窗口" in validate("2026-11-04", today=TODAY)
    assert validate("2026-11-13", today=TODAY) is not None  # future
    assert validate("2026-13-01", today=TODAY) is not None  # bad format
    assert validate("not-a-date", today=TODAY) is not None


def _run_record_feedback(monkeypatch, tmp_path, argv):
    log_path = tmp_path / "feedback_log.jsonl"
    monkeypatch.setattr(sys, "argv", ["record_feedback.py", "--log", str(log_path)] + argv)
    return record_feedback.main(), log_path


def test_record_feedback_rejects_old_date_via_cli(monkeypatch, tmp_path):
    old = (datetime.now() - timedelta(days=8)).strftime("%Y-%m-%d")
    code, log_path = _run_record_feedback(
        monkeypatch,
        tmp_path,
        ["--date", old, "--title", "旧选题", "--status", "已选"],
    )
    assert code == 1
    assert not log_path.exists()  # rejected before any write


def test_record_feedback_rejects_future_date_via_cli(monkeypatch, tmp_path):
    future = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    code, log_path = _run_record_feedback(
        monkeypatch,
        tmp_path,
        ["--date", future, "--title", "未来选题", "--status", "已选"],
    )
    assert code == 1
    assert not log_path.exists()


def test_record_feedback_accepts_recent_date_via_cli(monkeypatch, tmp_path):
    today = datetime.now().strftime("%Y-%m-%d")
    code, log_path = _run_record_feedback(
        monkeypatch,
        tmp_path,
        ["--date", today, "--title", "今日选题", "--status", "已选"],
    )
    assert code == 0
    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["date"] == today
