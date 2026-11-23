#!/usr/bin/env python3
"""L8 简报版本守护（v3 落地，2026-11-23 用户「L1–L16 全按建议落地」）。

问题：同一个北京时间日期，雷达可能跑多次（07:00 定时、watchdog 13:00/18:00 补跑、手动），
每次都把 docs/_posts/<日>-summary-zh.md 整个重写，gh-pages（keep_files）上同名文件被覆盖；
11-23 就有两版（02:49 版 ≥7.0 0 条 → 13:54 版 10 条）。反过来也可能：好简报被更差的补跑覆盖。

只在 Python 脚本层（make_hotspots 内）实现，**不改 workflow**：
  1. 取当天已发布版：git fetch gh-pages（checkout 已带凭据）→ 失败再试 raw.githubusercontent（公开仓）；
     都拿不到就当“当天第一版”，行为与改前一致。
  2. 判据（事先写死）：本次 **≥7.0 条数更少，且最高分不更高** → “更差”，不覆盖——
     把已发布版原文写回 docs/_posts（部署后内容不变，只更新底部版本行），
     hotspots.json / score_distribution 也按已发布版生成，三者一致。
     其余情况（更多、相等、最高分更高）一律允许覆盖并记录（11-23 的 0→10 条即允许覆盖）。
  3. 简报底部写版本行：vN（第几个被发布的版本）/ 当天第几次生成 / 覆盖过几次 / 拦下几次 /
     本版 ≥7.0 条数与最高分 / 代码提交；机读状态在 HTML 注释里，下一次从已发布版底部读回。
  4. score_distribution.json 当日记录按“当天最终版”写；顶层 brief_runs[日期] 留每次判定日志。
任何异常由调用方捕获只打警告，主流程照旧。
"""
import json
import os
import re
import subprocess
import tempfile
import urllib.request

VERSION = "brief-guard-v1"
FOOTER_MARK = "<!-- brief-version "
_FOOTER_RX = re.compile(r"\n*<!-- brief-version (\{.*?\}) -->\n(?:\n?> 简报版本：[^\n]*\n?)?", re.S)
DEFAULT_REPO = "lq707904686/Horizon"


def _run(cmd, cwd, timeout=60):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, timeout=timeout)


def fetch_published(day, repo_dir, *, allow_http=True):
    """返回 (已发布简报文本|None, 来源说明)。"""
    rel = f"_posts/{day}-summary-zh.md"
    try:
        f = _run(["git", "fetch", "--no-tags", "--depth=1", "origin",
                  "+refs/heads/gh-pages:refs/remotes/origin/gh-pages"], repo_dir, timeout=90)
        if f.returncode == 0:
            s = _run(["git", "show", f"origin/gh-pages:{rel}"], repo_dir)
            if s.returncode == 0:
                return s.stdout.decode("utf-8", "ignore"), "git:gh-pages"
            return None, "git:gh-pages（当天无已发布版）"
    except Exception:  # noqa: BLE001
        pass
    if not allow_http:
        return None, "未取到（git 失败，未试 http）"
    repo = os.environ.get("GITHUB_REPOSITORY") or DEFAULT_REPO
    url = f"https://raw.githubusercontent.com/{repo}/gh-pages/{rel}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "renxing-radar"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.read().decode("utf-8", "ignore"), "http:raw"
    except Exception as exc:  # noqa: BLE001 - 404 = 当天无已发布版
        return None, f"http:raw 未取到（{type(exc).__name__}）"


def read_state(text):
    """从已发布版底部读机读状态；没有底部行（改前的老简报）时返回 None。"""
    if not text:
        return None
    m = None
    for m in _FOOTER_RX.finditer(text):
        pass
    if not m:
        return None
    try:
        st = json.loads(m.group(1))
        return st if isinstance(st, dict) else None
    except Exception:  # noqa: BLE001
        return None


def strip_footer(text):
    return _FOOTER_RX.sub("\n", text or "").rstrip() + "\n"


def stats_of(text, extract_fn):
    """(带分条目数, ≥7.0 条数, 最高分)；extract_fn = make_hotspots.extract_scored_topics。"""
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as tf:
        tf.write(text or "")
        path = tf.name
    try:
        scores = [x[0] for x in extract_fn(path) if x[0] is not None]
    finally:
        os.unlink(path)
    return {"items": len(scores), "ge7": sum(1 for s in scores if s >= 7.0),
            "max": max(scores) if scores else None}


def is_worse(new, pub):
    """判据：≥7.0 条数更少，且最高分不更高 → 更差（不覆盖）。已发布版无分数时永不算更差。"""
    if not pub or not pub.get("items"):
        return False
    nmax = new.get("max") if new.get("max") is not None else -1
    pmax = pub.get("max") if pub.get("max") is not None else -1
    return new.get("ge7", 0) < pub.get("ge7", 0) and nmax <= pmax


def decide(new_stats, pub_text, extract_fn):
    """返回 {action: first|overwrite|keep, gen, overwrites, kept, version, new, pub}。"""
    if not pub_text:
        return {"action": "first", "gen": 1, "overwrites": 0, "kept": 0, "version": 1,
                "new": new_stats, "pub": None}
    st = read_state(pub_text) or {}
    pub_stats = stats_of(pub_text, extract_fn)
    gen = int(st.get("gen", 1)) + 1           # 老简报无底部行：视为已有 1 次生成
    overwrites = int(st.get("overwrites", 0))
    kept = int(st.get("kept", 0))
    version = int(st.get("version", 1))
    if is_worse(new_stats, pub_stats):
        kept += 1
        action = "keep"
    else:
        overwrites += 1
        version += 1
        action = "overwrite"
    return {"action": action, "gen": gen, "overwrites": overwrites, "kept": kept,
            "version": version, "new": new_stats, "pub": pub_stats}


def final_stats(d):
    return (d["pub"] if d["action"] == "keep" else d["new"]) or {}


def _fmt(x):
    if x is None:
        return "—"
    return f"{x:g}" if isinstance(x, (int, float)) else str(x)


def footer_text(day, d, commit=None):
    fs = final_stats(d)
    state = {"v": VERSION, "day": day, "gen": d["gen"], "overwrites": d["overwrites"],
             "kept": d["kept"], "version": d["version"], "last_action": d["action"]}
    note = {"first": "当天首版", "overwrite": "本次生成覆盖了上一版",
            "keep": "本次生成更差（≥7.0 更少且最高分不更高），未覆盖，保留上一版"}[d["action"]]
    line = (f"> 简报版本：{day}·v{d['version']}｜当天第 {d['gen']} 次生成｜已覆盖 {d['overwrites']} 次、"
            f"拦下 {d['kept']} 次｜{note}｜本版 ≥7.0 {fs.get('ge7', 0)} 条、最高 {_fmt(fs.get('max'))}"
            + (f"｜代码 {commit[:7]}" if commit else ""))
    return f"\n\n{FOOTER_MARK}{json.dumps(state, ensure_ascii=False)} -->\n\n{line}\n"


def write_footer(post_path, day, d, commit=None):
    if not post_path or not os.path.isfile(post_path):
        return None
    with open(post_path, encoding="utf-8") as f:
        txt = f.read()
    txt = strip_footer(txt).rstrip() + footer_text(day, d, commit)
    with open(post_path, "w", encoding="utf-8") as f:
        f.write(txt)
    return post_path


def log_run(dist_path, day, d, source, at_iso):
    """score_distribution.json 顶层 brief_runs[day]：每次生成的判定日志（只加键）。"""
    try:
        with open(dist_path, encoding="utf-8") as f:
            hist = json.load(f)
        if not isinstance(hist, dict):
            return
    except Exception:  # noqa: BLE001
        return
    fs = final_stats(d)
    runs = hist.setdefault("brief_runs", {})
    day_rec = runs.setdefault(day, {"log": []})
    day_rec.update(generations=d["gen"], overwrites=d["overwrites"], kept=d["kept"],
                   version=d["version"], final_ge7=fs.get("ge7"), final_max=fs.get("max"),
                   rule="≥7.0 条数更少且最高分不更高 → 不覆盖")
    day_rec.setdefault("log", []).append({"at": at_iso, "action": d["action"], "new": d["new"],
                                          "pub": d["pub"], "published_source": source})
    for k in sorted(runs)[:-30]:
        runs.pop(k, None)
    with open(dist_path, "w", encoding="utf-8") as f:
        json.dump(hist, f, ensure_ascii=False, indent=2)
        f.write("\n")
