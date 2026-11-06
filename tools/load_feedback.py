#!/usr/bin/env python3
"""加载历史反馈并注入评分 prompt（v1.8 反馈闭环：加载端）。

由 daily-renxing.yml 在“运行爆款雷达”之前调用。读取
data/feedback_log.jsonl，按最近 N 天（默认 14 天）聚合各标签
（5 真分类 / 人性母题 / 金句公式）的平均分，折算成加权分后，
追加一段“历史反馈加权”到 runner 里的 profiles/renxing/analysis.md
末尾（runner 每次是全新 checkout，追加只影响当次运行，不污染仓库）。

冷启动保护：日志总覆盖天数不足 21 天时权重强制为 0，只打印提示、
不改 prompt；日志缺失/为空时同样不改 prompt、退出 0。
（覆盖天数按整本日志算，不只看聚合窗口——窗口默认 14 天，
若按窗口算冷启动永远过不了。）

加权规则（与 wechat-topic-radar skill 同款，保守起步）：
  标签调整分 = 该标签下历史反馈分平均值 × 0.15，限幅到 ±5 分；
  某选题的总调整 = 其 category_5 + motifs + formulas 标签分之和，
  再限幅到 ±5 分；加权不得推翻封顶（被封顶压到 6 分的条目，
  加权后仍不得超过 6 分）。

用法：
  python tools/load_feedback.py --window 14 \\
    --log data/feedback_log.jsonl --prompt profiles/renxing/analysis.md
  # 测试（跳过冷启动期限制）：
  python tools/load_feedback.py --window 14 --min-days 0
"""
import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

WEIGHT = 0.15
CAP = 5.0


def load_recent(log_path, window_days):
    """加载最近 N 天反馈记录；日志缺失返回空列表。"""
    if not log_path.exists():
        return []
    cutoff = datetime.now() - timedelta(days=window_days)
    records = []
    with open(log_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            d = rec.get("date", "")
            try:
                dt = datetime.strptime(d, "%Y-%m-%d")
            except Exception:
                continue
            if dt < cutoff:
                continue
            records.append(rec)
    return records


def aggregate(records):
    """按标签聚合平均分 → 加权分（限幅 ±CAP）。"""
    bucket = defaultdict(list)
    for rec in records:
        score = rec.get("result_score", 0)
        for c in rec.get("category_5") or []:
            bucket[f"category_5:{c}"].append(score)
        for m in rec.get("motifs") or []:
            bucket[f"motifs:{m}"].append(score)
        for f in rec.get("formulas") or []:
            bucket[f"formulas:{f}"].append(score)
    out = {}
    for tag, scores in bucket.items():
        avg = sum(scores) / len(scores)
        adj = round(avg * WEIGHT, 2)
        adj = max(-CAP, min(CAP, adj))
        out[tag] = adj
    return out


def build_section(tag_scores, window_days, record_count, days_covered):
    payload = {
        "window_days": window_days,
        "record_count": record_count,
        "days_covered": days_covered,
        "weight": WEIGHT,
        "cap": CAP,
        "tag_scores": dict(sorted(tag_scores.items(), key=lambda x: -x[1])),
    }
    return (
        "\n\n## 历史反馈加权（v1.8 自动生成，勿手改）\n"
        "\n"
        "> 以下为最近 14 天用户实际挑选与文章效果的统计，仅作微调参考。\n"
        "> 先按五维打分与三条封顶正常评分，再按标签加权调整总分：某选题的调整分 = "
        "其 category_5、motifs、formulas 标签对应分值之和，限幅到 ±5 分。\n"
        "> 加权不得推翻封顶：被封顶压到 6 分的条目，加权后仍不得超过 6 分。\n"
        "\n"
        "```json\n"
        + json.dumps(payload, ensure_ascii=False, indent=1)
        + "\n```\n"
    )


def load_all_dates(log_path):
    """整本日志覆盖的不同日期数（冷启动判断用，不只看窗口）。"""
    if not log_path.exists():
        return set()
    dates = set()
    with open(log_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            d = rec.get("date", "")
            try:
                datetime.strptime(d, "%Y-%m-%d")
                dates.add(d)
            except Exception:
                continue
    return dates


def main():
    ap = argparse.ArgumentParser(description="加载历史反馈并注入评分 prompt")
    ap.add_argument("--window", type=int, default=14, help="最近 N 天（默认 14）")
    ap.add_argument("--log", default="data/feedback_log.jsonl",
                    help="反馈日志路径")
    ap.add_argument("--prompt", default="profiles/renxing/analysis.md",
                    help="要追加的 prompt 文件")
    ap.add_argument("--min-days", type=int, default=21,
                    help="冷启动期：日志总覆盖天数不足此值时权重为 0（默认 21）")
    args = ap.parse_args()

    log_path = Path(args.log)
    records = load_recent(log_path, args.window)
    if not records:
        print(f"无反馈数据（{log_path} 缺失或窗口内为空），权重为 0，不改 prompt。")
        return 0

    days_covered = len(load_all_dates(log_path))
    if days_covered < args.min_days:
        print(f"冷启动期：日志总覆盖仅 {days_covered} 天（需 {args.min_days} 天），"
              f"权重为 0，不改 prompt。窗口内共 {len(records)} 条记录。")
        return 0

    tag_scores = aggregate(records)
    section = build_section(tag_scores, args.window, len(records), days_covered)

    prompt_path = Path(args.prompt)
    text = prompt_path.read_text(encoding="utf-8")
    marker = "## 历史反馈加权（v1.8 自动生成，勿手改）"
    if marker in text:
        text = text[: text.index(marker)].rstrip() + "\n"
    prompt_path.write_text(text.rstrip() + "\n" + section, encoding="utf-8")

    print(f"已注入反馈加权：窗口 {args.window} 天，{len(records)} 条记录，"
          f"覆盖 {days_covered} 天，{len(tag_scores)} 个标签。")
    for tag, adj in sorted(tag_scores.items(), key=lambda x: -x[1]):
        print(f"  {tag}: {adj:+.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
