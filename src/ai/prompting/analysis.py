"""Prompt construction for profile-driven content analysis."""

from ...models import ContentItem
from ...processing.profiles import LoadedProfile
from .common import EVIDENCE_RULES, UNTRUSTED_INPUT_RULE

ANALYSIS_RULES = f"""You are a content curator evaluating an item under the supplied processing profile.

- {UNTRUSTED_INPUT_RULE}
- Base the analysis only on the supplied item and its metadata.
{EVIDENCE_RULES}
- Apply the profile's evaluation policy consistently."""


def analysis_system_prompt(profile: LoadedProfile) -> str:
    return f"""{ANALYSIS_RULES}

# Profile policy

{profile.analysis_prompt}

# Output contract

Return valid JSON only:
{{
  "score": <number from 0 to 10>,
  "reason": "<concise explanation>",
  "summary": "<one-sentence summary>",
  "tags": ["<tag>", "..."],
  "evidence_level": "<title_only | unconfirmed | opinion_only | verified>",
  "dimension_scores": {{"证据": <0-2>, "机制": <0-2>, "决策": <0-2>, "张力": <0-2>, "金句": <0-2>}},
  "video_score": <number from 0 to 12>,
  "video_reason": "<one sentence in Chinese>",
  "video_subscores": {{"钩子张力": <0-4>, "身份代入感": <0-2>, "情绪冲突": <0-2>, "可视化": <0-1>, "完播友好度": <0-1>, "传播潜力": <0-2>, "时效分": <0 | -0.5 | -1>}},
  "cap_triggered": <null | "标题级6分" | "未证实7分" | "纯观点6分">,
  "source_kind": "<商业科技 | 民生热榜 | 其他>",
  "topic_category": "<民生 | 财经 | 其他>",
  "time_anchor": "<news anchor date, e.g. 2026-11-04, or '未核实'>",
  "category_5": ["<看职场 | 带队伍 | 做生意 | 做销售 | 去创业>", "..."],
  "motifs": ["<分钱 | 分权 | 面子 | 画饼 | 甩锅 | 止损 | 圈子 | 信任 | 谈判 | 贪怕 | 幸存者偏差 | 既得利益>", "..."],
  "formulas": ["<痛点直击 | 反直觉反转 | 数字冲击 | 悬念留白 | 身份代入 | 损失厌恶>", "..."],
  "opening_type": "<热点 | 反常识 | 真实案例 | 其他>"
}}

Rules for the extra fields (all are required unless the profile says otherwise;
omit a field only when it genuinely cannot be judged):
- category_5 / motifs / formulas are the feedback-loop labels (P318-13):
  fill them from the item itself so the D5 anti-homogenization check can
  read them straight from the analysis instead of hand transcription.
  category_5 is the item's primary column (first entry); motifs and
  formulas take up to 3 entries each, in fit order.
- opening_type (P319-1) is the item's opening kind: 热点 (opens on a hot
  event), 反常识 (opens on a counter-intuitive claim), 真实案例 (opens on
  a real case), or 其他. Judge it from the title + content opening only;
  when genuinely undecidable, use 其他, never leave it blank.

Rules for the extra fields (all are required unless the profile says otherwise;
omit a field only when it genuinely cannot be judged):
- evidence_level decides the hard cap, enforced in code, not by goodwill:
  title_only and opinion_only cap score at 6, unconfirmed caps it at 7.
  When a cap applies, set cap_triggered to its name and say so in reason.
- video_score is independent of score and is NEVER capped: a high video_score
  with a capped score is allowed, but then video_reason must say the item is
  handled as 传闻/待核实 (conflict only, never present unverified numbers
  as fact)."""


def analysis_user_prompt(
    item: ContentItem,
    content_section: str,
    discussion_section: str,
) -> str:
    return f"""Analyze the following content.

Title: {item.title}
Source: {item.source_type.value}
Author: {item.author or "Unknown"}
URL: {item.url}
{content_section}
{discussion_section}"""
