"""Tests for P318-6 (FetchReport persistence) and P318-14 (dead secrets
removed from the workflow) in src/orchestrator.py.

  - _save_fetch_report writes per-source counts/durations to
    data/fetch_report.json; _update_fetch_report merges the analyzed
    count afterwards.
  - daily-renxing.yml no longer passes LIGHTVELA/RUNINFRA dead secrets
    and commits the new state files.
"""
import io
import json
from pathlib import Path
from types import SimpleNamespace

from rich.console import Console

from src.orchestrator import FetchReport, HorizonOrchestrator, SourceFetchOutcome


def _orchestrator(tmp_path):
    orch = object.__new__(HorizonOrchestrator)
    orch.console = Console(file=io.StringIO(), width=100)
    orch.storage = SimpleNamespace(data_dir=tmp_path)
    return orch


def test_save_fetch_report_writes_per_source_counts(tmp_path):
    orch = _orchestrator(tmp_path)
    orch.last_fetch_report = FetchReport(outcomes=[
        SourceFetchOutcome(source_name="RSS Feeds", status="success",
                           items=[object(), object()], duration_sec=1.23),
        SourceFetchOutcome(source_name="Google News", status="empty",
                           duration_sec=0.5),
        SourceFetchOutcome(source_name="Reddit", status="failure",
                           error="boom", duration_sec=2.0),
    ])
    orch._save_fetch_report()
    data = json.loads((tmp_path / "fetch_report.json").read_text(
        encoding="utf-8"))
    assert data["attempted"] == 3
    # "successful" is non-failed (an empty fetch is still a successful
    # fetch); the empty count is tracked separately.
    assert data["successful"] == 2
    assert data["empty"] == 1
    assert data["failed"] == 1
    assert data["item_count"] == 2
    by_source = {s["source"]: s for s in data["sources"]}
    assert by_source["RSS Feeds"]["item_count"] == 2
    assert by_source["RSS Feeds"]["duration_sec"] == 1.23
    assert by_source["Reddit"]["error"] == "boom"


def test_update_fetch_report_merges_analyzed(tmp_path):
    orch = _orchestrator(tmp_path)
    orch.last_fetch_report = FetchReport(outcomes=[])
    orch._save_fetch_report()
    orch._update_fetch_report(analyzed=461)
    data = json.loads((tmp_path / "fetch_report.json").read_text(
        encoding="utf-8"))
    assert data["analyzed"] == 461
    assert data["attempted"] == 0


def test_update_fetch_report_skips_when_no_file(tmp_path):
    orch = _orchestrator(tmp_path)
    orch._update_fetch_report(analyzed=461)  # must not raise, must not create
    assert not (tmp_path / "fetch_report.json").exists()


def test_workflow_has_no_dead_secrets_and_commits_state_files():
    text = (Path(__file__).resolve().parents[1] / ".github" / "workflows"
            / "daily-renxing.yml").read_text(encoding="utf-8")
    assert "LIGHTVELA_API_KEY" not in text
    assert "RUNINFRA_API_KEY" not in text
    assert "DEEPSEEK_API_KEY" in text
    assert "NVIDIA_BUILD_API_KEY" in text
    assert "data/fetch_report.json" in text
    assert "data/hotlists_seen.json" in text
    assert "data/event_history.json" in text
