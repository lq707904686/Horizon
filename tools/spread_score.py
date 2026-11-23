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


# ---------------- v3 L13（2026-11-23）：分渠道传播分，只并列展示，不换主分 ----------------
# 依据 v3 2.2：同日对比里「人」项在公众号贴图几乎为 0（ρ=0.013），在视频号却是最强信号；
# 贴图里只有「标题带钱」「家人」站得住。所以分两个渠道各取两项，原 spread_score 一字不动。
#   spread_video = (person 0–3 + hook 0–3) / 6 × 10
#   spread_tietu = (money 0–3 + family 0–2) / 5 × 10
# 人物判定读 tools/person_lexicon.json（v3 L3，三仓一致）；spread_score 本身仍是 spread-v1 冻结口径。
CHANNEL_VERSION = "spread-ch-v1"
MONEY_RX = re.compile(
    r"(\d+(?:\.\d+)?\s*(?:多|余)?\s*(?:万|亿|千|百)?\s*(?:元|块|美元|欧元|英镑|日元|港元|港币|刀)|"
    r"[一二三四五六七八九十两百千万亿几]+\s*(?:万|亿)?\s*(?:元|块钱|块)|"
    r"工资|薪水|月薪|年薪|收入|存款|房贷|月供|彩礼|赔偿|赔了|罚款|欠薪|讨薪|欠款|退休金|养老金|补偿|违约金|"
    r"学费|房租|租金|花了|省了|亏了|赚了|多少钱|买单|钱)")

try:  # v3 L3 统一人物词表
    import person_lexicon as _pl
except Exception:  # noqa: BLE001
    try:
        from tools import person_lexicon as _pl
    except Exception:  # noqa: BLE001
        _pl = None


def _person(text):
    return _pl.has_person(text) if _pl is not None else _hit(PERSON_RX, text)


def channel_parts(headline, body=""):
    """分渠道各项分：{person, hook, money, family}（person/hook 用统一人物词表重算）。"""
    h = headline or ""
    b = (body or "")[:400]
    hp, bp = _person(h), _person(b)
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
    money = 3.0 if _hit(MONEY_RX, h) else (1.5 if _hit(MONEY_RX, (body or "")[:200]) else 0.0)
    family = 2.0 if _hit(FAMILY_RX, h) else (1.0 if _hit(FAMILY_RX, b) else 0.0)
    return {"person": person, "hook": hook, "money": money, "family": family}


def _body_only(body):
    """brief_sections 的 body 以标题行剩余部分（⭐/🎬/📣 分数）开头；分渠道分去掉这一截再算，
    否则「开头有数」恒为真、且重复标注会挤动前 200 字（spread-v1 保留原样不动）。"""
    b = body or ""
    return b.split("\n", 1)[1] if "\n" in b else ""


def channel_scores(headline, body=""):
    """返回 (spread_video, spread_tietu, parts)，两个分都是 0–10。"""
    p = channel_parts(headline, body)
    video = round((p["person"] + p["hook"]) / 6.0 * 10, 1)
    tietu = round((p["money"] + p["family"]) / 5.0 * 10, 1)
    return video, tietu, p


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
_HEAD_RX = re.compile(r"^(###\s+\[(.+?)\]\((.+?)\).*?)(\s·\s\U0001f4e3 传播 [\d.]+/10（[^）]*）"
                      r"(?:\s·\s分渠道 视频 [\d.]+ 贴图 [\d.]+)?)?$", re.M)
_TOC_RX = re.compile(r"^(\d+\.\s\[(.+?)\]\(#item-[^)]+\)\s⭐\ufe0f?\s[\d.]+/10)(\s·\s\U0001f4e3 [\d.]+/10)?[ \t]*$", re.M)
LEGEND = ("传播分（📣，spread-v1，满分 10）= 有具体的人 3 + 开头有人有数 3 + 适合转给家人 2 + 40 岁以上男人关心 2；"
          "确定性规则，只并列展示，不参与 7.0/5.0 过滤与排序（2026-11-23 回测未达换主分门槛）。"
          "分渠道（spread-ch-v1，v3 L13）：视频 = 人+开头，贴图 = 钱+家人，各折算满分 10，只并列展示。")


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
        try:  # v3 L13：分渠道并列键（失败不影响上面三个键）
            v, t, cp = channel_scores(headline + " " + (s.get("topic") or ""), summary_text(_body_only(body)))
            s["spread_video"] = v
            s["spread_tietu"] = t
            s["spread_channel"] = {"video": {"score": v, "person": cp["person"], "hook": cp["hook"]},
                                   "tietu": {"score": t, "money": cp["money"], "family": cp["family"]},
                                   "src": CHANNEL_VERSION}
        except Exception:  # noqa: BLE001
            pass
    return sets


def annotate_brief_text(md):
    """在每个 ### 条目标题行和目录行末尾加「📣 传播 X/10（人3 开头3 家人0 40+2）」。幂等。"""
    from radar_v4 import brief_sections
    secs = brief_sections(md)
    by_title = {}
    for url, (headline, body) in secs.items():
        parts = spread_parts(headline, summary_text(body))
        try:
            v, t, _cp = channel_scores(headline, summary_text(_body_only(body)))
        except Exception:  # noqa: BLE001
            v = t = None
        by_title[headline] = (round(min(MAX, sum(parts.values())), 1), parts, v, t)

    def _head(m):
        sc, parts, v, t = by_title.get(m.group(2).strip(), (None, None, None, None))
        if sc is None:
            return m.group(0)
        tail = f" · 分渠道 视频 {v:g} 贴图 {t:g}" if v is not None and t is not None else ""
        return f"{m.group(1)} · {SPREAD_MARK} 传播 {sc:g}/10（{spread_label(parts)}）{tail}"

    def _toc(m):
        sc = by_title.get(m.group(2).strip(), (None,))[0]
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
