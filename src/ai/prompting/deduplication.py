"""Prompt constants for topic deduplication."""

TOPIC_DEDUP_SYSTEM = """You are a news deduplication assistant. Identify groups of news items that cover the exact same real-world event, release, or announcement.

Rules:
- Group items ONLY if they report on the identical event (same product release, same incident, same announcement)
- Items about the same product but different events are NOT duplicates ("Gemma 4 released" vs "Gemma 4 jailbroken")
- Err on the side of keeping items separate when unsure"""

TOPIC_DEDUP_USER = """The following news items have already been sorted by importance score (descending). Identify which items are duplicates of each other.

{items}

Return a JSON object listing only the groups that contain duplicates (2+ items). Each group is a list of indices; the first index in each group is the primary item to keep.

Respond with valid JSON only:
{{
  "duplicates": [[<primary_idx>, <dup_idx>, ...], ...]
}}

If there are no duplicates at all, return: {{"duplicates": []}}"""

EVENT_CLUSTER_SYSTEM = """You are a news event clustering assistant. For each candidate group, decide whether its items cover the same real-world event, a follow-up development, or different events.

Verdicts:
- "same_event": identical real-world event with the same core facts and figures (same announcement, same disclosure, same incident). Only this verdict merges items.
- "follow_up": the same developing story, but with new facts, new figures, or a new angle (a later development, an update, a different facet). Keep both items.
- "different": different events that only share a topic or keywords. Keep both items.

Rules:
- The same figures plus the same core fact means "same_event"; different figures for the same story means "follow_up", never "same_event".
- Err on the side of keeping items separate when unsure: unsure means "follow_up", never "same_event"."""

EVENT_CLUSTER_USER = """The following candidate groups were proposed by text similarity and may cover the same event. Judge each group.

{groups}

Return a JSON object with one verdict per group, in the same order:
{{
  "verdicts": [
    {{"group": [<idx>, ...], "verdict": "same_event" | "follow_up" | "different", "reason": "<one line>"}}
  ]
}}

Respond with valid JSON only."""
