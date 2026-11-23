"""L7 热榜补全文（v3 落地，2026-11-23 用户「L1–L16 全按建议落地」）。

背景：P318-2（060b1e4）对「模型实际打分文本 < 300 字、无全文」的条目强制 title_only
封顶 6 分。热榜条目只有标题 + 1～2 行 desc，于是“讲人”的热榜事件被系统性压在 6 分以下，
11-17 起 ≥7.0 断供（百度入选 0 条）。同一事件换成有全文的来源，分数就能恢复
（11-23 果园：头条热榜标题 5.0 → 澎湃全文 8.25）。

本模块在打分之前做一步**纯本地**匹配：热榜条目（以及其他正文 <300 字的标题级条目）用标题关键词（中文二元组 + 数字/英文词）
去**当天已抓到**的全文条目池（中新网社会/法治、澎湃热闻、36氪、虎嗅等任何正文 ≥ 300 字的
非热榜条目）里找同一事件；匹配上就把对方全文接到该热榜条目正文后面（带来源标注），
让模型按全文打分。

不变的东西：
  - 封顶规则本身不动（analyzer.TITLE_ONLY_MAX_CONTENT_CHARS / CAP_LIMITS 原样）；
  - 不发任何网络请求（零外网请求，不存在请求风暴）；
  - 匹配不上的条目原样保留；任何异常由调用方捕获只打警告，不拦主流程。
匹配阈值事先固定（见 MATCH_*），用 11-23 当天数据离线回放校准，结果见
hidden_files/v3落地_20261123/雷达_落地.md。
"""

from __future__ import annotations

import html
import re
from typing import Iterable, List, Optional

VERSION = "fulltext-fill-v1"
MIN_FULLTEXT_CHARS = 300        # 与 analyzer.TITLE_ONLY_MAX_CONTENT_CHARS 同值：短于它才需要补
MIN_TITLE_GRAMS = 4             # 热榜标题有效关键词太少（<4 个二元组）不匹配，防误配
# 阈值按 11-23 当天 355 条热榜 × 45 条全文池人工标注校准（见雷达_落地.md）：
#   真同事件：055大驱 0.22/0.78、万吨大驱 0.43/0.50、黎曼猜想 0.38/0.48、冷空气 0.30/0.50、登机口扔硬币 0.23/0.54…
#   误配：GTA6↔「8点1氪」早报 0.10/0.80、高兴夫↔情报案 0.33/0.33、年轻人不接电话↔大学 0.11/0.44
MATCH_TITLE = 0.5               # 热榜标题关键词在对方标题里的覆盖率 ≥ 0.5 → 同事件
MATCH_BODY = 0.45               # 或：在对方标题+导语前 800 字里覆盖率 ≥ 0.45 且标题覆盖 ≥ 0.2
MATCH_BODY_TITLE_FLOOR = 0.2
LEAD_CHARS = 800
MAX_APPEND_CHARS = 6000         # 接入的全文上限（打分只取前 analysis_max_chars，够用）
FILL_MARKER = "--- 同事件全文（L7 补全文"

HOTLIST_CATEGORY = "热榜"

_TAG_RX = re.compile(r"<[^>]+>")
_WS_RX = re.compile(r"\s+")
_CJK_RX = re.compile(r"[\u4e00-\u9fff]+")
_TOKEN_RX = re.compile(r"[A-Za-z]{2,}|\d+(?:\.\d+)?")
# 标题里高频但不指认事件的二元组（只在热榜标题侧剔除）
_STOP_GRAMS = frozenset("""
回应 官方 网友 热议 曝光 最新 事件 视频 现场 怎么 为什么 什么 如何 这个 一个 我们 他们 今天
真的 中国 国家 进行 表示 通报 发布 情况 相关 问题 引发 关注 背后 原来 竟然 终于 突然 已经
""".split())


def clean_text(text: Optional[str]) -> str:
    t = html.unescape(text or "")
    t = _TAG_RX.sub(" ", t)
    return _WS_RX.sub(" ", t).strip()


def grams(text: str, *, drop_stop: bool = False) -> set:
    """中文连续段的二元组 + 英文词/数字 token。"""
    out = set()
    for seg in _CJK_RX.findall(text or ""):
        if len(seg) == 1:
            continue
        for i in range(len(seg) - 1):
            g = seg[i:i + 2]
            if drop_stop and g in _STOP_GRAMS:
                continue
            out.add(g)
    for tok in _TOKEN_RX.findall(text or ""):
        out.add(tok.lower())
    return out


def is_hotlist(item) -> bool:
    meta = getattr(item, "metadata", None) or {}
    return meta.get("category") == HOTLIST_CATEGORY


def main_text(item) -> str:
    """去掉评论段后的正文（与 analyzer 的 split_content 口径一致）。"""
    content = getattr(item, "content", None) or ""
    marker = "--- Top Comments ---"
    if marker in content:
        content = content.split(marker, 1)[0]
    return clean_text(content)


def needs_fill(item) -> bool:
    """正文 < 300 字（会被 P318-2 封顶）的条目都尝试补：主体是热榜，也覆盖 Google News 等
    只有标题的源（11-23 回放：「测测」在 Google News 只有标题 5.0，澎湃全文版 6.5）。"""
    return len(main_text(item)) < MIN_FULLTEXT_CHARS


def is_fulltext_source(item) -> bool:
    """全文池：正文 ≥ 300 字的条目（不论来源类别，但热榜条目本身只有在已有全文时才算）。"""
    return len(main_text(item)) >= MIN_FULLTEXT_CHARS


def match_score(hot_title: str, cand_title: str, cand_body: str):
    """返回 (title_cov, body_cov, n_hot_grams)。"""
    h = grams(clean_text(hot_title), drop_stop=True)
    if not h:
        return 0.0, 0.0, 0
    t = grams(clean_text(cand_title))
    b = t | grams(clean_text(cand_body)[:LEAD_CHARS])
    return len(h & t) / len(h), len(h & b) / len(h), len(h)


def is_match(title_cov: float, body_cov: float, n: int) -> bool:
    if n < MIN_TITLE_GRAMS:
        return False
    return title_cov >= MATCH_TITLE or (body_cov >= MATCH_BODY and title_cov >= MATCH_BODY_TITLE_FLOOR)


def _index(pool: Iterable):
    """预先算好候选的 (item, 标题 grams, 标题+导语 grams)，避免 N×M 次重复切词。"""
    out = []
    for cand in pool:
        t = grams(clean_text(getattr(cand, "title", "")))
        out.append((cand, t, t | grams(main_text(cand)[:LEAD_CHARS])))
    return out


def find_match(hot_title: str, pool: Iterable, exclude_id: Optional[str] = None, *, indexed=None):
    """在 pool（有 title/content 的对象）里找最佳同事件全文条目，返回 (item, title_cov, body_cov) 或 None。"""
    best = None
    best_key = None
    h = grams(clean_text(hot_title), drop_stop=True)
    if len(h) < MIN_TITLE_GRAMS:
        return None
    for cand, t, b in (indexed if indexed is not None else _index(pool)):
        if exclude_id is not None and getattr(cand, "id", None) == exclude_id:
            continue
        tc, bc, n = len(h & t) / len(h), len(h & b) / len(h), len(h)
        if not is_match(tc, bc, n):
            continue
        key = (tc + bc, tc)
        if best_key is None or key > best_key:
            best, best_key = (cand, round(tc, 3), round(bc, 3)), key
    return best


def _label(item) -> str:
    meta = getattr(item, "metadata", None) or {}
    return meta.get("feed_name") or getattr(getattr(item, "source_type", None), "value", None) or "unknown"


def fill_hotlist_fulltext(items: List) -> dict:
    """原地给需要补的热榜条目接上同事件全文。返回统计 dict（供日志/抓取报告）。

    只读当天已抓到的 items，不发网络请求。被补的条目 metadata 加
    fulltext_fill = {src, url, title, title_cov, body_cov, version}。
    """
    pool = [it for it in items if is_fulltext_source(it) and not needs_fill(it)]
    targets = [it for it in items if needs_fill(it)]
    indexed = _index(pool)
    filled = []
    for it in targets:
        m = find_match(it.title, pool, exclude_id=getattr(it, "id", None), indexed=indexed)
        if not m:
            continue
        cand, tc, bc = m
        full = main_text(cand)[:MAX_APPEND_CHARS]
        src = _label(cand)
        block = f"\n\n{FILL_MARKER}，来源 {src}：{cand.title}）---\n" + full
        content = it.content or ""
        marker = "--- Top Comments ---"
        if marker in content:  # 接在正文末、评论段之前，保证 analyzer.split_content 把它算进正文
            head, tail = content.split(marker, 1)
            it.content = head.rstrip() + block + "\n\n" + marker + tail
        else:
            it.content = content.rstrip() + block
        it.metadata["fulltext_fill"] = {
            "src": src, "url": str(getattr(cand, "url", "")), "title": cand.title,
            "title_cov": tc, "body_cov": bc, "version": VERSION,
        }
        filled.append({"hot": it.title, "hot_src": _label(it), "src": src, "title": cand.title,
                       "title_cov": tc, "body_cov": bc})
    return {"version": VERSION, "hotlist_short": sum(1 for t in targets if is_hotlist(t)),
            "short_total": len(targets), "filled_hotlist": sum(1 for t in targets if is_hotlist(t)
                                                                and "fulltext_fill" in t.metadata),
            "pool": len(pool),
            "filled": len(filled), "pairs": filled}
