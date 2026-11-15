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
  冷启动门槛与公众号路径不同：视频满 10 条、30 天窗口内记录即启动
  （--min-video-records，默认 10；视频窗口固定 30 天，不跟 --window，
  因视频发布约 1 集/天、14 天窗口内攒满 20 条结构上够不着）。

用法：
  python tools/load_feedback.py --window 14 \\
    --log data/feedback_log.jsonl --prompt profiles/renxing/analysis.md
  # 测试（跳过冷启动期限制）：
  python tools/load_feedback.py --window 14 --min-days 0
  # 测试视频路径（跳过 10 条门槛）：
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
VIDEO_MIN_RECORDS = 10  # 视频路径冷启动门槛：30 天窗口内满 10 条视频记录即启动
VIDEO_WINDOW_DAYS = 30  # 视频路径聚合窗口（固定，不跟 --window）
# P317：日志应每天记——最新一行超过 2 天即告警（只喊出来，不阻塞跑雷达）。
FEEDBACK_STALE_AFTER_DAYS = 2

ARTICLE_MARKER = "## 历史反馈加权（v1.8 自动生成，勿手改）"
VIDEO_MARKER = "## 视频反馈加权（v1.8 自动生成，勿手改）"
# D5 防同质化：近 3 天已用（归属，主母题，主公式）三元组，供模型核对公式重复。
# 本段是事实历史，不是加权——冷启动期也照常追加（权重为 0 不影响本段）。
RECENT_MARKER = "## 近 3 天已用母题与公式（防同质化，勿手改）"
RECENT_DAYS = 3


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


def recent_combos(article_records, days=RECENT_DAYS):
    """近 N 天已选/已写选题的三元组（归属，主母题，主公式）。

    每条记录取第一角度的标签（category_5[0] / motifs[0] / formulas[0]）；
    缺失的字段留空字符串。供 D5 公式重复降分核对用。
    """
    cutoff = datetime.now().date() - timedelta(days=days)
    combos = []
    for rec in article_records:
        d = rec.get("date", "")
        try:
            dt = datetime.strptime(d, "%Y-%m-%d").date()
        except Exception:
            continue
        if dt < cutoff:
            continue
        cats = rec.get("category_5") or []
        motifs = rec.get("motifs") or []
        formulas = rec.get("formulas") or []
        combos.append({
            "date": d,
            "title": rec.get("title", ""),
            "category_5": cats[0] if cats else "",
            "motif": motifs[0] if motifs else "",
            "formula": formulas[0] if formulas else "",
        })
    return combos


def build_recent_section(combos, days=RECENT_DAYS):
    """近 3 天已用三元组段：事实历史，冷启动期也照常追加。"""
    payload = {
        "days": days,
        "count": len(combos),
        "combos": combos,
    }
    return (
        "\n\n## 近 3 天已用母题与公式（防同质化，勿手改）\n"
        "\n"
        "> 以下为近 3 天已选 / 已写的选题三元组（归属，主母题，主公式）。\n"
        "> 评分时核对：某候选条目的三元组若与表中任一完全相同，总分降 1 分，\n"
        "> reason 注明\"公式重复：与 X 月 X 日《标题》同款\"（见 analysis.md 公式重复降分）。\n"
        "> 不在表中的不算重复；同一事件有新进展时按升分触发器与回访规则走，不走本条。\n"
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


def load_all_dates(log_path, kinds=None):
    """整本日志覆盖的不同日期数（冷启动判断用，不只看窗口）。

    kinds 传入时只计该类记录（kind 缺失的老记录按公众号算）。
    P317 纠正：公众号路径只计 article 行——旧版把 11-11 视频行的日期
    也算进公众号覆盖，诊断的 5 天里有 1 天是视频的，实为 4 天。
    """
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
            if kinds is not None and rec.get("kind", "article") not in kinds:
                continue
            d = rec.get("date", "")
            try:
                datetime.strptime(d, "%Y-%m-%d")
                dates.add(d)
            except Exception:
                continue
    return dates


def latest_log_date(log_path):
    """整本日志最新一行的 date；日志缺失/无有效日期返回 None。"""
    latest = None
    if not log_path.exists():
        return None
    with open(log_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                d = datetime.strptime(rec.get("date", ""), "%Y-%m-%d").date()
            except Exception:
                continue
            if latest is None or d > latest:
                latest = d
    return latest


def missing_dates_since(last_date, today):
    """last_date 之后、today 之前缺失的日期（最多列 10 个）。"""
    if last_date is None:
        return []
    return [
        (last_date + timedelta(days=i)).isoformat()
        for i in range(1, (today - last_date).days)
    ][:10]


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
    # 视频路径用独立的 30 天窗口（固定，不跟 --window），与公众号路径互不干扰。
    video_records = [
        r for r in load_recent(log_path, VIDEO_WINDOW_DAYS)
        if r.get("kind") == "video"
    ]
    # P317：缺失告警——日志自 11-11 起再无新行，两条权重恒 0 却无人知晓。
    # 只告警、不阻塞（返回码保持 0，雷达照常跑；真空窗由人补记，不由机器猜）。
    _today = datetime.now().date()
    _latest = latest_log_date(log_path)
    if _latest is not None:
        _gap = (_today - _latest).days
        if _gap > FEEDBACK_STALE_AFTER_DAYS:
            _missing = missing_dates_since(_latest, _today)
            print(f"⚠ 反馈缺失告警：日志最新一行是 {_latest.isoformat()}，"
                  f"距今天已 {_gap} 天（应每天记）。"
                  f"缺失日期：{'、'.join(_missing) or '无'}。"
                  "请用 tools/record_feedback.py 按发布日补记。")
    elif not log_path.exists():
        print("⚠ 反馈缺失告警：反馈日志不存在，请先记第一行。")
    if not records and not video_records:
        print(f"无反馈数据（{log_path} 缺失或窗口内为空），权重为 0，不改 prompt。")
        return 0

    # 两条路径各走各的门槛、互不干扰（kind 缺失的老记录按公众号算）。
    article_records = [r for r in records if r.get("kind", "article") != "video"]

    article_section = None
    if article_records:
        days_covered = len(load_all_dates(log_path, kinds={"article"}))
        if days_covered < args.min_days:
            print(f"冷启动期：日志总覆盖仅 {days_covered} 天（需 {args.min_days} 天），"
                  f"还差 {args.min_days - days_covered} 天（每天记 1 行以上，攒够即通），"
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
            if len(video_records) == args.min_video_records - 1:
                # P317：只差 1 条即通——把补 1 条的清单直接印出来，别让人再翻文档。
                _logged = sorted({r.get("rec_id") for r in video_records
                                  if r.get("rec_id")})
                print("  只差 1 条即通：补 1 条视频记录即可启动——")
                print(f"  已记 rec_id：{'、'.join(_logged) or '无'}；"
                      "挑其中没有的、已发布的 1 集，发布日当 --date：")
                print("  python tools/record_feedback.py --kind video --date <发布日>"
                      " --rec-id REC-xx --title \"<标题>\" --plays N --completion X"
                      " --publish-date <发布日> --as-of <指标口径日>")
                print("  补完再跑本脚本验：视频段出现、告警消失才算通。")
        else:
            video_tag_scores = aggregate(video_records)
            video_section = build_video_section(video_tag_scores, VIDEO_WINDOW_DAYS,
                                                len(video_records))
            print(f"已注入视频反馈加权：窗口 {VIDEO_WINDOW_DAYS} 天，{len(video_records)} 条视频记录，"
                  f"{len(video_tag_scores)} 个标签。")
            for tag, adj in sorted(video_tag_scores.items(), key=lambda x: -x[1]):
                print(f"  视频 {tag}: {adj:+.2f}")

    # D5 防同质化段：事实历史，不走冷启动门槛——近 3 天有已用组合就追加。
    recent_combos_list = recent_combos(article_records)
    recent_section = None
    if recent_combos_list:
        recent_section = build_recent_section(recent_combos_list)
        print(f"已注入近 3 天已用组合：{len(recent_combos_list)} 条（防同质化，冷启动期也追加）。")

    if article_section is None and video_section is None and not recent_combos_list:
        print("两条路径均未达启动门槛，且近 3 天无已用组合，不改 prompt。")
        return 0

    prompt_path = Path(args.prompt)
    text = prompt_path.read_text(encoding="utf-8")
    text = strip_section(text, VIDEO_MARKER)
    text = strip_section(text, ARTICLE_MARKER)
    text = strip_section(text, RECENT_MARKER)
    new_text = text.rstrip() + "\n"
    if recent_section is not None:
        new_text += recent_section
    if article_section is not None:
        new_text += article_section
    if video_section is not None:
        new_text += video_section
    prompt_path.write_text(new_text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
