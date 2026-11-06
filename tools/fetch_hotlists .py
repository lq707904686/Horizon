#!/usr/bin/env python3
"""抓取中文热榜（百度热搜、头条热榜、今日热榜 5 节点、NewsNow 试点 3 平台），生成本地 RSS 2.0 文件。

Horizon 通过 RSS 抓取器消费这些文件（workflow 里用本机 http.server
serving，源地址配成 http://127.0.0.1:8123/*.xml），零改 Horizon 内核。

注意：
- 每条目必须带 <pubDate>（24 小时内），否则 Horizon 的 RSS 抓取器
  会按日期过滤掉（_parse_date 返回 None 的条目直接跳过）。
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


# NewsNow 试点平台（2026-11-06 起跑一周看效果）。
# 公共 API：https://newsnow.busiyi.world/api/s?id=<平台>&latest=true，免登录。
# 选这 3 个的理由：澎湃（严肃社会议题）/牛客（求职裁员）/华尔街见闻（财经），
# 都是现有源没覆盖的域。36氪/虎嗅已在 NewsNow 下线；小红书 id 调不通（500）；
# 少数派已有自家 RSS（sspai.com/feed），热榜是重复信号，不进试点。
NEWSNOW_PLATFORMS = [
    # (平台ID, RSS文件名, 频道名)
    ("thepaper", "thepaper.xml", "澎湃热榜"),
    ("nowcoder", "nowcoder.xml", "牛客热榜"),
    ("wallstreetcn-news", "wallstreetcn.xml", "华尔街见闻"),
]


def fetch_newsnow(platform_id):
    """抓一个 NewsNow 平台热榜，返回 [(标题, 链接)]。

    status 为 "success"（实时）或 "cache"（缓存，快照一般在 1 小时内，
    对一天跑一次的雷达够用）都接受；缓存超过 24 小时视为过期数据，拒绝。
    """
    data = json.loads(
        _get(f"https://newsnow.busiyi.world/api/s?id={platform_id}&latest=true")
    )
    status = data.get("status")
    if status not in ("success", "cache"):
        raise RuntimeError(f"newsnow 返回异常状态: {status}")
    updated = data.get("updatedTime") or 0
    age_hours = (time.time() * 1000 - updated) / 3600000
    if age_hours > 24:
        raise RuntimeError(f"newsnow 缓存过期: {age_hours:.1f} 小时前")
    items = []
    for x in data.get("items", []):
        title = (x.get("title") or "").strip()
        url = (x.get("url") or "").strip()
        if title:
            items.append((title, url))
    return items


def fetch_newsnow_all():
    """NewsNow 试点平台 → {rss文件名: (频道名, items)}。"""
    out = {}
    for platform_id, fname, feed_name in NEWSNOW_PLATFORMS:
        try:
            raw = fetch_newsnow(platform_id)
            items = [
                {
                    "title": title,
                    "link": url,
                    "desc": f"{feed_name}：{title}",
                    "guid": f"newsnow-{platform_id}-{title}",
                }
                for title, url in raw
            ]
            out[fname] = (feed_name, items)
        except Exception as e:  # noqa: BLE001
            print(f"[warn] NewsNow {feed_name} 抓取失败: {e}", file=sys.stderr)
    return out


def write_rss(items, feed_title, path):
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
        parts += [
            "<item>",
            f"<title>{escape(it['title'])}</title>",
            f"<link>{escape(it['link'])}</link>",
            f"<description>{escape(it['desc'])}</description>",
            f"<guid isPermaLink=\"false\">{escape(it['guid'])}</guid>",
            f"<pubDate>{now}</pubDate>",
            "</item>",
        ]
    parts.append("</channel></rss>")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(parts))
    print(f"已写入 {path}（{len(items)} 条）")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", required=True, help="RSS 输出目录")
    args = ap.parse_args()
    os.makedirs(args.out_dir, exist_ok=True)

    baidu = fetch_baidu()
    print(f"百度热搜: {len(baidu)} 条")
    write_rss(baidu, "百度热搜", os.path.join(args.out_dir, "baidu.xml"))

    toutiao = fetch_toutiao()
    print(f"头条热榜: {len(toutiao)} 条")
    write_rss(toutiao, "头条热榜", os.path.join(args.out_dir, "toutiao.xml"))

    tophub = fetch_tophub()
    for fname, (feed_name, items) in tophub.items():
        print(f"今日热榜 {feed_name}: {len(items)} 条")
        write_rss(items, feed_name, os.path.join(args.out_dir, fname))

    newsnow = fetch_newsnow_all()
    for fname, (feed_name, items) in newsnow.items():
        print(f"NewsNow {feed_name}: {len(items)} 条")
        write_rss(items, feed_name, os.path.join(args.out_dir, fname))


if __name__ == "__main__":
    main()
