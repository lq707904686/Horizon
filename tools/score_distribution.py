#!/usr/bin/env python3
"""每日评分分布留存（终轮整改 2026-11-12，P1-14）。

问题：雷达跑了 12 天，仓库里没有任何一个自动汇总分数分布的文件。
“选题荒还是标准收紧”只能靠手工逐条解析 gh-pages 简报来回答
（2026-11-12 终轮就是这么手工挖的 12 天分布）。

本工具：从当日最新简报解析全部带分条目，生成一条分布记录，
按日期追加进 data/score_distribution.json（已存在则按日期去重覆盖）。
由 tools/make_hotspots.py 在每轮写完 hotspots.json 后顺带调用，
也可独立跑：

    python tools/score_distribution.py --summaries data/summaries
    python tools/score_distribution.py --summaries data/summaries --out data/score_distribution.json

记录字段：
  date                 简报日期（文件名 horizon-YYYY-MM-DD.md，取不到则用今天）
  total_items          简报带分条目数
  candidate_pool_total 候选池总数（简报头“从 N 条内容中筛选出 M 条”，取不到为 null）
  max_score            当日最高分（无条目为 null）
  count_ge_7 / count_ge_5  ≥7.0 / ≥5.0 的条目数
  bands                 分段计数：below_5 / 5.0-5.9 / 6.0-6.9 / 7.0-7.9 / 8.0+
  per_source           各源贡献条数（来源行取不到的记为 "unknown"，
                       只统计已进简报的条目，不是全池贡献）
"""

import argparse
import datetime
import glob
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from make_hotspots import _POOL_RX, extract_scored_topics  # noqa: E402

_DATE_RX = re.compile(r"horizon-(\d{4}-\d{2}-\d{2})\.md$")

BANDS = ("below_5", "5.0-5.9", "6.0-6.9", "7.0-7.9", "8.0+")


def _band(score: float) -> str:
    if score < 5:
        return "below_5"
    if score < 6:
        return "5.0-5.9"
    if score < 7:
        return "6.0-6.9"
    if score < 8:
        return "7.0-7.9"
    return "8.0+"


def _pool_total(md_path: str):
    """从简报头解析候选池总数，取不到返回 None。"""
    try:
        with open(md_path, encoding="utf-8") as f:
            head = f.read(2000)
    except OSError:
        return None
    m = _POOL_RX.search(head)
    return int(m.group(1)) if m else None


def build_distribution(scored, md_path=None, date=None):
    """从 extract_scored_topics 的结果生成一条分布记录。

    scored: [(score|None, video_score|None, category|None, source|None,
    url|None, cap|None, feasibility|None, feasibility_reason|None,
    [topics]), ...]。无分条目不计入分布。
    """
    if date is None:
        date = datetime.date.today().isoformat()
        if md_path:
            m = _DATE_RX.search(os.path.basename(md_path))
            if m:
                date = m.group(1)
    scores = [s for s, *_ in scored if s is not None]
    bands = {b: 0 for b in BANDS}
    per_source = {}
    for score, _v, _c, source, _u, _cap, _feas, _feas_reason, _t in scored:
        if score is None:
            continue
        bands[_band(score)] += 1
        per_source[source or "unknown"] = per_source.get(source or "unknown", 0) + 1
    return {
        "date": date,
        "total_items": len(scores),
        "candidate_pool_total": _pool_total(md_path) if md_path else None,
        "max_score": max(scores) if scores else None,
        "count_ge_7": sum(1 for s in scores if s >= 7.0),
        "count_ge_5": sum(1 for s in scores if s >= 5.0),
        "bands": bands,
        "per_source": dict(sorted(per_source.items(), key=lambda kv: -kv[1])),
    }


def update_history(path, record):
    """把 record 按 date 去重追加进 path（{"records": [...]}，按日期排序）。"""
    history = {"records": []}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            try:
                history = json.load(f)
            except json.JSONDecodeError:
                history = {"records": []}
    records = [r for r in history.get("records", []) if r.get("date") != record["date"]]
    records.append(record)
    records.sort(key=lambda r: r.get("date", ""))
    history["records"] = records
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)
        f.write("\n")
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--summaries", required=True, help="Horizon 简报目录 (data/summaries)")
    ap.add_argument("--out", default=None,
                    help="分布历史文件，默认 <summaries 上级>/score_distribution.json")
    args = ap.parse_args()
    files = sorted(glob.glob(os.path.join(args.summaries, "horizon-*.md")))
    if not files:
        print("未找到简报文件", file=sys.stderr)
        return 1
    latest = files[-1]
    record = build_distribution(extract_scored_topics(latest), md_path=latest)
    out = args.out or os.path.join(
        os.path.dirname(os.path.abspath(args.summaries)), "score_distribution.json")
    update_history(out, record)
    print(f"已更新 {out}：{record['date']}，共 {record['total_items']} 条，"
          f"最高 {record['max_score']}，≥7.0 有 {record['count_ge_7']} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
