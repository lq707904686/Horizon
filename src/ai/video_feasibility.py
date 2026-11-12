"""Deterministic "script visualization feasibility" check for video_score.

Background (全盘打磨方案_20261113 §七 联动断点 2): video_score ranked the
040 round backwards — 2401 scored 11.5 and 2403 scored 10.5, the two
format-v1 FAIL episodes, while the lowest-scored 2402 (7.5) produced the
v5 benchmark (REC-30). The numbers died while writing the script, not at
topic selection: 2401's article is number-rich (290.92万, 65.92万, 120万,
16万, 9万) yet its narration has zero data numbers; 2403's article has
2万/4000/20万/17万 and the narration used only two of them. video_score
looks at the article alone, never at whether the script can say the numbers
out loud.

This module adds one deterministic, model-independent tier next to
video_score. It is computed in code from the article's number table, never
by the model, so it cannot be talked up by a generous video_reason.

Rules (task spec, nothing else):
- numbers >= 3 AND placeable templates >= 2 -> "可视化可行"
- numbers 1-2 OR placeable templates 1 -> "部分可行"
- numbers == 0 -> "不可行"

Limitations, stated plainly (there is no number-extraction or template
matching function anywhere in this repo — video_score's 可视化 subscore is
model-judged, and the format-v1 render templates live in the video workflow
repo, not Horizon — so this is the simplest counting implementation):
- Only Arabic numerals are extracted; Chinese numerals (三万, 五千) are
  not counted.
- A "data number" is a numeral with a value unit (万/亿/元/%/倍/…) or a
  bare numeral >= 1000 (e.g. 4000). Years (2019年) and month/day fragments
  (11月8日) are NOT data numbers: they are date anchors, not filmable
  figures. A bare small numeral with no unit (e.g. "3 个") is not counted
  either — it carries no magnitude a frame can show.
- "Placeable templates" is a proxy, not the real template engine: it counts
  how many of the five number-taking format-v1 slots the article's numbers
  can fill (数字对比 / 账本对账 / 比例占比 / 时间线 / 单数字冲击). It
  does NOT check whether the narration actually uses the numbers — that
  check belongs to the script stage (新集剧本逐行对文章数字表), this tier
  only says whether the article gives the script anything to work with.
- Feasibility is judged on the same selected text the model scored
  (title + analysis_max_chars slice), not the full article.
"""

import re
from typing import Dict, List, Optional, Tuple

# Level literals. The summarizer renders the level into the digest heading
# and tools/make_hotspots.py parses it back; keep the three strings in sync
# across src/ai/video_feasibility.py, src/ai/summarizer.py and
# tools/make_hotspots.py (all three list them explicitly).
LEVEL_FEASIBLE = "可视化可行"
LEVEL_PARTIAL = "部分可行"
LEVEL_INFEASIBLE = "不可行"
LEVELS = (LEVEL_FEASIBLE, LEVEL_PARTIAL, LEVEL_INFEASIBLE)

# Value units that make a numeral a filmable data number. 年/月/日 are
# deliberately absent: years and dates are timeline anchors, handled by
# the 时间线 slot, not data numbers.
_VALUE_UNITS = (
    "万", "亿", "千", "百",
    "元", "美元", "块", "毛", "分", "角",
    "%", "％", "倍",
    "人", "次", "辆", "台", "单", "户", "名", "家", "只", "张", "部",
    "件", "个", "款", "项", "笔",
    "天", "小时", "分钟", "秒",
    "斤", "公斤", "千克", "克", "吨",
    "米", "公里", "千米", "厘米", "平米", "平方米", "㎡", "亩",
)
_UNIT_ALT = "|".join(re.escape(u) for u in sorted(_VALUE_UNITS, key=len, reverse=True))

# Numeral with an optional value unit, e.g. 290.92万 / 4000 / 17%. Commas in
# the integer part are allowed (1,200万). A trailing unit is optional so
# bare magnitudes (4000) match; the >= 1000 gate is applied afterwards.
_NUMBER_RX = re.compile(
    r"(?<!\d)(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?\s*(" + _UNIT_ALT + r")?"
)
# Year anchor, e.g. 2019年 / 2023年. Used only for the 时间线 slot and to
# keep years out of the data-number count.
_YEAR_RX = re.compile(r"(?:19|20)\d{2}\s*年")
_DATE_RX = re.compile(r"\d{1,2}\s*月\s*\d{1,2}\s*日")

_BARE_MIN = 1000  # bare numeral counts as a data number only at/above this


def _normalize(raw_int: str, raw_frac: Optional[str], unit: Optional[str]) -> Optional[str]:
    """Normalize one regex hit to a dedup key, or None if it is not data."""
    if unit in ("年", "月", "日"):
        return None
    digits = raw_int.replace(",", "")
    if unit is None:
        # Bare numeral: only a magnitude (4000, not 3) is a filmable number.
        # Years never reach here: 年 is not in _VALUE_UNITS, so 2019年
        # matches as bare 2019 — drop it explicitly.
        if len(digits) == 4 and digits.startswith(("19", "20")):
            return None
        try:
            if float(digits) < _BARE_MIN:
                return None
        except ValueError:
            return None
    value = digits + (f".{raw_frac}" if raw_frac else "")
    return f"{value}{unit or ''}"


def extract_article_numbers(text: str) -> List[str]:
    """Extract the article's number table: distinct data numbers, in order.

    Distinct by normalized value+unit, so a number repeated in the article
    is one entry — the script cannot film the same figure twice as two
    different frames.
    """
    if not text:
        return []
    seen = set()
    numbers = []
    for m in _NUMBER_RX.finditer(text):
        key = _normalize(m.group(1), m.group(2), m.group(3))
        if key is None or key in seen:
            continue
        seen.add(key)
        numbers.append(key)
    return numbers


def _has_money_unit(numbers: List[str]) -> int:
    """How many numbers carry a money unit (元/万/亿)."""
    return sum(1 for n in numbers if n.endswith(("元", "万", "亿")))


def _has_percent(numbers: List[str]) -> bool:
    return any(n.endswith(("%", "％")) for n in numbers)


def _has_big_number(numbers: List[str]) -> bool:
    """A number worth a full-screen splash: 万/亿 magnitude, or >= 10000."""
    for n in numbers:
        if n.endswith(("万", "亿")):
            return True
        try:
            if float(re.sub(r"[^\d.]", "", n)) >= 10000:
                return True
        except ValueError:
            continue
    return False


def count_placeable_templates(numbers: List[str], text: str) -> Tuple[int, List[str]]:
    """Count the format-v1 number slots this article's numbers can fill.

    Slots (proxy for the video workflow's render templates, judged from
    text alone):
    - 数字对比: >= 2 distinct numbers (a contrast frame needs a pair).
    - 账本对账: >= 2 money-unit numbers (a ledger frame needs two amounts).
    - 比例占比: >= 1 percent number.
    - 时间线: a year/date anchor in the text (2019年 / 11月8日).
    - 单数字冲击: >= 1 big number (万/亿 magnitude or >= 10000).
    """
    slots = []
    if len(numbers) >= 2:
        slots.append("数字对比")
    if _has_money_unit(numbers) >= 2:
        slots.append("账本对账")
    if _has_percent(numbers):
        slots.append("比例占比")
    if _YEAR_RX.search(text or "") or _DATE_RX.search(text or ""):
        slots.append("时间线")
    if _has_big_number(numbers):
        slots.append("单数字冲击")
    return len(slots), slots


def assess_video_feasibility(text: str) -> Dict[str, object]:
    """Assess one article's script-visualization feasibility.

    Returns a plain dict (stored as ContentAnalysis.video_feasibility, the
    same shape pattern as video_subscores): level / numbers / number_count /
    placeable_templates / template_count / reason. The level is the field
    the summarizer renders and make_hotspots carries into hotspots.json
    sets[]; the counts and reason travel with it so the tier is auditable
    instead of a bare label.
    """
    text = text or ""
    numbers = extract_article_numbers(text)
    template_count, templates = count_placeable_templates(numbers, text)
    number_count = len(numbers)
    if number_count == 0:
        level = LEVEL_INFEASIBLE
    elif number_count >= 3 and template_count >= 2:
        level = LEVEL_FEASIBLE
    else:
        level = LEVEL_PARTIAL
    reason = (
        f"文章数字 {number_count} 个"
        + (f"（{', '.join(numbers)}）" if numbers else "")
        + f"，可落位模板 {template_count} 种"
        + (f"（{'、'.join(templates)}）" if templates else "")
    )
    return {
        "level": level,
        "numbers": numbers,
        "number_count": number_count,
        "placeable_templates": templates,
        "template_count": template_count,
        "reason": reason,
    }
