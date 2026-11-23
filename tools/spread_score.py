#!/usr/bin/env python3
"""传播分 spread_score（2026-11-23 雷达迭代，用户「根据你的最优规划落地雷达迭代」）。

原雷达分（⭐️ x/10，模型五维评分，7.0 / 5.0 线）**不动**，本模块只新增一个并列的
「传播分」（满分 10），回答“这条传不传得开”，与原分回答的“论证深不深”互补。

四个维度（权重**事先固定**，未按实绩调参，避免对 216 条小样本过拟合）：
  person   0–3  有没有具体的人（个人处境，而非公司/资本/行业）
  hook     0–3  开头能否做到「有人 + 有数字」（标题里同时有人和数字最好）
  family   0–2  是否适合「转给家人看」（防骗、养老、看病、孩子、彩礼、物业……）
  audience 0–2  是否 40 岁以上男人关心（上有老下有小、在单位或生意里算过账吃过亏）
标题命中计满档，只在正文前 400 字命中计半档；标题是纯公司/资本且无人 → person 记 0。

确定性规则、零模型调用：同一输入同一输出；不改评分 prompt，原分 diff 恒为 0。
词表与 radar_v4（topic_type / share_family）共用，改一处须同步核对。
"""
import re

try:  # 作为 tools/ 下模块被 make_hotspots 导入
    from radar_v4 import CAPITAL_RX, DIGIT_RX, FAMILY_RX, PERSON_RX
except ImportError:  # pragma: no cover - 作为包导入（tests 里 sys.path 已含 tools/）
    from tools.radar_v4 import CAPITAL_RX, DIGIT_RX, FAMILY_RX, PERSON_RX

VERSION = "spread-v1"
MAX = 10.0

# 40+ 男人关心：钱袋子、饭碗、房子、孩子、老人、单位与生意里的账
AUDIENCE_RX = re.compile(
    r"(工资|收入|到手|降薪|欠薪|讨薪|裁员|被裁|失业|找工作|退休|养老|社保|医保|看病|住院|房贷|房子|买房|"
    r"首付|彩礼|孩子|上学|学费|父母|老人|赡养|遗产|单位|领导|体制|编制|考核|老板|生意|合伙|欠款|欠钱|"
    r"借钱|赔偿|赔了|罚款|合同|维权|起诉|被骗|骗局|物业|中年|离婚|保险|理赔|补偿|年终奖|加班)")


def _hit(rx, s):
    return bool(rx.search(s or ""))


def spread_parts(headline, body=""):
    """返回各维度分（dict）。headline=标题（可含 topic），body=正文/摘要前段。"""
    h = headline or ""
    b = (body or "")[:400]
    hp, bp = _hit(PERSON_RX, h), _hit(PERSON_RX, b)
    hd, bd = _hit(DIGIT_RX, h), _hit(DIGIT_RX, (body or "")[:200])
    cap_only = _hit(CAPITAL_RX, h) and not hp

    person = 3.0 if hp else (0.0 if cap_only else (1.5 if bp else 0.0))
    if hp and hd:
        hook = 3.0
    elif hp and bd:
        hook = 2.0
    elif hd or (bp and bd):
        hook = 1.0
    else:
        hook = 0.0
    family = 2.0 if _hit(FAMILY_RX, h) else (1.0 if _hit(FAMILY_RX, b) else 0.0)
    audience = 2.0 if _hit(AUDIENCE_RX, h) else (1.0 if _hit(AUDIENCE_RX, b) else 0.0)
    return {"person": person, "hook": hook, "family": family, "audience": audience}


def spread_score(headline, body=""):
    p = spread_parts(headline, body)
    return round(min(MAX, sum(p.values())), 1)


def spread_label(parts):
    """简报里一行可读说明，如「人3 开头3 家人0 40+2」。"""
    return (f"人{parts['person']:g} 开头{parts['hook']:g} 家人{parts['family']:g} "
            f"40+{parts['audience']:g}")


# ---------------- 接线：sets 结构化字段 + 简报展示 ----------------
SPREAD_MARK = "\U0001f4e3"  # 📣（不用 ⭐，避免被 make_hotspots 的 ⭐ 分数正则误读）
_SKIP_PREFIX = ("双评", "五维", "事件组", "**", "`", "\\`", "#", "-", "\\-", ">", "角度", "标签", "|")
_HEAD_RX = re.compile(r"^(###\s+\[(.+?)\]\((.+?)\).*?)(\s·\s\U0001f4e3 传播 [\d.]+/10（[^）]*）)?$", re.M)
_TOC_RX = re.compile(r"^(\d+\.\s\[(.+?)\]\(#item-[^)]+\)\s⭐\ufe0f?\s[\d.]+/10)(\s·\s\U0001f4e3 [\d.]+/10)?[ \t]*$", re.M)
LEGEND = ("传播分（📣，spread-v1，满分 10）= 有具体的人 3 + 开头有人有数 3 + 适合转给家人 2 + 40 岁以上男人关心 2；"
          "确定性规则，只并列展示，不参与 7.0/5.0 过滤与排序（2026-11-23 回测未达换主分门槛）。")


def summary_text(body):
    """从简报条目正文里取摘要段（跳过双评/五维/事件组/标签/代码块等结构行）。"""
    out = []
    for ln in (body or "").splitlines():
        s = ln.strip()
        if not s or s.startswith(_SKIP_PREFIX) or s.startswith("<"):
            continue
        out.append(s)
        if sum(len(x) for x in out) >= 400:
            break
    return " ".join(out)[:400]


def annotate_sets(sets, md_text):
    """原地给 sets 加 spread_score / spread_parts / spread_src；不碰任何已有键。"""
    from radar_v4 import brief_sections
    secs = brief_sections(md_text)
    for s in sets:
        headline, body = secs.get(s.get("source_url") or "", (s.get("topic", ""), ""))
        parts = spread_parts(headline + " " + (s.get("topic") or ""), summary_text(body))
        s["spread_score"] = round(min(MAX, sum(parts.values())), 1)
        s["spread_parts"] = parts
        s["spread_src"] = VERSION
    return sets


def annotate_brief_text(md):
    """在每个 ### 条目标题行和目录行末尾加「📣 传播 X/10（人3 开头3 家人0 40+2）」。幂等。"""
    from radar_v4 import brief_sections
    secs = brief_sections(md)
    by_title = {}
    for url, (headline, body) in secs.items():
        parts = spread_parts(headline, summary_text(body))
        by_title[headline] = (round(min(MAX, sum(parts.values())), 1), parts)

    def _head(m):
        sc, parts = by_title.get(m.group(2).strip(), (None, None))
        if sc is None:
            return m.group(0)
        return f"{m.group(1)} · {SPREAD_MARK} 传播 {sc:g}/10（{spread_label(parts)}）"

    def _toc(m):
        sc, _ = by_title.get(m.group(2).strip(), (None, None))
        if sc is None:
            return m.group(0)
        return f"{m.group(1)} · {SPREAD_MARK} {sc:g}/10"

    md = _HEAD_RX.sub(_head, md)
    return _TOC_RX.sub(_toc, md)


def annotate_brief_file(posts_dir, day):
    import os
    p = os.path.join(posts_dir, f"{day}-summary-zh.md")
    if not os.path.isfile(p):
        return None
    txt = open(p, encoding="utf-8").read()
    new = annotate_brief_text(txt)
    if new != txt:
        open(p, "w", encoding="utf-8").write(new)
    return p
