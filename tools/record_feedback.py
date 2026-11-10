#!/usr/bin/env python3
"""记录一条选题反馈（v1.8 反馈闭环：记录端）。

每天早上用户挑完选题后，由助理调用本脚本把“挑了哪条 / 写了哪条 /
效果如何”追加到 data/feedback_log.jsonl（JSON Lines）。
load_feedback.py 再按 14 天窗口聚合，给评分加权。

状态与分值（只记有信号的；15 选 3-5，“未选”不代表差，不记）：
  已选   +1  当天早上用户挑中该选题
  已写   +1  已写成文章发表（效果未报）
  爆款   +3  已发表且用户报效果好（阅读/在看明显高于平时）
  明确弃 -1  用户明确说不要这条
  扑街   -3  已发表且用户报效果差

视频记录（--kind video，与公众号记录互不干扰）：
  不用 --status（状态枚举只属于公众号路径，视频不用它装数据），
  用独立 kind: video 标记；分值由完播率按档位换算（见下）。
  完播率档位（锚定 2026-11-11 全链路分析 9 集实测分布：均值 16.92%、
  中位 18.42%、区间 7.82–25.73%，属 v1.8-video 暂行口径）：
    >= 22%  +3；>= 15%  +1；>= 10%  -1；< 10%  -3
  播放量/平均观看/3s 播放量只记原始值供以后用，不参与本次换算
  （完播率是报告认定的最可比留存指标）。

用法：
  python tools/record_feedback.py --date 2026-11-07 \\
    --title "谁把高校教师逼成了业务员" \\
    --category5 看职场 --motifs 分钱,信任 --formulas 数字冲击 \\
    --status 已选 [--reads 12000]

  python tools/record_feedback.py --kind video --date 2026-11-11 \\
    --rec-id REC-23 --title "120亿" --category5 做生意 \\
    --plays 1073 --completion 15.83 --avg-watch 25.54 --plays-3s 746 \\
    --publish-date 2026-11-10 --as-of 2026-11-11

标签（category5/motifs/formulas）从当日简报对应条目里取；
多个用逗号分隔；没有的留空。
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

RESULT_SCORES = {
    "已选": 1,
    "已写": 1,
    "爆款": 3,
    "明确弃": -1,
    "扑街": -3,
}

# 视频完播率 → 分值档位（见模块 docstring；暂行口径）。
VIDEO_COMPLETION_BANDS = ((22.0, 3), (15.0, 1), (10.0, -1))


def video_result_score(completion_pct):
    for threshold, score in VIDEO_COMPLETION_BANDS:
        if completion_pct >= threshold:
            return score
    return -3


def _split(s):
    return [x.strip() for x in (s or "").split(",") if x.strip()]


def _fail(msg):
    print(f"错误：{msg}", file=sys.stderr)
    return 1


def main():
    ap = argparse.ArgumentParser(description="记录一条选题反馈")
    ap.add_argument("--date", required=True, help="日期 YYYY-MM-DD")
    ap.add_argument("--title", default="", help="选题标题（与简报一致；视频可省略，默认用 rec-id）")
    ap.add_argument("--category5", default="", help="5 真分类，逗号分隔")
    ap.add_argument("--motifs", default="", help="人性母题，逗号分隔")
    ap.add_argument("--formulas", default="", help="金句公式，逗号分隔")
    ap.add_argument("--status", default=None, choices=list(RESULT_SCORES),
                    help="状态：已选/已写/爆款/明确弃/扑街（仅公众号记录用）")
    ap.add_argument("--reads", type=int, default=None, help="阅读量（报效果时填）")
    ap.add_argument("--kind", default="article", choices=["article", "video"],
                    help="记录类型：article（默认，公众号）/ video（视频号）")
    ap.add_argument("--rec-id", default=None, help="视频集编号，如 REC-23（仅视频）")
    ap.add_argument("--plays", type=int, default=None, help="播放量（仅视频）")
    ap.add_argument("--completion", type=float, default=None,
                    help="完播率，百分比数字，如 15.83（仅视频）")
    ap.add_argument("--avg-watch", type=float, default=None,
                    help="平均观看时长，秒（仅视频）")
    ap.add_argument("--plays-3s", type=int, default=None,
                    help="3 秒播放量，次（仅视频）")
    ap.add_argument("--publish-date", default=None,
                    help="视频发布日期 YYYY-MM-DD（仅视频）")
    ap.add_argument("--as-of", default=None,
                    help="指标口径日期 YYYY-MM-DD，默认等于 --date（仅视频）")
    ap.add_argument("--log", default="data/feedback_log.jsonl",
                    help="反馈日志路径（默认 data/feedback_log.jsonl）")
    args = ap.parse_args()

    try:
        datetime.strptime(args.date, "%Y-%m-%d")
    except ValueError:
        print(f"日期格式错误：{args.date}，应为 YYYY-MM-DD", file=sys.stderr)
        return 1

    video_args = (args.rec_id, args.plays, args.completion, args.avg_watch,
                  args.plays_3s, args.publish_date, args.as_of)
    if args.kind == "article":
        if any(v is not None for v in video_args):
            return _fail("--kind article 不能带视频参数（--rec-id/--plays/--completion/--avg-watch/--plays-3s/--publish-date/--as-of）")
        if args.status is None:
            return _fail("公众号记录必须传 --status")
        if not args.title:
            return _fail("公众号记录必须传 --title")
        result_score = RESULT_SCORES[args.status]
    else:  # video：状态枚举不动，不复用它装视频数据
        if args.status is not None:
            return _fail("视频记录不用 --status（状态枚举只属于公众号路径）")
        if args.reads is not None:
            return _fail("视频记录不用 --reads（阅读量只属于公众号路径），用 --plays")
        if not args.rec_id:
            return _fail("视频记录必须传 --rec-id")
        if args.plays is None or args.completion is None:
            return _fail("视频记录必须传 --plays 与 --completion")
        if args.plays < 0:
            return _fail("--plays 不能为负")
        if not 0 <= args.completion <= 100:
            return _fail("--completion 应在 0–100 之间")
        if args.avg_watch is not None and args.avg_watch < 0:
            return _fail("--avg-watch 不能为负")
        if args.plays_3s is not None:
            if args.plays_3s < 0:
                return _fail("--plays-3s 不能为负")
            if args.plays_3s > args.plays:
                return _fail("--plays-3s 不能大于 --plays")
        for label, value in (("--publish-date", args.publish_date),
                             ("--as-of", args.as_of)):
            if value is not None:
                try:
                    datetime.strptime(value, "%Y-%m-%d")
                except ValueError:
                    return _fail(f"{label} 格式错误：{value}，应为 YYYY-MM-DD")
        result_score = video_result_score(args.completion)

    rec = {
        "date": args.date,
        "kind": args.kind,
        "title": args.title or args.rec_id,
        "category_5": _split(args.category5),
        "motifs": _split(args.motifs),
        "formulas": _split(args.formulas),
        "result_score": result_score,
        "logged_at": datetime.now().isoformat(timespec="seconds"),
    }
    if args.kind == "article":
        rec["status"] = args.status
        rec["reads"] = args.reads
    else:
        rec["rec_id"] = args.rec_id
        rec["publish_date"] = args.publish_date
        rec["as_of"] = args.as_of or args.date
        plays_3s_rate = (round(args.plays_3s / args.plays * 100, 2)
                         if args.plays_3s is not None and args.plays else None)
        rec["video_metrics"] = {
            "plays": args.plays,
            "completion_pct": args.completion,
            "avg_watch_s": args.avg_watch,
            "plays_3s": args.plays_3s,
            "plays_3s_rate_pct": plays_3s_rate,
        }

    log_path = Path(args.log)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    total = sum(1 for _ in open(log_path, "r", encoding="utf-8"))
    if args.kind == "article":
        print(f"已记录：{args.date}「{args.title[:20]}」{args.status}（{result_score:+d} 分）")
    else:
        print(f"已记录视频：{args.date} {args.rec_id}「{(args.title or args.rec_id)[:20]}」"
              f"播放 {args.plays}、完播 {args.completion}%（{result_score:+d} 分）")
    print(f"日志：{log_path}，当前共 {total} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
