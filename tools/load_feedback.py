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

视频路径（与公众号路径并行、互不干扰）：
  只取 kind == video 的记录另算一套“视频标签分”，权重 0.15、
  限幅 ±5 不变，写入独立 video_tag_scores 段，**仅供 video_score
  参考，不参与公众号 score、不推翻任何封顶**。
  冷启动门槛与公众号路径不同：视频满 20 条窗口内记录即启动
  （--min-video-records，默认 20），不看 21 天覆盖。

用法：
  python tools/load_feedback.py --window 14 \\
    --log data/feedback_log.jsonl --prompt profiles/renxing/analysis.md
  # 测试（跳过冷启动期限制）：
  python tools/load_feedback.py --window 14 --min-days 0
  # 测试视频路径（跳过 20 条门槛）：
  python tools/load_feedback.py --window 14 --min-video-records 0
"""
import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

WEIGHT = 0.15
CAP = 5.0
VIDEO_MIN_RECORDS = 20  # 视频路径冷启动门槛：窗口内满 20 条视频记录即启动

ARTICLE_MARKER = "## 历史反馈加权（v1.8 自动生成，勿手改）"
VIDEO_MARKER = "## 视频反馈加权（v1.8 自动生成，勿手改）"


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


def build_video_section(video_tag_scores, window_days, video_record_count):
    """视频标签分段：仅供 video_score 参考，不参与公众号 score。"""
    payload = {
        "window_days": window_days,
        "video_record_count": video_record_count,
        "min_video_records": VIDEO_MIN_RECORDS,
        "weight": WEIGHT,
        "cap": CAP,
        "video_tag_scores": dict(sorted(video_tag_scores.items(),
                                        key=lambda x: -x[1])),
    }
    return (
        "\n\n## 视频反馈加权（v1.8 自动生成，勿手改）\n"
        "\n"
        "> 以下为窗口内视频号播放数据的统计，仅供 video_score 参考。\n"
        "> 不参与公众号 score，不推翻任何封顶：某选题的视频调整分 = "
        "其 category_5、motifs、formulas 标签对应分值之和，限幅到 ±5 分。\n"
        "> 单条视频分值由完播率按档位换算（见 record_feedback.py）。\n"
        "\n"
        "```json\n"
        + json.dumps(payload, ensure_ascii=False, indent=1)
        + "\n```\n"
    )


def strip_section(text, marker):
    """删掉 prompt 里已有的某一段（幂等重跑用）；没有则原样返回。"""
    if marker in text:
        text = text[: text.index(marker)].rstrip() + "\n"
    return text


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
    ap.add_argument("--min-video-records", type=int, default=VIDEO_MIN_RECORDS,
                    help="视频冷启动：窗口内视频记录不足此值时视频权重为 0"
                         f"（默认 {VIDEO_MIN_RECORDS}）")
    args = ap.parse_args()

    log_path = Path(args.log)
    records = load_recent(log_path, args.window)
    if not records:
        print(f"无反馈数据（{log_path} 缺失或窗口内为空），权重为 0，不改 prompt。")
        return 0

    # 两条路径各走各的门槛、互不干扰（kind 缺失的老记录按公众号算）。
    article_records = [r for r in records if r.get("kind", "article") != "video"]
    video_records = [r for r in records if r.get("kind") == "video"]

    article_section = None
    if article_records:
        days_covered = len(load_all_dates(log_path))
        if days_covered < args.min_days:
            print(f"冷启动期：日志总覆盖仅 {days_covered} 天（需 {args.min_days} 天），"
                  f"公众号权重为 0，不改公众号段。窗口内共 {len(article_records)} 条公众号记录。")
        else:
            tag_scores = aggregate(article_records)
            article_section = build_section(tag_scores, args.window,
                                            len(article_records), days_covered)
            print(f"已注入反馈加权：窗口 {args.window} 天，{len(article_records)} 条记录，"
                  f"覆盖 {days_covered} 天，{len(tag_scores)} 个标签。")
            for tag, adj in sorted(tag_scores.items(), key=lambda x: -x[1]):
                print(f"  {tag}: {adj:+.2f}")

    video_section = None
    if video_records:
        if len(video_records) < args.min_video_records:
            print(f"视频冷启动期：窗口内仅 {len(video_records)} 条视频记录"
                  f"（需 {args.min_video_records} 条），视频权重为 0，不改视频段。")
        else:
            video_tag_scores = aggregate(video_records)
            video_section = build_video_section(video_tag_scores, args.window,
                                                len(video_records))
            print(f"已注入视频反馈加权：窗口 {args.window} 天，{len(video_records)} 条视频记录，"
                  f"{len(video_tag_scores)} 个标签。")
            for tag, adj in sorted(video_tag_scores.items(), key=lambda x: -x[1]):
                print(f"  视频 {tag}: {adj:+.2f}")

    if article_section is None and video_section is None:
        print("两条路径均未达启动门槛，不改 prompt。")
        return 0

    prompt_path = Path(args.prompt)
    text = prompt_path.read_text(encoding="utf-8")
    text = strip_section(text, VIDEO_MARKER)
    text = strip_section(text, ARTICLE_MARKER)
    new_text = text.rstrip() + "\n"
    if article_section is not None:
        new_text += article_section
    if video_section is not None:
        new_text += video_section
    prompt_path.write_text(new_text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
