#!/usr/bin/env python3
"""抓取中文热榜（百度热搜、头条热榜、今日热榜 5 节点），生成本地 RSS 2.0 文件。

Horizon 通过 RSS 抓取器消费这些文件（workflow 里用本机 http.server
serving，源地址配成 http://127.0.0.1:8123/*.xml），零改 Horizon 内核。

注意：
- 每条目必须带 <pubDate>（24 小时内），否则 Horizon 的 RSS 抓取器
  会按日期过滤掉（_parse_date 返回 None 的条目直接跳过）。
- P318-7：pubDate 写首见日期而非 now（状态见 data/hotlists_seen.json，
  workflow 随 hotspots 一起提交才跨日有效）——滞留榜单超过 24 小时
  即由抓取器自然过滤，不再每天变“新”。
- P318-6：0 条的源写空文件（不写有效 RSS），让 workflow 的 BUG-1
  检查（test -s）真正拦住死源。
- 失败的源只告警不中断，保证 workflow 继续跑。

用法: python tools/fetch_hotlists.py --out-dir data/hotlists
"""
import argparse
import datetime
import http.client
import json
import os
import re
import sys
import time
import urllib.request
from email.utils import format_datetime
from xml.sax.saxutils import escape

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def _get(url, timeout=20, retries=3):
    """抓取文本，兼容服务端提前断开（IncompleteRead 取已读部分）。"""
    last = None
    for _ in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", errors="ignore")
        except http.client.IncompleteRead as e:
            last = e.partial.decode("utf-8", errors="ignore")
            time.sleep(1)
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1)
    if isinstance(last, str):
        return last
    raise last if isinstance(last, Exception) else RuntimeError("fetch failed")


def fetch_baidu():
    """百度热搜：公开 JSON 接口，免登录，约 50 条。"""
    items = []
    try:
        data = json.loads(
            _get("https://top.baidu.com/api/board?platform=pc&tab=realtime")
        )
        for card in data.get("data", {}).get("cards", []):
            for i, x in enumerate(card.get("content", []), 1):
                word = (x.get("word") or "").strip()
                if not word:
                    continue
                desc = (x.get("desc") or "").strip()
                hot = x.get("hotScore") or ""
                items.append(
                    {
                        "title": word,
                        "link": x.get("url") or "https://top.baidu.com/board?tab=realtime",
                        "desc": f"{desc}\n（百度热搜第{i}名，热度{hot}）"
                        if desc
                        else f"百度热搜第{i}名，热度{hot}：{word}",
                        "guid": f"baidu-{x.get('index') or word}",
                    }
                )
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 百度热搜抓取失败: {e}", file=sys.stderr)
    return items


def _jstr(s):
    """把 JSON 字符串片段反转义。"""
    try:
        return json.loads(f'"{s}"')
    except Exception:  # noqa: BLE001
        return s


def _toutiao_items(text):
    """解析头条热榜 JSON；残缺时降级按对象切分提取。"""
    try:
        data = json.loads(text)
        return [
            (
                (x.get("Title") or "").strip(),
                x.get("Url") or "",
                str(x.get("HotValue") or ""),
                str(x.get("ClusterIdStr") or x.get("Title") or ""),
            )
            for x in data.get("data", [])
            if (x.get("Title") or "").strip()
        ]
    except Exception:  # noqa: BLE001
        pass
    out = []
    for ch in re.split(r'\{"ClusterId":', text)[1:]:
        tm = re.search(r'"Title":"((?:[^"\\]|\\.)*)"', ch)
        if not tm:
            continue
        um = re.search(r'"Url":"((?:[^"\\]|\\.)*)"', ch)
        hm = re.search(r'"HotValue":(\d+)', ch)
        cm = re.search(r'"ClusterIdStr":"?(\d+)"?', ch)
        out.append(
            (
                _jstr(tm.group(1)),
                _jstr(um.group(1)) if um else "",
                hm.group(1) if hm else "",
                cm.group(1) if cm else _jstr(tm.group(1)),
            )
        )
    return out


def fetch_toutiao():
    """头条热榜：页面直接返回 JSON（含 data 数组），免登录，约 50 条。"""
    items = []
    try:
        raw = _get("https://www.toutiao.com/hot-event/hot-board/?origin=toutiao_pc")
        for i, (title, url, hot, cid) in enumerate(_toutiao_items(raw), 1):
            items.append(
                {
                    "title": title,
                    "link": url or "https://www.toutiao.com/",
                    "desc": f"头条热榜第{i}名，热度{hot}：{title}",
                    "guid": f"toutiao-{cid}",
                }
            )
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 头条热榜抓取失败: {e}", file=sys.stderr)
    return items


# 今日热榜（tophub.today）节点：服务端直出 HTML，curl 可抓，免登录。
# 节点 ID 来自首页节点卡片（<a href="/n/{id}"> 后紧跟的 zb-kc-Cb 即节点名）。
TOPHUB_NODES = [
    # (节点ID, 平台域名过滤, RSS文件名, 频道名)
    ("WnBe01o371", "weixin", "weixin.xml", "微信24h热文榜"),
    ("KqndgxeLl9", "weibo", "weibo.xml", "微博热搜榜"),
    ("DpQvNABoNE", "douyin", "douyin.xml", "抖音总榜"),
    ("74KvxwokxM", "bilibili", "bilibili.xml", "哔哩哔哩全站日榜"),
    ("mproPpoq6O", "zhihu", "zhihu.xml", "知乎热榜"),
    # 2026-11-20 雷达线路扩容新增（实测：第一财经 10 条、虎嗅热文 15 条）：
    # 雪球/华尔街见闻偏个股，与“排除股市个股”口径冲突，不接；
    # tophub 36氪节点与 sinrowa 36氪热榜疑似同一上游，不重复接。
    ("0MdKam4ow1", "yicai", "yicai.xml", "第一财经"),
    ("5VaobgvAj1", "huxiu", "huxiu_hot.xml", "虎嗅网热文"),
]


def fetch_tophub_node(node_id, domain_filter):
    """抓一个今日热榜节点页，返回 [(标题, 链接)]，按标题去重保序。"""
    html = _get(f"https://tophub.today/n/{node_id}")
    links = re.findall(
        r'href="(https?://[^"]+)"[^>]*>([^<]{4,80})</a>', html
    )
    seen, items = set(), []
    for url, title in links:
        title = title.strip()
        if domain_filter not in url or len(title) < 4 or title in seen:
            continue
        seen.add(title)
        items.append((title, url))
    return items


def fetch_tophub():
    """今日热榜 5 个节点 → {rss文件名: (频道名, items)}。"""
    out = {}
    for node_id, domain, fname, feed_name in TOPHUB_NODES:
        try:
            raw = fetch_tophub_node(node_id, domain)
            items = [
                {
                    "title": title,
                    "link": url,
                    "desc": f"{feed_name}：{title}",
                    "guid": f"tophub-{node_id}-{title}",
                }
                for title, url in raw
            ]
            out[fname] = (feed_name, items)
        except Exception as e:  # noqa: BLE001
            print(f"[warn] 今日热榜 {feed_name} 抓取失败: {e}", file=sys.stderr)
    return out


# NewsNow 试点已于 2026-11-13 结束（7 天最佳 6.5，未达 7.0 保留线），三源已撤。


# P318-7：首见日期留存——热榜条目按 guid 记首见日，pubDate 写首见日。
# 本轮未出现且首见超过 30 天的 guid 清掉，免得一条消失一个月的旧闻
# 回来仍被当成旧闻（回榜即按新条目计）。
SEEN_PRUNE_DAYS = 30


def load_seen(path):
    """容错读首见日期状态文件：缺失/损坏返回 {}。"""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items()
            if isinstance(k, str) and isinstance(v, str)}


def save_seen(path, seen):
    """写首见日期状态文件（guid -> 首见日 ISO）。"""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(seen, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


def apply_first_seen(items, seen, today):
    """P318-7：给每条目按 guid 记首见日期，返回 (items, seen)。

    新 guid 的首见日为 today；已见过的沿用旧首见日。条目 dict 上挂
    "first_seen"（date），write_rss 据此写 pubDate；24h 窗口外条目
    由 RSS 抓取器自然过滤。
    """
    seen = dict(seen)
    for it in items:
        guid = it.get("guid") or it.get("title") or ""
        first = seen.get(guid)
        if first is None:
            first = today.isoformat()
            seen[guid] = first
        try:
            it["first_seen"] = datetime.date.fromisoformat(first)
        except ValueError:
            it["first_seen"] = today
            seen[guid] = today.isoformat()
    return items, seen


def prune_seen(seen, live_guids, today):
    """清掉本轮未出现且首见超过 SEEN_PRUNE_DAYS 天的 guid。"""
    cutoff = today - datetime.timedelta(days=SEEN_PRUNE_DAYS)
    kept = {}
    for guid, first in seen.items():
        try:
            first_date = datetime.date.fromisoformat(first)
        except ValueError:
            continue
        if guid not in live_guids and first_date < cutoff:
            continue
        kept[guid] = first
    return kept


def write_rss(items, feed_title, path):
    if not items:
        # P318-6：0 条不写有效 RSS——写空文件，让 workflow 的 BUG-1 检查
        # （test -s）真正拦住死源。此前 0 条也照写带 channel 的 RSS，
        # 空文件检查永远通过，死源当天不可见。
        with open(path, "w", encoding="utf-8") as f:
            f.write("")
        print(f"警告：{feed_title} 0 条，{path} 写空文件（死源待查）",
              file=sys.stderr)
        return
    now = format_datetime(datetime.datetime.now(datetime.timezone.utc))
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0"><channel>',
        f"<title>{escape(feed_title)}</title>",
        f"<link>{escape('https://github.com/lq707904686/Horizon')}</link>",
        f"<description>{escape(feed_title)}</description>",
        f"<pubDate>{now}</pubDate>",
    ]
    for it in items:
        # P318-7：条目 pubDate 写首见日（无首见记录时写 now）。
        first_seen = it.get("first_seen")
        if isinstance(first_seen, datetime.date):
            pub = format_datetime(datetime.datetime.combine(
                first_seen, datetime.time.min, tzinfo=datetime.timezone.utc))
        else:
            pub = now
        parts += [
            "<item>",
            f"<title>{escape(it['title'])}</title>",
            f"<link>{escape(it['link'])}</link>",
            f"<description>{escape(it['desc'])}</description>",
            f"<guid isPermaLink=\"false\">{escape(it['guid'])}</guid>",
            f"<pubDate>{pub}</pubDate>",
            "</item>",
        ]
    parts.append("</channel></rss>")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(parts))
    print(f"已写入 {path}（{len(items)} 条）")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", required=True, help="RSS 输出目录")
    ap.add_argument("--seen-file", default=None,
                    help="首见日期状态文件，默认 <out-dir>/../hotlists_seen.json")
    args = ap.parse_args()
    os.makedirs(args.out_dir, exist_ok=True)
    seen_path = args.seen_file or os.path.join(
        os.path.dirname(os.path.abspath(args.out_dir)),
        "hotlists_seen.json",
    )
    seen = load_seen(seen_path)
    today = datetime.date.today()
    live_guids = set()

    baidu = fetch_baidu()
    baidu, seen = apply_first_seen(baidu, seen, today)
    live_guids.update(it.get("guid") for it in baidu)
    print(f"百度热搜: {len(baidu)} 条")
    write_rss(baidu, "百度热搜", os.path.join(args.out_dir, "baidu.xml"))

    toutiao = fetch_toutiao()
    toutiao, seen = apply_first_seen(toutiao, seen, today)
    live_guids.update(it.get("guid") for it in toutiao)
    print(f"头条热榜: {len(toutiao)} 条")
    write_rss(toutiao, "头条热榜", os.path.join(args.out_dir, "toutiao.xml"))

    tophub = fetch_tophub()
    for fname, (feed_name, items) in tophub.items():
        items, seen = apply_first_seen(items, seen, today)
        live_guids.update(it.get("guid") for it in items)
        print(f"今日热榜 {feed_name}: {len(items)} 条")
        write_rss(items, feed_name, os.path.join(args.out_dir, fname))

    seen = prune_seen(seen, live_guids, today)
    save_seen(seen_path, seen)
    print(f"首见日期已更新 {seen_path}（{len(seen)} 条）")


if __name__ == "__main__":
    main()
