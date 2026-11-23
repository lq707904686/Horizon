#!/usr/bin/env python3
"""雷达 v4 迭代辅助（2026-11-23「先不动以前的内容，直接做所有的迭代落地」）。

只诊断、只打标，**不改任何分数**（7.0 / 5.0 线不动）：
  R-P0-2 诊断：近 30 天 ≥7.0 vs <7.0 条目的实际中位阅读（公众号）/播放（视频号），
         数据来自 data/feedback_actuals.jsonl（tools/backfill_reads.py 写）。
  R-P1-1 sets 加 topic_type（人/处境｜公司/资本｜其他）和 video_fit（高｜中｜低）：
         **确定性规则**打标（不改评分 prompt，保证分数 diff 为 0）；与方案原文“模型输出”不同，
         是有意的取舍，后续要换模型打标须另行评估对分数的影响。
  R-P1-2 同母题提示：候选的母题（简报「母题：X」）在近 7 天已发记录里出现 ≥3 次，
         或候选标题与近 7 天已发标题去数字相似 >0.6 的已有 ≥3 条 → repeat_hint。
"""
import difflib
import json
import os
import re
import statistics
from datetime import date, timedelta

MIN_SCORE = 7.0
DIAG_DAYS = 30
REPEAT_DAYS = 7
REPEAT_MIN = 3
SIM = 0.6
DIAG_MARKER = "<!-- v4-feedback-diag -->"
_NUM = set("0123456789一二三四五六七八九十两百千万亿.%．")

CAPITAL_RX = re.compile(r"(IPO|上市|股价|市值|估值|财报|融资|股权|期权|营收|净利|利润|亏损|并购|收购|"
                        r"基金|投资人|资本|美股|港股|A股|纳斯达克|市盈|股东|分红|裁员|季度|年报|"
                        r"Anthropic|OpenAI|英伟达|苹果|特斯拉|美团|京东|阿里|腾讯|字节|拼多多|小米|华为)", re.I)
PERSON_RX = re.compile(r"(\d+\s*岁|男子|女子|老人|大爷|大妈|阿姨|小伙|姑娘|夫妻|妻子|丈夫|父亲|母亲|儿子|女儿|"
                       r"孩子|家长|员工|职员|打工人|业主|租客|房东|主播|骑手|司机|医生|护士|教师|老师|副教授|"
                       r"学生|大学生|毕业生|村民|农民|店主|老板娘|保安|保姆|网友|用户称|消费者|顾客|患者|"
                       r"心腹|维权|离职|被裁|欠薪|起诉|挂名|富二代|病人|农民工|求职者|年轻人|中年人|打工|\d+\s*户|一家|全家)")
DIGIT_RX = re.compile(r"\d|[一二三四五六七八九十两百千万亿]{2,}")


def _strip(t):
    return "".join(c for c in (t or "") if c not in _NUM and not c.isspace()
                   and c not in "，。、：；！？,.:;!?“”\"'《》（）()「」—-")


def load_actuals(path):
    out = []
    if not path or not os.path.isfile(path):
        return out
    for ln in open(path, encoding="utf-8"):
        ln = ln.strip()
        if ln:
            try:
                out.append(json.loads(ln))
            except Exception:
                continue
    return out


def count_with_metrics(actuals):
    return sum(1 for r in actuals if r.get("reads") is not None or r.get("plays") is not None)


def _median(xs):
    return statistics.median(xs) if xs else None


def diagnose(actuals, today=None, days=DIAG_DAYS):
    today = today or date.today()
    lo = (today - timedelta(days=days)).isoformat()
    out = {"window_days": days, "as_of": today.isoformat(), "min_score": MIN_SCORE}
    for kind, metric, label in (("article", "reads", "公众号阅读"), ("video", "plays", "视频号播放")):
        rs = [r for r in actuals if r.get("kind") == kind and lo <= (r.get("date") or "") <= today.isoformat()
              and r.get("radar_score") is not None and r.get(metric) is not None]
        hi = [r[metric] for r in rs if r["radar_score"] >= MIN_SCORE]
        lw = [r[metric] for r in rs if r["radar_score"] < MIN_SCORE]
        out[kind] = {"metric": label, "n_ge7": len(hi), "median_ge7": _median(hi),
                     "n_lt7": len(lw), "median_lt7": _median(lw)}
    return out


def _fmt(x):
    if x is None:
        return "—"
    return str(int(x)) if float(x).is_integer() else f"{x:.1f}"


def diag_line(d):
    parts = []
    for kind in ("article", "video"):
        k = d[kind]
        parts.append(f"{k['metric']}中位 ≥7.0 {_fmt(k['median_ge7'])}（{k['n_ge7']} 条）vs <7.0 "
                     f"{_fmt(k['median_lt7'])}（{k['n_lt7']} 条）")
    return (f"实绩诊断（近 {d['window_days']} 天，截至 {d['as_of']}，只诊断不改分）：" + "；".join(parts))


# ---------------- 打标 ----------------
def topic_type(text, headline=None):
    """标题优先：标题里有具体的人 → 人/处境；标题里是公司/资本 → 公司/资本；
    标题判不出再看正文前 400 字；都没有 → 其他。"""
    h = headline if headline is not None else (text or "")[:40]
    if PERSON_RX.search(h):
        return "人/处境"
    if CAPITAL_RX.search(h):
        return "公司/资本"
    t = (text or "")[:400]
    if PERSON_RX.search(t):
        return "人/处境"
    if CAPITAL_RX.search(t):
        return "公司/资本"
    return "其他"


def video_fit(headline):
    h = headline or ""
    p, n = bool(PERSON_RX.search(h)), bool(DIGIT_RX.search(h))
    return "高" if (p and n) else ("中" if (p or n) else "低")


_HEADING = re.compile(r"^###\s+\[(.+?)\]\((.+?)\)", re.M)


def brief_sections(md_text):
    """{url: (headline, body)}；读简报原文。"""
    out = {}
    ms = list(_HEADING.finditer(md_text or ""))
    for i, m in enumerate(ms):
        end = ms[i + 1].start() if i + 1 < len(ms) else len(md_text)
        out[m.group(2).strip()] = (m.group(1).strip(), md_text[m.end():end])
    return out


def motifs_of(body):
    return sorted(set(re.findall(r"母题[：:]\s*([^；;，,\s（(]+)", body or "")))


def recent_published(actuals, feedback_log, today, days=REPEAT_DAYS):
    lo = (today - timedelta(days=days)).isoformat()
    titles = [r.get("title", "") for r in actuals if lo <= (r.get("date") or "") <= today.isoformat()]
    motifs = []
    for r in feedback_log:
        if lo <= (r.get("date") or "") <= today.isoformat():
            motifs += list(r.get("motifs") or [])
    return titles, motifs


def repeat_hint(headline, motifs, recent_titles, recent_motifs):
    hints = []
    for m in motifs:
        c = recent_motifs.count(m)
        if c >= REPEAT_MIN:
            hints.append(f"母题「{m}」近 {REPEAT_DAYS} 天已发 {c} 次")
    sh = _strip(headline)
    sims = [t for t in recent_titles if sh and _strip(t)
            and difflib.SequenceMatcher(None, sh, _strip(t)).ratio() > SIM]
    if len(sims) >= REPEAT_MIN:
        hints.append(f"与近 {REPEAT_DAYS} 天已发 {len(sims)} 条标题相似")
    return "；".join(hints) or None


def annotate_sets(sets, md_text, actuals=None, feedback_log=None, today=None):
    """原地给 sets 加 topic_type / video_fit / motifs / repeat_hint；不碰 score 等已有键。"""
    today = today or date.today()
    secs = brief_sections(md_text)
    rt, rm = recent_published(actuals or [], feedback_log or [], today)
    for s in sets:
        headline, body = secs.get(s.get("source_url") or "", (s.get("topic", ""), ""))
        text = headline + " " + (s.get("topic") or "") + " " + body[:400]
        mot = motifs_of(body)
        s["topic_type"] = topic_type(body[:400], headline=headline + " " + (s.get("topic") or ""))
        s["video_fit"] = video_fit(headline + " " + (s.get("topic") or ""))
        s["motifs"] = mot
        s["repeat_hint"] = repeat_hint(headline, mot, rt, rm)
        s["tag_src"] = "rule-v4"
    return sets


def append_brief_tail(posts_dir, day, lines):
    """把诊断行写到当日 summary-zh 尾部（幂等：已有标记则替换）。文件不存在返回 None。"""
    p = os.path.join(posts_dir, f"{day}-summary-zh.md")
    if not os.path.isfile(p):
        return None
    txt = open(p, encoding="utf-8").read()
    if DIAG_MARKER in txt:
        txt = txt[: txt.index(DIAG_MARKER)].rstrip() + "\n"
    txt = txt.rstrip() + "\n\n" + DIAG_MARKER + "\n\n---\n\n" + "\n\n".join("> " + x for x in lines) + "\n"
    open(p, "w", encoding="utf-8").write(txt)
    return p
