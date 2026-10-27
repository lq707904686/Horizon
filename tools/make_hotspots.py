#!/usr/bin/env python3
"""从 Horizon 每日简报中抽取 ```hotspots 代码块, 生成 hotspots.json.

hotspots.json 是「人性台账」生产流程 v3.4.6 阶段 3.5 (gen_viral_content)
的热点输入契约, 字段含义见《爆款内容方法论》第五节。
用法: python tools/make_hotspots.py --summaries data/summaries --out hotspots.json

v1.6 硬过滤 (2026-10-27):
  简报里每个条目以 `### 标题 ⭐️ X.0/10` 开头, 其下的 ```hotspots 代码块
  是该条目的候选选题。旧版不看分数全量抽取, 导致 5-6 分条目也进了
  hotspots.json (2026-10-27 实测: 30 个条目仅 1 个 7.0 分, 却抽出 90 条)。
  本版按条目分数硬过滤: 低于 MIN_SCORE 的条目整块丢弃, 每条目最多保留
  MAX_PER_ITEM 条, 全局去重, 按分数降序。prompt 不动, 只靠代码层把关。
  若当日没有任何条目达到 MIN_SCORE, 回退到当日最高分档 (避免空 feed),
  并在输出 JSON 里标注 fallback_score。
"""

import argparse
import datetime
import glob
import html
import json
import os
import re
import sys

# --- 硬过滤参数: 改这里即可调线, 不用动 prompt ---
MIN_SCORE = 7.0      # 低于此分的条目不进 hotspots.json
MAX_PER_ITEM = 3     # 每个条目最多保留的选题数 (取块内前 N 行)

HOTSPOTS_RX = re.compile(r"```hotspots\s*\n(.*?)```", re.S)
# Horizon 的 summarizer 落盘前会对 AI 输出做 markdown 转义：
#   _MARKDOWN_SPECIAL: \ ` * _ { } [ ] ( ) < > # ! | → 前加反斜杠
#   _MARKDOWN_BLOCK_START: 行首的 "- " → "\- "
# 所以简报里的 hotspots 代码块实际是 \`\`\`hotspots … \- … \`\`\`。
# 抽取前必须先还原，否则正则永远匹配不到（2026-10-16 实测：run #4
# 明明选中 1 条、简报里有 6 个话题，topics 依然是 []）。
_MD_ESCAPE_RX = re.compile(r"\\([\\`*_{}\[\]()<>#!|\-])")

# 条目标题行里的分数: `### [标题](url) ⭐️ 7.0/10`
# ⭐ 是 U+2B50, 后常跟 U+FE0F 变体选择符。
_SCORE_EMOJI_RX = re.compile(r"⭐\uFE0F?\s*(\d+(?:\.\d+)?)\s*/\s*10")
_SCORE_EOL_RX = re.compile(r"(\d+(?:\.\d+)?)\s*/\s*10\s*$")
_HEADING_RX = re.compile(r"^#{2,4}\s", re.M)


def _unescape(text: str) -> str:
    text = html.unescape(text)
    return _MD_ESCAPE_RX.sub(r"\1", text)


def _extract_score(heading_line: str):
    """从条目标题行提取分数, 提不到返回 None。"""
    m = _SCORE_EMOJI_RX.search(heading_line)
    if m:
        return float(m.group(1))
    m = _SCORE_EOL_RX.search(heading_line.strip())
    if m:
        return float(m.group(1))
    return None


def _norm(topic: str) -> str:
    """去重用归一化: 去空格与常见标点, 避免同一话题换个写法重复进。"""
    return re.sub(r"[\s·,，。！？!?:：、\-—_()\[\]\"\"''<>#*]", "", topic)


def _split_sections(text: str):
    """按 ##/### 标题切分简报, 返回 [(标题行, 正文)]。"""
    starts = [m.start() for m in _HEADING_RX.finditer(text)]
    if not starts:
        return []
    starts.append(len(text))
    sections = []
    for i in range(len(starts) - 1):
        chunk = text[starts[i]:starts[i + 1]]
        heading, _, body = chunk.partition("\n")
        sections.append((heading.strip(), body))
    return sections


def _block_topics(body: str):
    """抽取一个 section 内所有 hotspots 块的行, 保持原顺序。"""
    topics = []
    for block in HOTSPOTS_RX.findall(body):
        for line in block.splitlines():
            line = line.strip()
            m = re.match(r"^[-*]\s+(.+)$", line)
            if m:
                topic = m.group(1).strip()
                if topic:
                    topics.append(topic)
    return topics


def extract_scored_topics(md_path):
    """返回 [(score|None, [topics]), ...], 每项对应简报里的一个条目。"""
    with open(md_path, encoding="utf-8") as f:
        text = _unescape(f.read())
    scored = []
    for heading, body in _split_sections(text):
        topics = _block_topics(body)
        if not topics:
            continue
        scored.append((_extract_score(heading), topics))
    return scored


def legacy_extract(md_path):
    """旧行为: 不分条目, 全量抽取 (仅当简报里完全没有可解析分数时回退用)。"""
    with open(md_path, encoding="utf-8") as f:
        text = _unescape(f.read())
    topics = []
    for block in HOTSPOTS_RX.findall(text):
        for line in block.splitlines():
            line = line.strip()
            m = re.match(r"^[-*]\s+(.+)$", line)
            if m:
                topic = m.group(1).strip()
                if topic and topic not in topics:
                    topics.append(topic)
    return topics


def filter_topics(scored):
    """硬过滤, 返回 (topics, stats)。

    stats: kept_sections / dropped_sections / fallback_score(None=未回退)。
    """
    stats = {"kept_sections": 0, "dropped_sections": 0, "fallback_score": None}
    with_score = [(s, t) for s, t in scored if s is not None]
    if not with_score:
        return None, stats  # 调用方回退旧行为

    tiers = sorted({s for s, _ in with_score}, reverse=True)
    line = MIN_SCORE
    if all(s < MIN_SCORE for s in tiers):
        line = tiers[0]  # 当日无过线条目: 回退到最高分档
        stats["fallback_score"] = line

    kept = []  # (score, order, topic)
    order = 0
    for score, topics in with_score:
        if score < line:
            stats["dropped_sections"] += 1
            continue
        stats["kept_sections"] += 1
        for topic in topics[:MAX_PER_ITEM]:
            kept.append((score, order, topic))
            order += 1

    seen = set()
    topics = []
    for score, _, topic in sorted(kept, key=lambda x: (-x[0], x[1])):
        if _norm(topic) in seen:
            continue
        seen.add(_norm(topic))
        topics.append(topic)
    return topics, stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--summaries", required=True, help="Horizon 简报目录 (data/summaries)")
    ap.add_argument("--out", required=True, help="输出的 hotspots.json 路径")
    args = ap.parse_args()

    files = sorted(glob.glob(os.path.join(args.summaries, "horizon-*.md")))
    min_score = MIN_SCORE
    fallback_score = None
    if not files:
        print("未找到简报文件, hotspots.json 写空 topics", file=sys.stderr)
        topics = []
    else:
        latest = files[-1]
        print(f"读取最新简报: {latest}")
        scored = extract_scored_topics(latest)
        topics, stats = filter_topics(scored)
        if topics is None:
            # 简报里没有任何可解析的条目分数 (格式变了): 回退旧行为, 不静默丢数据
            print("警告: 简报中未解析到任何条目分数, 回退为全量抽取 (请检查简报格式)",
                  file=sys.stderr)
            topics = legacy_extract(latest)
            min_score = 0.0
        else:
            fallback_score = stats["fallback_score"]
            if fallback_score is not None:
                min_score = fallback_score
                print(f"当日无 {MIN_SCORE} 分以上条目, 回退到最高分档 {fallback_score} 分")
            print(f"硬过滤: 保留 {stats['kept_sections']} 个条目, "
                  f"丢弃 {stats['dropped_sections']} 个低于 {min_score} 分的条目, "
                  f"抽取到 {len(topics)} 个话题")

    today = datetime.date.today().isoformat()
    data = {
        "date": today,
        "scope": "人性台账爆款选题雷达·每日选题 (Horizon renxing profile, 图文源)",
        "queries": ["职场 OR 创业 OR 搞钱 OR 人性 OR 认知 OR 商业"],
        "topics": topics,
        "min_score": min_score,
        "fallback_score": fallback_score,
        "note": "由爆款雷达每日简报自动抽取, 供 v3.4.6 阶段3.5 gen_viral_content 使用。"
                "已按条目分数硬过滤 (低于 min_score 的条目不进), 使用前请人工过一遍: "
                "只选用与人性/商业判断主题真实相关的话题, 不硬蹭无关热点。",
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(f"已写入 {args.out}")


if __name__ == "__main__":
    main()
