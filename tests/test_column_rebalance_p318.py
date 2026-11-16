"""Tests for P319-3 in src/orchestrator.py (column quota soft enforcement).

  - One column over 60% of the digest swaps its lowest-video_score excess
    item for a same-band item from a zero-selected column (highest
    video_score first).
  - No same-band backfill -> no swap, the digest does not shrink.
  - A follow-up of an already-selected event is never used as backfill
    (P318-9 cooperation: no padding with the same event).
  - Under 5 selected items the rule does not trigger.
"""
from datetime import datetime, timezone
from types import SimpleNamespace

from rich.console import Console

from src.models import (
    ClassificationResult,
    ContentAnalysis,
    ContentItem,
    DigestConfig,
    ProcessingConfig,
    ProcessingResult,
    ProfileSettingsConfig,
    SourceType,
)
from src.orchestrator import HorizonOrchestrator


def _make_item(item_id, score, video_score, category_5, *,
               event_group=None, event_relation=None):
    metadata = {}
    if event_group is not None:
        metadata["event_group"] = event_group
    if event_relation is not None:
        metadata["event_relation"] = event_relation
    return ContentItem(
        id=item_id,
        source_type=SourceType.RSS,
        title=item_id,
        url=f"https://example.com/{item_id}",
        published_at=datetime.now(timezone.utc),
        metadata=metadata,
        profile="tech-news",
        processing=ProcessingResult(
            classification=ClassificationResult(
                profile="tech-news", method="source_override"
            ),
            analysis=ContentAnalysis(
                score=score, reason="test", summary=item_id,
                video_score=video_score, category_5=[category_5],
            ),
        ),
    )


def _make_orchestrator(digest):
    orchestrator = HorizonOrchestrator.__new__(HorizonOrchestrator)
    orchestrator.config = SimpleNamespace(
        digest=digest,
        processing=ProcessingConfig(
            profile_settings={
                "tech-news": ProfileSettingsConfig(threshold=7.0),
            }
        ),
    )
    orchestrator.console = Console(record=True)
    return orchestrator


def _dominant_setup(*, backfill_band=7):
    """10 selected by score (7x 看职场 70%) + 2 unselected candidates.

    Selected cutoff is 7.2; candidates score 7.1 / 7.0 (band 7) or, when
    backfill_band=6, 6.9 / 6.8 (band 6). The lowest-video 看职场 item
    (video 2, score 7.8, band 7) is the excess item.
    """
    items = [
        _make_item("zc-1", 9.0, 8, "看职场"),
        _make_item("zc-2", 8.8, 7, "看职场"),
        _make_item("zc-3", 8.6, 6, "看职场"),
        _make_item("zc-4", 8.4, 5, "看职场"),
        _make_item("zc-5", 8.2, 4, "看职场"),
        _make_item("zc-6", 8.0, 3, "看职场"),
        _make_item("zc-7", 7.8, 2, "看职场"),
        _make_item("sy-1", 7.6, 9, "做生意"),
        _make_item("sx-1", 7.4, 9, "做销售"),
        _make_item("cy-1", 7.2, 9, "去创业"),
    ]
    if backfill_band == 7:
        candidates = [
            _make_item("dd-1", 7.1, 10, "带队伍"),
            _make_item("dd-2", 7.0, 3, "带队伍"),
        ]
    else:
        candidates = [
            _make_item("dd-1", 6.9, 10, "带队伍"),
            _make_item("dd-2", 6.8, 3, "带队伍"),
        ]
    return items + candidates


def test_dominant_column_swaps_to_zero_column_same_band():
    orch = _make_orchestrator(DigestConfig(max_items=10))
    result = orch.apply_balanced_digest(_dominant_setup())
    ids = [item.id for item in result.items]
    assert result.rebalanced_count == 1
    assert "dd-1" in ids  # highest-video same-band zero-column candidate
    assert "zc-7" not in ids  # lowest-video excess item swapped out
    assert "dd-2" not in ids  # only one excess slot
    assert len(ids) == 10


def test_no_same_band_backfill_keeps_excess():
    orch = _make_orchestrator(DigestConfig(max_items=10))
    result = orch.apply_balanced_digest(_dominant_setup(backfill_band=6))
    ids = [item.id for item in result.items]
    assert result.rebalanced_count == 0
    assert "zc-7" in ids
    assert "dd-1" not in ids
    assert len(ids) == 10


def test_follow_up_candidate_not_used_as_backfill():
    items = _dominant_setup()
    # The best backfill candidate is a follow-up of an already-selected
    # event: it must not pad the digest.
    items[10].metadata["event_group"] = "evt:zc-1"
    items[10].metadata["event_relation"] = "follow_up"
    items[0].metadata["event_group"] = "evt:zc-1"
    orch = _make_orchestrator(DigestConfig(max_items=10))
    result = orch.apply_balanced_digest(items)
    ids = [item.id for item in result.items]
    # dd-2 (same band, zero column, not a follow-up) is used instead.
    assert result.rebalanced_count == 1
    assert "dd-2" in ids
    assert "dd-1" not in ids


def test_small_digest_no_rebalance():
    items = [
        _make_item("zc-1", 9.0, 8, "看职场"),
        _make_item("zc-2", 8.0, 7, "看职场"),
        _make_item("zc-3", 7.0, 6, "看职场"),
        _make_item("dd-1", 6.5, 10, "带队伍"),
    ]
    orch = _make_orchestrator(DigestConfig(max_items=10))
    result = orch.apply_balanced_digest(items)
    assert result.rebalanced_count == 0
    assert [item.id for item in result.items] == [
        "zc-1", "zc-2", "zc-3", "dd-1"]


def test_balanced_columns_no_rebalance():
    items = [
        _make_item(f"{col}-{i}", 9.0 - i * 0.1, 5, col)
        for i, col in enumerate(
            ["看职场", "带队伍", "做生意", "做销售", "去创业"] * 2)
    ]
    orch = _make_orchestrator(DigestConfig(max_items=10))
    result = orch.apply_balanced_digest(items)
    assert result.rebalanced_count == 0
    assert len(result.items) == 10
