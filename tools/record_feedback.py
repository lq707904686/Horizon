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

用法：
  python tools/record_feedback.py --date 2026-11-07 \\
    --title "谁把高校教师逼成了业务员" \\
    --category5 看职场 --motifs 分钱,信任 --formulas 数字冲击 \\
    --status 已选 [--reads 12000]

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


def _split(s):
    return [x.strip() for x in (s or "").split(",") if x.strip()]


def main():
    ap = argparse.ArgumentParser(description="记录一条选题反馈")
    ap.add_argument("--date", required=True, help="日期 YYYY-MM-DD")
    ap.add_argument("--title", required=True, help="选题标题（与简报一致）")
    ap.add_argument("--category5", default="", help="5 真分类，逗号分隔")
    ap.add_argument("--motifs", default="", help="人性母题，逗号分隔")
    ap.add_argument("--formulas", default="", help="金句公式，逗号分隔")
    ap.add_argument("--status", required=True, choices=list(RESULT_SCORES),
                    help="状态：已选/已写/爆款/明确弃/扑街")
    ap.add_argument("--reads", type=int, default=None, help="阅读量（报效果时填）")
    ap.add_argument("--log", default="data/feedback_log.jsonl",
                    help="反馈日志路径（默认 data/feedback_log.jsonl）")
    args = ap.parse_args()

    try:
        datetime.strptime(args.date, "%Y-%m-%d")
    except ValueError:
        print(f"日期格式错误：{args.date}，应为 YYYY-MM-DD", file=sys.stderr)
        return 1

    rec = {
        "date": args.date,
        "title": args.title,
        "category_5": _split(args.category5),
        "motifs": _split(args.motifs),
        "formulas": _split(args.formulas),
        "status": args.status,
        "result_score": RESULT_SCORES[args.status],
        "reads": args.reads,
        "logged_at": datetime.now().isoformat(timespec="seconds"),
    }

    log_path = Path(args.log)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    total = sum(1 for _ in open(log_path, "r", encoding="utf-8"))
    print(f"已记录：{args.date}「{args.title[:20]}」{args.status}（{RESULT_SCORES[args.status]:+d} 分）")
    print(f"日志：{log_path}，当前共 {total} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
