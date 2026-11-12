"""Text-similarity event candidate proposal (no AI calls).

This module only *proposes* candidates: items whose titles and summaries
overlap enough that they might cover the same real-world event. Whether a
candidate pair is the same event, a follow-up, or two different events is
decided by the model in ``Orchestrator.cluster_events`` — text alone never
merges anything, because two different reports of one topic (or the same
topic with different figures) can look textually similar.

Calibration (2026-11-12, real 11-12 brief items 9/10, "离岸信托补税 1600 亿"):
combined similarity 0.247; unrelated items in the same brief score 0.0.
The candidate threshold 0.10 is deliberately recall-oriented: it proposes
the real pair with wide margin while the model disposes of false friends.
"""

import re
from typing import List

# Candidate threshold: recall-oriented; the model verdict does the deciding.
CANDIDATE_THRESHOLD = 0.10
# Title counts more than body: two reports of one event usually share the
# headline's core phrase even when their summaries are written differently.
TITLE_WEIGHT = 0.6

_STRIP = re.compile(r"[\s，。、；：！？\"\"''（）《》\[\]·—…%]")


def _char_ngrams(text: str, n: int) -> set:
    cleaned = _STRIP.sub("", text)
    if len(cleaned) < n:
        return {cleaned} if cleaned else set()
    return {cleaned[i : i + n] for i in range(len(cleaned) - n + 1)}


def _jaccard(left: set, right: set) -> float:
    if not left and not right:
        return 0.0
    union = left | right
    return len(left & right) / len(union) if union else 0.0


def text_similarity(title_a: str, body_a: str, title_b: str, body_b: str) -> float:
    """Combined title/body similarity in [0, 1].

    0.6 * title bigram Jaccard + 0.4 * full-text trigram Jaccard.
    Character n-grams need no word segmenter, which is what makes this
    usable for Chinese headlines and summaries as-is.
    """
    title_sim = _jaccard(_char_ngrams(title_a, 2), _char_ngrams(title_b, 2))
    body_sim = _jaccard(
        _char_ngrams(title_a + body_a, 3), _char_ngrams(title_b + body_b, 3)
    )
    return TITLE_WEIGHT * title_sim + (1 - TITLE_WEIGHT) * body_sim


def _item_text(item) -> tuple:
    """(title, body) for an item: title plus analysis summary and tags."""
    analysis = item.processing.analysis if item.processing else None
    summary = analysis.summary if analysis and analysis.summary else ""
    tags = " ".join(analysis.tags) if analysis and analysis.tags else ""
    body = f"{summary} {tags}".strip()
    if item.content and len(item.content) > len(body):
        # Prefer the fuller text when the item carries its own content;
        # cap it so one long comment thread cannot dominate the score.
        body = item.content[:1500]
    return item.title, body


def propose_event_candidates(items: List, *, threshold: float = CANDIDATE_THRESHOLD) -> List[List[int]]:
    """Group indices of items that may cover the same event.

    Pairwise combined similarity >= threshold, transitively grouped with
    union-find. Returns only groups of 2+ items, each sorted ascending.
    """
    n = len(items)
    if n <= 1:
        return []
    texts = [_item_text(item) for item in items]
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(n):
        for j in range(i + 1, n):
            sim = text_similarity(texts[i][0], texts[i][1], texts[j][0], texts[j][1])
            if sim >= threshold:
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[ri] = rj
    groups: dict = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    return sorted(
        (sorted(g) for g in groups.values() if len(g) >= 2),
        key=lambda g: g[0],
    )
