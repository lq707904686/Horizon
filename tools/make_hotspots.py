#!/usr/bin/env python3
"""从 Horizon 每日简报中抽取 ```hotspots 代码块, 生成 hotspots.json.

hotspots.json 是「人性台账」生产流程 v3.4.6 阶段 3.5 (gen_viral_content)
的热点输入契约, 字段含义见《爆款内容方法论》第五节。
用法: python tools/make_hotspots.py --summaries data/summaries --out hotspots.json
"""
import argparse
import datetime
import glob
import json
import os
import re
import sys

HOTSPOTS_RX = re.compile(r"```hotspots\s*\n(.*?)```", re.S)


def extract_topics(md_path):
    with open(md_path, encoding="utf-8") as f:
        text = f.read()
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--summaries", required=True, help="Horizon 简报目录 (data/summaries)")
    ap.add_argument("--out", required=True, help="输出的 hotspots.json 路径")
    args = ap.parse_args()

    files = sorted(glob.glob(os.path.join(args.summaries, "horizon-*.md")))
    if not files:
        print("未找到简报文件, hotspots.json 写空 topics", file=sys.stderr)
        topics = []
    else:
        latest = files[-1]
        print(f"读取最新简报: {latest}")
        topics = extract_topics(latest)
        print(f"抽取到 {len(topics)} 个话题")

    today = datetime.date.today().isoformat()
    data = {
        "date": today,
        "scope": "人性台账爆款雷达·每日选题 (Horizon renxing profile, 图文源)",
        "queries": ["职场 OR 创业 OR 搞钱 OR 人性 OR 认知 OR 商业"],
        "topics": topics,
        "note": "由爆款雷达每日简报自动抽取, 供 v3.4.6 阶段3.5 gen_viral_content 使用。"
                "使用前请人工过一遍: 只选用与人性/商业判断主题真实相关的话题, 不硬蹭无关热点。",
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(f"已写入 {args.out}")


if __name__ == "__main__":
    main()
