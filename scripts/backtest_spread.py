#!/usr/bin/env python3
"""传播分回测（2026-11-23）：新分 spread_score vs 原雷达分 radar_score 对实绩的预测力。

数据：data/feedback_actuals.jsonl（公众号后台 + 视频号助手官方导出回填，216 行）。
口径：
  - 只用**已发布标题**算传播分（实绩行没有候选摘要；正文不读，避免与原分输入不对等）。
  - 原分取 radar_score；< 5.0 的是 10-26 洞见轮另一套分制，剔除。
  - 分组：视频号播放（全期）；公众号贴图阅读、文章阅读，各按推荐期（≤10-28）/归零后（≥10-29）分开报。
    推荐 10-29 起整号归零，归零后阅读多为 0–5，基本是噪声，只列不做判据。
  - 指标：Spearman ρ（平均秩处理并列）；配对 bootstrap 2000 次的 Δρ=ρ新−ρ旧 95% 区间；
    高低档中位数（新分 ≥6 vs <6；原分 ≥7 vs <7）。
  - 权重事先固定、不调参；判据：视频号播放 / 推荐期贴图阅读 至少一个 Δρ 区间下界 >0，
    且另一个 Δρ 点估计不 <0，才换主分。否则只并列展示。
用法：python scripts/backtest_spread.py [--out data/backtest_spread_20261123.json]
"""
import argparse
import json
import os
import random
import statistics
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import spread_score as sp  # noqa: E402

CUT_NEW, CUT_OLD = 6.0, 7.0
SPLIT = "2026-10-29"


def ranks(xs):
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    r = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        for k in range(i, j + 1):
            r[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return r


def spearman(x, y):
    if len(x) < 3:
        return None
    rx, ry = ranks(x), ranks(y)
    mx, my = statistics.mean(rx), statistics.mean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = (sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry)) ** 0.5
    return None if den == 0 else round(num / den, 3)


def med(xs):
    return statistics.median(xs) if xs else None


def boot_delta(rows, n=2000, seed=20261123):
    rnd = random.Random(seed)
    ds = []
    for _ in range(n):
        s = [rows[rnd.randrange(len(rows))] for _ in rows]
        a = spearman([r["new"] for r in s], [r["y"] for r in s])
        b = spearman([r["old"] for r in s], [r["y"] for r in s])
        if a is not None and b is not None:
            ds.append(a - b)
    ds.sort()
    if not ds:
        return None
    return [round(ds[int(0.025 * len(ds))], 3), round(ds[int(0.975 * len(ds)) - 1], 3)]


def group_stats(name, rows):
    both = [r for r in rows if r["old"] is not None]
    out = {"group": name, "n_all": len(rows), "n_both": len(both),
           "rho_new_all": spearman([r["new"] for r in rows], [r["y"] for r in rows]),
           "rho_new": spearman([r["new"] for r in both], [r["y"] for r in both]),
           "rho_old": spearman([r["old"] for r in both], [r["y"] for r in both])}
    out["delta"] = (None if out["rho_new"] is None or out["rho_old"] is None
                    else round(out["rho_new"] - out["rho_old"], 3))
    out["delta_ci95"] = boot_delta(both) if len(both) >= 8 else None
    hi = [r["y"] for r in rows if r["new"] >= CUT_NEW]
    lo = [r["y"] for r in rows if r["new"] < CUT_NEW]
    out["new_ge6"] = {"n": len(hi), "median": med(hi)}
    out["new_lt6"] = {"n": len(lo), "median": med(lo)}
    hi = [r["y"] for r in both if r["old"] >= CUT_OLD]
    lo = [r["y"] for r in both if r["old"] < CUT_OLD]
    out["old_ge7"] = {"n": len(hi), "median": med(hi)}
    out["old_lt7"] = {"n": len(lo), "median": med(lo)}
    return out


def load(path):
    rows = []
    for ln in open(path, encoding="utf-8"):
        ln = ln.strip()
        if not ln:
            continue
        r = json.loads(ln)
        y = r.get("plays") if r.get("kind") == "video" else r.get("reads")
        if y is None:
            continue
        old = r.get("radar_score")
        if old is not None and old < 5.0:
            old = None
        rows.append({"date": r.get("date") or "", "type": r.get("type") or "视频",
                     "kind": r.get("kind"), "title": r.get("title") or "",
                     "y": y, "old": old, "new": sp.spread_score(r.get("title") or "")})
    return rows


def decide(res):
    v = next(g for g in res if g["group"] == "视频号播放·全期")
    t = next(g for g in res if g["group"] == "公众号贴图阅读·推荐期")
    def sig(g):
        return bool(g["delta_ci95"]) and g["delta_ci95"][0] > 0
    def not_worse(g):
        return g["delta"] is not None and g["delta"] >= 0
    switch = (sig(v) and not_worse(t)) or (sig(t) and not_worse(v))
    return {"switch_main": switch,
            "rule": "视频号播放/推荐期贴图阅读 至少一个 Δρ 95% 下界>0，且另一个 Δρ≥0",
            "video_sig": sig(v), "tietu_sig": sig(t)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--actuals", default=os.path.join(ROOT, "data/feedback_actuals.jsonl"))
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    rows = load(a.actuals)
    groups = [
        ("视频号播放·全期", [r for r in rows if r["kind"] == "video"]),
        ("公众号贴图阅读·推荐期", [r for r in rows if r["type"] == "贴图" and r["date"] < SPLIT]),
        ("公众号文章阅读·推荐期", [r for r in rows if r["type"] == "文章" and r["date"] < SPLIT]),
        ("公众号贴图阅读·归零后", [r for r in rows if r["type"] == "贴图" and r["date"] >= SPLIT]),
        ("公众号文章阅读·归零后", [r for r in rows if r["type"] == "文章" and r["date"] >= SPLIT]),
    ]
    res = [group_stats(n, g) for n, g in groups]
    out = {"version": sp.VERSION, "as_of_actuals": "2026-11-22T22:19", "split": SPLIT,
           "cut_new": CUT_NEW, "cut_old": CUT_OLD, "groups": res, "decision": decide(res)}
    txt = json.dumps(out, ensure_ascii=False, indent=2)
    print(txt)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(txt + "\n")


if __name__ == "__main__":
    main()
