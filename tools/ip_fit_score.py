#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""IP 契合度 ip_fit（2026-11-25，G1：用户令「百万数据必须建立在我们的 IP 上」）。

移植自 动态监测/ip_fit.py（ip-fit-v1 打分规则，score() 逐字照搬，已由测试做逐条对拍）：
标题级规则给选题打 高/中/低/无。词表：人物 tools/person_lexicon.json（三仓统一 person-lexicon-v1，
sha bde6b72c…）、栏目 tools/ip_fit_column_lex.json（长文仓标签词表 column_lex 拷贝，sha d06e2246…）。

接线（make_hotspots.py，传播分之后）：sets 加 ip_fit / ip_fit_score / ip_fit_hits / ip_fit_src；
当日简报每条 ### 标题行下方插一行「> 🧭 IP 契合 X（命中…）」与传播分并列展示。
**只并列展示，不改主分、不参与 7.0/5.0 过滤与排序**；失败只告警。
等级「无」在简报里标红提示（🔴）。冻结 holdout 100 条：二分类（高+中 vs 低+无）准确率 84%（≥80% 才合入）。
注意：展示行以 ">" 开头且不含「母题：」，不会被 spread_score.summary_text / radar_v4.motifs_of 读进去。
"""
import json, os, re

HERE = os.path.dirname(os.path.abspath(__file__))
VERSION = "ip-fit-v1"

_lex = json.load(open(os.path.join(HERE, "person_lexicon.json"), encoding="utf-8"))
_terms = sorted(_lex["terms"], key=len, reverse=True)
# 去掉过宽的单字指代（他/她/姐/哥/弟/妹/爸/妈）以免「哥伦比亚」「他山之石」误命中；单字只在带量词/称谓时由下方补丁捕获
_LOOSE = {"他", "她", "姐", "哥", "弟", "妹", "爸", "妈", "销售", "行政", "老板", "一家"}
PERSON_RX = re.compile("(" + "|".join([re.escape(t) for t in _terms if t not in _LOOSE] + _lex["patterns"]
                       + [r"(?:他|她)(?:们)?(?:跑|干|做|说|被|把|在|用|花|赚|亏|靠|只|才|却|也)",
                          r"(?:我|我们)(?:发现|见过|扒了|算了|问了|跟|和|在|被)", r"一家(?=人|子|老小|三口|四口|五口)|全家|车主|少年|少女|博主|摊主|店长|HR|老板娘|小老板|夫妻店|打工人|年轻人|中年人|中年|00后|90后|80后|宝妈|上班族|普通人|穷人|富人|有钱人|老实人|新人|下属|同事|领导|上司|合伙人|员工|股东们"])
                       + ")")
_EXCL = re.compile("|".join(re.escape(x) for x in sorted(_lex["exclude"], key=len, reverse=True)))
_col = json.load(open(os.path.join(HERE, "ip_fit_column_lex.json"), encoding="utf-8"))
COLUMN_LEX = _col["column_lex"]
COLUMNS = ["看职场", "带队伍", "做生意", "做销售", "去创业", "过日子"]

MOTIF_RX = re.compile(r"(人性|人情|面子|圈子|圈层|信任|背叛|翻脸|画饼|甩锅|背锅|站队|潜规则|割韭菜|收割|被割|坑|套路|骗|"
                      r"止损|沉没成本|贪|嫉妒|眼红|既得利益|分钱|分账|散伙|合伙|算计|心机|吃亏|占便宜|讨好|巴结|势利|"
                      r"人走茶凉|欠的|欠钱|借钱|还钱|欠薪|讨薪|赖账|跑路|翻车|穷|富|阶层|体面|尊严|老实人|关系户|拿捏|PUA|"
                      r"背后|真相|代价|账|情商|人心|捞|靠山|拍马屁|圈钱|割|被裁|裁员|降薪|辞退|离职|跳槽|升职|提拔|"
                      r"彩礼|离婚|赡养|遗产|分家|养老|啃老|攀比|鄙视链|提成|陌拜|长衫|"
                      r"倒闭|破产|避雷|踩坑|失败|致命|认输|退网|押金|退钱|退款|买课|智商税|亏钱|血亏|赔光)")
MONEY_RX = re.compile(r"(\d+(?:\.\d+)?\s*(?:多|余)?\s*(?:万|亿|千|百)?\s*(?:元|块|美元)|[一二三四五六七八九十两百千万亿几]+\s*(?:万|亿)?\s*(?:元|块钱|块)|"
                      r"\d+(?:\.\d+)?\s*(?:万|亿)|工资|薪水|薪资|月薪|年薪|收入|存款|房贷|月供|彩礼|赔偿|赔了|罚款|欠薪|讨薪|欠款|退休金|养老金|"
                      r"押金|补偿|违约金|学费|房租|租金|花了|省了|亏了|赚了|亏损|多少钱|赚钱|挣钱|搞钱|没钱|年入|月入|日入|利润|毛利|成本|价格|涨价|降价|便宜|贵)")
FAMILY_RX = re.compile(r"(防骗|骗局|诈骗|被骗|骗子|养老|养老金|退休|医保|社保|看病|住院|子女|毕业生|父母|老人|保健品|遗产|赡养|彩礼|租房|物业|孩子|婆婆|丈母娘|老公|老婆|婚)")
REVERSE_RX = re.compile(r"(竟|反而|居然|才知道|才发现|真相|为什么|凭什么|不是.{1,10}(?:而是|是)|越.{1,6}越|其实|别再|万能公式|反常识)")

V1_RX = re.compile(r"(内耗|焦虑自救|情绪管理|原生家庭|疗愈|治愈|自洽|和解|松弛|变强|正能量|鸡汤|自律|成长型|心态|"
                   r"顶级思维|思维方式|认知觉醒|认知升级|底层逻辑|复盘.{0,4}翻盘|成功学|逆袭人生|人生建议|格局|修养|精力管理|"
                   r"自我|活出|做自己|允许一切|能量|教你|一定要学会|"
                   r"(?:做成了?|成就|封神|传奇|逆袭).{0,6}(?:巨头|首富|帝国|百亿|千亿))")
V2_RX = re.compile(r"(特朗普|拜登|普京|美方|外交|制裁|外交部|商务部回应|国务院|共产党|党委|党建|两会|中美|北约|乌克兰|以色列|选举|总统|白宫|大使|(?:甩锅|制裁|围堵|打压)中国|中国值得|国家级论坛)")
V3_RX = re.compile(r"(上新|好友日|训练营|报名|招生|课程|直播间|研学|走进|对标学习|优惠|福利|抽奖|带货|小店|团购|限时|特惠|下单|领取|免费领|"
                   r"开课|课程表|招募|广告|推广|好物|种草|测评)")
V4_RX = re.compile(r"(早报|晚报|周报|日报|快讯|速递|简讯|丨AI周报|融资丨|完成.{0,12}融资|轮融资|天使轮|Pre-?A|A轮|B轮|C轮|"
                   r"；|_快讯|首发|官宣|任命|出任|加入|履新|发布会|签约|战略合作|落地|投建|开业|上线|发布)")
V5_RX = re.compile(r"(AI|大模型|模型|芯片|算力|GPU|机器人|具身|智能体|Agent|iPhone|手机|折叠屏|自动驾驶|Robotaxi|智驾|新能源车|"
                   r"光器件|半导体|SaaS|云计算|开源|算法|元宇宙|量子|卫星|火箭|eVTOL|无人机|鸿蒙|英伟达|OpenAI|Anthropic)", re.I)
V6_RX = re.compile(r"(电影|综艺|明星|演员|票房|追剧|剧集|短剧|漫剧|游戏|电竞|动漫|歌手|演唱会|球队|足球|篮球|NBA|世界杯|奥运)")
V7_RX = re.compile(r"(股价|涨停|跌停|A股|美股|港股|炒股|荐股|大盘|牛市|熊市|比特币|币圈|加密货币|基金经理|收益率|证券|券商|理财|信托|家族办公室|金价|油价)")
V8_RX = re.compile(r"(工具矩阵|SWOT|PEST|波特五力|模板|手把手|教程|干货|全攻略|指南|清单大全|方法论|框架图|速查|赚钱项目|网络兼职|兼职项目)")
CAP_RX = re.compile(r"(IPO|上市|股价|市值|估值|财报|融资|股权|营收|净利|并购|收购|基金|投资人|资本|季度|年报|"
                    r"腾讯|阿里|字节|美团|京东|拼多多|小米|华为|特斯拉|苹果|英伟达|OpenAI)", re.I)


def has_person(t):
    return PERSON_RX.search(_EXCL.sub("\u3000", t or ""))


def column_hits(t):
    out = {}
    for c in COLUMNS:
        hs = [w for w in COLUMN_LEX[c] if w and w in t]
        if hs:
            out[c] = hs
    return out


def score(title, base_col=""):
    t = title or ""
    s = 0.0
    why = []
    p = has_person(t)
    if p:
        s += 2; why.append("P人:" + p.group(0))
    m = MOTIF_RX.findall(t)
    if m:
        s += 2; why.append("M母题:" + "/".join(sorted(set(m))[:3]))
    mo = MONEY_RX.search(t)
    if mo:
        s += 1.5; why.append("$钱:" + mo.group(0))
    ch = column_hits(t)
    if ch:
        s += min(2, len(ch)); why.append("C栏目:" + ",".join(f"{c}({'/'.join(v[:2])})" for c, v in ch.items()))
    f = FAMILY_RX.search(t)
    if f:
        s += 1; why.append("F家人:" + f.group(0))
    if REVERSE_RX.search(t):
        s += 0.5; why.append("R反常识")
    cap = None
    vetoes = []
    if V4_RX.search(t):
        s -= 3; vetoes.append("V4快讯/融资/人事")
    if V5_RX.search(t) and not (p or m):
        s -= 2; vetoes.append("V5纯科技")
    if V6_RX.search(t):
        s -= 3; vetoes.append("V6娱乐")
    if V7_RX.search(t):
        s -= 2; vetoes.append("V7荐股/股价")
    if V8_RX.search(t):
        s -= 1; vetoes.append("V8工具干货")
    if CAP_RX.search(t) and not p:
        s -= 1; vetoes.append("K公司资本无人")
    if V1_RX.search(t):
        cap = "低"; vetoes.append("V1鸡汤/反内耗")
    if V2_RX.search(t):
        cap = "无"; vetoes.append("V2政治")
    if V3_RX.search(t):
        cap = "无"; vetoes.append("V3广告/课程")
    # 拼接快讯（多分号/超长）直接无
    if t.count("；") + t.count(";") >= 3 or len(t) > 60:
        cap = "无"; vetoes.append("拼接/超长标题")
    lvl = "高" if s >= 4 else ("中" if s >= 2 else ("低" if s >= 0.5 else "无"))
    order = ["无", "低", "中", "高"]
    if cap and order.index(lvl) > order.index(cap):
        lvl = cap
    # 栏目归属：column_lex 命中最多者；平手按 IP 主题优先级（做生意>看职场>带队伍>做销售>去创业>过日子）；无命中沿用主库栏目
    if ch:
        col = max(ch, key=lambda c: (len(ch[c]), -COLUMNS.index(c) if c != "做生意" else 9))
    elif f:
        col = "过日子"
    else:
        col = base_col if base_col in COLUMNS else "其他"
    return lvl, round(s, 2), col, why, vetoes



LEVEL_MARK = {"高": "🟢", "中": "🟡", "低": "⚪", "无": "🔴"}
IP_MARK = "\U0001f9ed"  # 🧭
_HIT_NAME = {"P": "人", "M": "人性词", "$": "钱", "C": "栏目", "F": "家人", "R": "反常识"}
LEGEND = ("IP 契合（🧭，ip-fit-v1，标题级规则）= 具体的人 / 人性母题 / 普通人的钱 / 六栏目 / 家人防骗 / 反常识 加分，"
          "快讯、纯科技、娱乐、荐股、鸡汤、政治、广告 减分或封顶；只并列展示，不改主分、不参与过滤与排序。"
          "冻结抽检二分类准确率 84%，有效类精确率 63%（偏宽）。")


def hit_names(why):
    """命中理由 → 短标签（去掉具体词，防止「母题:」被 radar_v4.motifs_of 误读）。"""
    out = []
    for w in why or []:
        n = _HIT_NAME.get(w[:1])
        if n and n not in out:
            out.append(n)
    return out


def ip_fit(headline, base_col=""):
    lvl, sc, col, why, vet = score(headline or "", base_col)
    return {"ip_fit": lvl, "ip_fit_score": sc, "ip_fit_column": col,
            "ip_fit_hits": hit_names(why), "ip_fit_vetoes": list(vet), "ip_fit_src": VERSION}


def annotate_sets(sets):
    """原地给 sets 加 ip_fit* 键；不碰任何已有键（含 score / spread_*）。"""
    for s in sets:
        s.update(ip_fit(s.get("topic") or "", s.get("column") or ""))
    return sets


def line_for(r):
    hits = "、".join(r["ip_fit_hits"]) or "无命中"
    veto = ("；减分 " + "、".join(v.lstrip("VK0123456789G") for v in r["ip_fit_vetoes"])) if r["ip_fit_vetoes"] else ""
    return f"> {IP_MARK} IP 契合 {LEVEL_MARK[r['ip_fit']]} {r['ip_fit']}（命中 {hits}{veto}；只并列展示，不改主分与排序）"


_HEAD = re.compile(r"^###\s+\[(.+?)\]\((.+?)\).*$", re.M)
_OLD = re.compile(r"\n> \U0001f9ed IP 契合 [^\n]*")


def annotate_brief_text(md):
    """每个 ### 条目标题行下方插/换一行 IP 契合。幂等。标题行本身一字不动（传播分正则仍可重跑）。"""
    md = _OLD.sub("", md or "")
    return _HEAD.sub(lambda m: m.group(0) + "\n" + line_for(ip_fit(m.group(1).strip())), md)


def annotate_brief_file(posts_dir, day):
    p = os.path.join(posts_dir, f"{day}-summary-zh.md")
    if not os.path.isfile(p):
        return None
    txt = open(p, encoding="utf-8").read()
    new = annotate_brief_text(txt)
    if new != txt:
        open(p, "w", encoding="utf-8").write(new)
    return p
