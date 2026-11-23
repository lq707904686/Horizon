# -*- coding: utf-8 -*-
"""三仓统一人物词表加载器（v3 L3，2026-11-23 用户「L1–L16 全按建议落地」）。

唯一事实源：同目录 person_lexicon.json（与雷达 tools/、视频 scripts/ 的副本字节一致，
person-lexicon-v1，sha256 bde6b72c…（2026-11-23 补亲属称谓））。判定 = terms + patterns − exclude：
先把 exclude 里的泛指词（你/你们/顾客/网友/消费者/大家/人们/用户）从文本里剔掉，再查 terms 与 patterns。
「你」不算人；「陈先生」这类 姓+称谓 由 patterns 命中。雷达人物判定（radar_v4.topic_type、spread_score 分渠道键 spread_video/spread_tietu）
都只调这里。注意：spread_score（spread-v1）为保持回测与 11-24 验收可比，仍用 radar_v4.PERSON_RX 冻结口径。
"""
import os, re, json

HERE = os.path.dirname(os.path.abspath(__file__))
LEXICON_PATH = os.path.join(HERE, "person_lexicon.json")

with open(LEXICON_PATH, encoding="utf-8") as _f:
    LEXICON = json.load(_f)

VERSION = LEXICON.get("version", "")
TERMS = list(LEXICON.get("terms", []))
PATTERNS = list(LEXICON.get("patterns", []))
EXCLUDE = sorted(LEXICON.get("exclude", []), key=len, reverse=True)

# 长词优先，避免「副教授」被「教授」截断（只影响 group 文本，不影响是否命中）
PERSON_RX = re.compile("(" + "|".join([re.escape(t) for t in sorted(TERMS, key=len, reverse=True)] + PATTERNS) + ")")
_EXCLUDE_RX = re.compile("|".join(re.escape(x) for x in EXCLUDE)) if EXCLUDE else None


def _clean(text):
    t = text or ""
    return _EXCLUDE_RX.sub("\u3000", t) if _EXCLUDE_RX else t


def person_hit(text):
    """返回命中的人物指称（字符串）或 None。"""
    m = PERSON_RX.search(_clean(text))
    return m.group(0) if m else None


def has_person(text):
    return person_hit(text) is not None
