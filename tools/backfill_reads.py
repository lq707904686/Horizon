#!/usr/bin/env python3
"""R-P0-1 实绩回填（2026-11-23 四线迭代 v4）。

把公众号后台导出 + 视频号助手单篇导出的实绩，按标题（或 eid）对回长文仓条目和雷达分，
写进**新文件** data/feedback_actuals.jsonl（按 key 合并、带 as_of）。
**不改 data/feedback_log.jsonl 的任何已有行**（用户 11-23 令「先不动以前的内容」）。

输入（本地跑，不进 workflow）：
  --registry   长文仓 _published_registry.json（type/title/日期/entry_id）
  --wx-csv     公众号「发表记录」导出：序号,日期,时间,类型,标题,阅读,点赞,分享,在看,留言,状态,…
  --wx-ch-csv  公众号「单篇渠道阅读」：ch,date(YYYYMMDD),title,n,pct（取 ch=推荐 作 rec；无行 = 未知，不写 0）
  --sph-csv    视频号「单篇视频表」：视频描述,视频ID,发布时间,完播率,平均播放时长,播放量,推荐,喜欢,评论量,分享量,关注量,…
  --changwen   长文仓根目录（条目标题索引 + 各轮 _ID台账.json 的 score = 选题时的雷达分）
雷达分优先取条目台账 score（radar_score_src=台账）；对不上条目时再用 gh-pages 简报标题相似 ≥0.6
（radar_score_src=简报，取发布前 14 天内最相似一条）。对不上的写 unmatched 列表，不丢。

v3 L12（2026-11-23）统一「发布后 48 小时」口径（只对新写入的行；旧行一字不动）：
  - 新行带 window="发布后48小时"、obs_lag_days（as_of 日期 − 发布日期）、unstable、as_of；
  - unstable=true：发布日期落在 as_of 的最后 2 天内（as_of 当天与前一天）——还没到 48 小时，数会涨；
  - 合并规则：旧行（无 window 字段）不覆盖、不改；新口径行一旦稳定（unstable=false）即冻结，
    之后的导出不再覆盖（导出是累计值，冻结最早一次稳定观测最接近 48 小时口径）；
    未稳行可被更新的 as_of 覆盖。obs_lag_days 越大，离 48 小时口径越远，分析时可据此筛。
  - 诊断（radar_v4.diagnose）与回测（--exclude-unstable）剔除 unstable=true 的行。

用法：
  python tools/backfill_reads.py --as-of 2026-11-22T22:19            # 默认路径，见 DEFAULTS
  python tools/backfill_reads.py --dry-run                           # 只打印统计
"""
import argparse
import csv
import difflib
import glob
import json
import os
import re
import subprocess
import sys
from datetime import date, timedelta

HOME = os.path.expanduser("~")
GOAL_HIDDEN = os.path.join(HOME, "workspace/goals/wechat-account-content-system/hidden_files")
DEFAULTS = {
    "registry": os.path.join(HOME, "workspace/changwen-review2/_published_registry.json"),
    "changwen": os.path.join(HOME, "workspace/changwen-review2"),
    "wx_csv": os.path.join(GOAL_HIDDEN, "后台_发表记录全量_批4.csv"),
    "wx_ch_csv": os.path.join(GOAL_HIDDEN, "后台_单篇渠道阅读.csv"),
    "sph_csv": os.path.join(GOAL_HIDDEN, "视频号_单篇视频表_批4.csv"),
    "out": "data/feedback_actuals.jsonl",
}
SIM = 0.6
RADAR_LOOKBACK_DAYS = 14
WINDOW_LABEL = "发布后48小时"
UNSTABLE_DAYS = 2


def stamp_window(rec, as_of):
    """L12：给新行打 48 小时口径字段。日期解析失败时 unstable=None（未知），不猜。"""
    rec["window"] = WINDOW_LABEL
    try:
        a = date.fromisoformat(str(as_of)[:10])
        d = date.fromisoformat(str(rec.get("date") or "")[:10])
    except Exception:
        rec["obs_lag_days"] = None
        rec["unstable"] = None
        return rec
    lag = (a - d).days
    rec["obs_lag_days"] = lag
    rec["unstable"] = lag < UNSTABLE_DAYS
    return rec
_NUM = set("0123456789一二三四五六七八九十两百千万亿.%．")
BRIEF_RX = re.compile(r"^(\d+)\. \[(.+?)\]\(#item-renxing-\d+\) ⭐️ ([\d.]+)/10", re.M)


def strip_num(t):
    return "".join(c for c in (t or "") if c not in _NUM and not c.isspace()
                   and c not in "，。、：；！？,.:;!?“”\"'《》（）()「」—-").strip()


def sim(a, b):
    a, b = strip_num(a), strip_num(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def _lj(p):
    try:
        with open(p, encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return None


def _int(x):
    try:
        return int(str(x).replace(",", "").strip())
    except Exception:
        return None


def _pct(x):
    try:
        return float(str(x).strip().rstrip("%"))
    except Exception:
        return None


def _float(x):
    try:
        return float(str(x).strip())
    except Exception:
        return None


def _sec(x):
    try:
        return float(str(x).strip().rstrip("秒"))
    except Exception:
        return None


# ---------------- 长文仓条目索引 ----------------
def entry_index(changwen):
    """返回 [{eid, round, score, titles:[...]}]。"""
    out = []
    for rd in sorted(glob.glob(os.path.join(changwen, "第*轮*"))):
        m = re.match(r"第(\d+)轮", os.path.basename(rd))
        if not m:
            continue
        led = _lj(os.path.join(rd, "_ID台账.json")) or {}
        lmap = {str(e.get("id")): e for e in (led.get("entries") or [])} if isinstance(led, dict) else {}
        for ed in sorted(glob.glob(os.path.join(rd, "条目", "*"))):
            eid = os.path.basename(ed)
            titles = []
            for name in ("03_标题与标签.json", "35_标题标签.json"):
                d = _lj(os.path.join(ed, name)) or {}
                tt = d.get("titles") if isinstance(d, dict) else None
                if isinstance(tt, dict):
                    titles += [tt.get("main")] + list(tt.get("alt") or []) + [tt.get("published")]
            for name in ("36_金句爆款图文.json",):
                d = _lj(os.path.join(ed, name)) or {}
                for c in (d.get("cards") or []) if isinstance(d, dict) else []:
                    if isinstance(c, dict):
                        titles.append(c.get("viral_title"))
            for bf in ("02_成品正文.md", "20_成文.md"):
                p = os.path.join(ed, bf)
                if os.path.isfile(p):
                    for ln in open(p, encoding="utf-8", errors="ignore"):
                        ln = ln.strip().lstrip("#").strip()
                        if ln:
                            titles.append(ln)
                            break
                    break
            le = lmap.get(eid, {})
            if le.get("title"):
                titles.append(le["title"])
            titles = [t for t in titles if isinstance(t, str) and t.strip()]
            if titles:
                out.append({"eid": eid, "round": m.group(1), "score": le.get("score"), "titles": titles})
    return out


def match_entry(title, idx):
    best, bs = None, 0.0
    for e in idx:
        for t in e["titles"]:
            s = sim(title, t)
            if s > bs:
                best, bs = e, s
    return (best, round(bs, 3)) if bs >= SIM else (None, round(bs, 3))


# ---------------- 雷达简报 ----------------
def brief_items(repo="."):
    try:
        ls = subprocess.run(["git", "-C", repo, "ls-tree", "-r", "--name-only", "origin/gh-pages"],
                            capture_output=True, text=True, timeout=60).stdout.split()
    except Exception:
        return []
    items = []
    for p in ls:
        m = re.match(r"_posts/(\d{4}-\d{2}-\d{2})-summary-zh\.md$", p)
        if not m:
            continue
        txt = subprocess.run(["git", "-C", repo, "show", "origin/gh-pages:" + p],
                             capture_output=True).stdout.decode("utf-8", "ignore")
        for mm in BRIEF_RX.finditer(txt):
            items.append({"date": m.group(1), "title": mm.group(2), "score": float(mm.group(3))})
    return items


def match_brief(title, pub_date, items):
    try:
        d = date.fromisoformat(pub_date)
    except Exception:
        return None
    lo = (d - timedelta(days=RADAR_LOOKBACK_DAYS)).isoformat()
    best, bs = None, 0.0
    for it in items:
        if lo <= it["date"] <= pub_date:
            s = sim(title, it["title"])
            if s > bs:
                best, bs = it, s
    if best and bs >= SIM:
        return dict(best, sim=round(bs, 3))
    return None


def _radar(rec, entry, title, pub_date, items):
    sc = _float(entry.get("score")) if entry else None
    if sc is not None:
        rec.update(radar_score=sc, radar_score_src="台账")
        return
    b = match_brief(title, pub_date, items)
    if b:
        rec.update(radar_score=b["score"], radar_score_src="简报", radar_date=b["date"],
                   radar_title=b["title"], radar_sim=b["sim"])
    else:
        rec.setdefault("unmatched", []).append("radar_score")


# ---------------- 公众号 / 视频号 ----------------
def wx_records(wx_csv, ch_csv, registry, idx, items, as_of):
    rec_map = {}
    if ch_csv and os.path.isfile(ch_csv):
        for r in csv.DictReader(open(ch_csv, encoding="utf-8-sig")):
            if r.get("ch") == "推荐":
                d = r.get("date", "")
                d = f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 else d
                rec_map[(d, r.get("title", ""))] = _int(r.get("n"))
    reg = (registry or {}).get("entries", [])
    out = []
    for r in csv.DictReader(open(wx_csv, encoding="utf-8-sig")):
        if r.get("状态") and r["状态"] != "已发表":
            continue
        d, title, typ = r["日期"], r["标题"], r["类型"]
        rec = {"key": f"wx:{d}:{typ}:{title}", "kind": "article", "platform": "公众号",
               "date": d, "time": r.get("时间") or None, "type": typ, "title": title,
               "reads": _int(r.get("阅读")), "likes": _int(r.get("点赞")), "shares": _int(r.get("分享")),
               "wow": _int(r.get("在看")), "comments": _int(r.get("留言")),
               "rec": rec_map.get((d, title)), "as_of": as_of, "source_file": os.path.basename(wx_csv)}
        eid = ""
        for e in reg:
            if e.get("title") == title and e.get("published_date") == d and e.get("entry_id"):
                eid = str(e["entry_id"])
                break
        entry = None
        if eid:
            entry = next((x for x in idx if x["eid"] == eid), None)
            rec.update(eid=eid, eid_src="登记册", eid_sim=1.0)
        else:
            entry, s = match_entry(title, idx)
            if entry:
                rec.update(eid=entry["eid"], eid_src="标题相似", eid_sim=s)
            else:
                rec.setdefault("unmatched", []).append("eid")
        if entry:
            rec["round"] = entry["round"]
        _radar(rec, entry, title, d, items)
        out.append(stamp_window(rec, as_of))
    return out


def sph_records(sph_csv, idx, items, as_of):
    out = []
    for r in csv.DictReader(open(sph_csv, encoding="utf-8-sig")):
        desc = (r.get("视频描述") or "").strip()
        title = desc.splitlines()[0].strip() if desc else ""
        d = (r.get("发布时间") or "").strip().replace("/", "-")
        try:
            d = date.fromisoformat("-".join(x.zfill(2) for x in d.split("-"))).isoformat()
        except Exception:
            pass
        vid = (r.get("视频ID") or "").strip()
        rec = {"key": f"sph:{vid or d + ':' + title}", "kind": "video", "platform": "视频号",
               "date": d, "title": title, "video_id": vid,
               "plays": _int(r.get("播放量")), "completion_pct": _pct(r.get("完播率")),
               "avg_watch_s": _sec(r.get("平均播放时长")), "followers": _int(r.get("关注量")),
               "likes": _int(r.get("喜欢")), "recommends": _int(r.get("推荐")),
               "comments": _int(r.get("评论量")), "shares": _int(r.get("分享量")),
               "as_of": as_of, "source_file": os.path.basename(sph_csv)}
        entry, s = match_entry(title, idx)
        if entry:
            rec.update(eid=entry["eid"], eid_src="标题相似", eid_sim=s, round=entry["round"])
        else:
            rec.setdefault("unmatched", []).append("eid")
        _radar(rec, entry, title, d, items)
        out.append(stamp_window(rec, as_of))
    return out


def merge(path, new):
    """按 key 合并（L12）：旧行（无 window 字段）原样保留不覆盖；新口径行已稳定则冻结；
    未稳行取 as_of 较新的一条；文件里其他 key 原样保留。返回 (rows, added, updated)。"""
    old = {}
    if os.path.isfile(path):
        for ln in open(path, encoding="utf-8"):
            ln = ln.strip()
            if ln:
                try:
                    r = json.loads(ln)
                    old[r["key"]] = r
                except Exception:
                    continue
    added = updated = 0
    for r in new:
        o = old.get(r["key"])
        if o is None:
            added += 1
            old[r["key"]] = r
        elif "window" not in o:
            continue  # L12：旧行不动
        elif o.get("unstable") is False:
            continue  # L12：已稳定的 48 小时口径行冻结
        elif (r.get("as_of") or "") >= (o.get("as_of") or ""):
            updated += 1
            old[r["key"]] = r
    rows = sorted(old.values(), key=lambda r: (r.get("date", ""), r.get("time") or "", r["key"]))
    return rows, added, updated


def stats(rows, lo="2026-10-21", hi="2026-11-22"):
    wx = [r for r in rows if r["kind"] == "article" and lo <= r["date"] <= hi]
    v = [r for r in rows if r["kind"] == "video"]
    f = lambda xs, k: sum(1 for r in xs if r.get(k) is not None and r.get(k) != "")
    return {"wx_window": len(wx), "wx_reads": f(wx, "reads"), "wx_eid": f(wx, "eid"),
            "wx_radar": f(wx, "radar_score"), "video": len(v), "video_plays": f(v, "plays"),
            "video_eid": f(v, "eid"), "video_radar": f(v, "radar_score")}


def main(argv=None):
    ap = argparse.ArgumentParser(description="R-P0-1 实绩回填 → data/feedback_actuals.jsonl")
    for k, v in DEFAULTS.items():
        ap.add_argument("--" + k.replace("_", "-"), default=v)
    ap.add_argument("--as-of", default=date.today().isoformat())
    ap.add_argument("--repo", default=".", help="雷达仓（读 origin/gh-pages 简报）")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    idx = entry_index(a.changwen)
    items = brief_items(a.repo)
    registry = _lj(a.registry) or {}
    new = []
    if os.path.isfile(a.wx_csv):
        new += wx_records(a.wx_csv, a.wx_ch_csv, registry, idx, items, a.as_of)
    if os.path.isfile(a.sph_csv):
        new += sph_records(a.sph_csv, idx, items, a.as_of)
    rows, added, updated = merge(a.out, new)
    st = stats(rows)
    print(f"条目索引 {len(idx)} 个、简报条目 {len(items)} 条；本次 {len(new)} 条（新增 {added}、更新 {updated}）")
    print("10-21～11-22 公众号 {wx_window} 条：阅读 {wx_reads}、对上条目 {wx_eid}、带雷达分 {wx_radar}；"
          "视频 {video} 条：播放 {video_plays}、对上条目 {video_eid}、带雷达分 {video_radar}".format(**st))
    if not a.dry_run:
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
        with open(a.out, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(f"已写 {a.out}（{len(rows)} 行）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
