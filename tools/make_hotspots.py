#!/usr/bin/env python3
"""从 Horizon 每日简报中抽取 ```hotspots 代码块, 生成 hotspots.json.

hotspots.json 是「人性台账」生产流程 v3.4.6 阶段 3.5 (gen_viral_content)
的热点输入契约, 字段含义见《爆款内容方法论》第五节。
用法: python tools/make_hotspots.py --summaries data/summaries --out hotspots.json

D2 Set 决策单元 (2026-11-11):
  hotspots.json 新增 "sets" 数组: 每个话题带上父条目的公众号分 score、
  视频分 video_score（标题行有 🎬 X/12 时解析, 模型没输出时为 null，
  由助理按 rubric 补评，不在 radar run 里等模型）、5 真分类 category_5、
  来源 source。文章线按 score 挑、视频线按 video_score >= 7 拍，
  同一话题的文章+视频构成一个 Set。原 "topics" 字符串数组原样保留，
  供 v3.4.6 阶段 3.5 继续使用，不破坏旧契约。

终轮契约升级 (2026-11-12):
  sets 每项新增 source_url（条目原文链接，从标题行 [标题](url) 取）、
  source_name（与 source 同值，明确语义）、verified（该条是否通过核实：
  简报里有“封顶：X”行即 False，无封顶即 True，无分数时为 None）。
  旧键 topic/score/video_score/category_5/source 全部保留，不破坏旧契约。
  剧本可视化可行性复核 (2026-11-13，全盘打磨方案 §七 联动断点 2)：
  sets 每项再新增 video_feasibility（可视化可行 / 部分可行 / 不可行，
  标题行无此字段时为 null）与 video_feasibility_reason（"文章数字 N
  个，可落位模板 M 种"）。video_score 本身、7.0 的 hotspots 线与评分
  标准文字一概不动；这档只回答"剧本能不能把文章数字说出来"。
  每轮写完 hotspots.json 后，顺带更新 data/score_distribution.json
  （见 tools/score_distribution.py）：各分段条数、最高分、≥7.0 数、
  过 5.0 阈值条数、候选池总数、各源贡献条数，按日期追加，供“选题荒
  还是标准收紧”的判断有数可查，不再靠手工挖 12 天历史。

BUG-2/BUG-3 已修 (2026-11-12):
  回退时 min_score 不再改写为回退分档，恒为 MIN_SCORE (7.0)；
  note 回退时追加回退明文。BUG-1（daily-renxing.yml 的 curl 无 -f）
  在 workflow 文件里，本脚本动不了，须随 workflow 文件另行处理。

v1.6 硬过滤 (2026-10-27):
  简报里每个条目以 `### 标题 ⭐️ X.0/10` 开头, 其下的 ```hotspots 代码块
  是该条目的候选选题。旧版不看分数全量抽取, 导致 5-6 分条目也进了
  hotspots.json (2026-10-27 实测: 30 个条目仅 1 个 7.0 分, 却抽出 90 条)。
  本版按条目分数硬过滤: 低于 MIN_SCORE 的条目整块丢弃, 每条目最多保留
  MAX_PER_ITEM 条, 全局去重, 按分数降序。prompt 不动, 只靠代码层把关。
  若当日没有任何条目达到 MIN_SCORE, 回退到当日最高分档 (避免空 feed),
  并在输出 JSON 里标注 fallback_score。（P315 R8：回退只取该档第 1 个
  条目、最多 3 个话题，低分凑数不再出简报。）
"""

import argparse
import datetime
import glob
import html
import json
import os
import re
import sys

# --- 硬过滤参数: 改这里即可调线, 不用动 prompt ---
MIN_SCORE = 7.0      # 低于此分的条目不进 hotspots.json
MAX_PER_ITEM = 3     # 每个条目最多保留的选题数 (取块内前 N 行)

# ★ P315 Top10 R4/R5/R8（2026-11-14）：来源配额 + 栏目分流统计 + 干旱日回退收敛。
#   R4：单源占入选话题超 SOURCE_QUOTA_WARN_PCT 即 WARN，只告警、不删源、不改
#       入选结果；百度是当前单点（v2 报告：百度供 ≥7.0 的 64%），目标 <40%。
#   R5：五栏目当日入选数 / ≥7.0 数统计进 score_distribution.json 记录
#       （per_column），某栏目连续 COLUMN_ZERO_ALERT_DAYS 日入选为 0 即告警。
#   R8：干旱日回退只取最高分档第 1 个条目（其余同档条目计入
#       fallback_capped_sections），话题数仍受 MAX_PER_ITEM 约束，最多 3 条。
SOURCE_QUOTA_WARN_PCT = 0.40  # 单源占比告警线（入选话题 / ≥7.0 条目任一超线即 WARN）
COLUMNS_5 = ("看职场", "带队伍", "做生意", "做销售", "去创业")
COLUMN_ZERO_ALERT_DAYS = 3    # 某栏目连续多日（默认 3 日，含当日）入选为 0 即告警

HOTSPOTS_RX = re.compile(r"```hotspots\s*\n(.*?)```", re.S)
# Horizon 的 summarizer 落盘前会对 AI 输出做 markdown 转义：
#   _MARKDOWN_SPECIAL: \ ` * _ { } [ ] ( ) < > # ! | → 前加反斜杠
#   _MARKDOWN_BLOCK_START: 行首的 "- " → "\- "
# 所以简报里的 hotspots 代码块实际是 \`\`\`hotspots … \- … \`\`\`。
# 抽取前必须先还原，否则正则永远匹配不到（2026-10-16 实测：run #4
# 明明选中 1 条、简报里有 6 个话题，topics 依然是 []）。
_MD_ESCAPE_RX = re.compile(r"\\([\\`*_{}\[\]()<>#!|\-])")

# 条目原文链接: 标题行 `[标题](url)` 里的 url。
_URL_RX = re.compile(r"\]\((https?://[^)\s]+)\)")
# 封顶行: summarizer 在被封顶的条目下写 `封顶：未证实7分`。
_CAP_RX = re.compile(r"^封顶：\s*(标题级6分|未证实7分|纯观点6分)\s*$", re.M)
# 候选池总数: 简报头 `> 从 514 条内容中筛选出 11 条重要资讯。`
_POOL_RX = re.compile(r"从\s*(\d+)\s*条内容中筛选出")
# ⭐ 是 U+2B50, 后常跟 U+FE0F 变体选择符。
_SCORE_EMOJI_RX = re.compile(r"⭐\uFE0F?\s*(\d+(?:\.\d+)?)\s*/\s*10")
# 视频分: 标题行 "🎬 8.5/12"（🎬 是 U+1F3AC, 后常跟 U+FE0F）。
_VIDEO_SCORE_RX = re.compile(r"🎬\uFE0F?\s*(\d+(?:\.\d+)?)\s*/\s*12")
# 剧本可视化可行性: 标题行 "· 可视化可行（5数/3模板）"，由 summarizer 在
# 🎬 后渲染（src/ai/video_feasibility.py 定档位，LEVELS 三档）。
# 顺序不能乱：先试 "可视化可行"/"部分可行"，最后 "不可行"（"不可行"含
# "可行"二字，裸 "可行"绝不能单独成档）。counts 缺失时只取档位、原因
# 为 None（旧简报无此字段时就是这样）。
_FEASIBILITY_RX = re.compile(r"·\s*(可视化可行|部分可行|不可行)(?:（(\d+)数/(\d+)模板）)?")
# 5 真分类: 正文首个 "归属：做生意"（第一角度即主归属）。
_CATEGORY_RX = re.compile(r"归属：\s*([^；\n，,]+)")
# 来源: 正文 "rss · 36氪24小时热榜 · 11月8日 08:43" 一行。
# P318-4：google_news 必须排在 google 之前——Google News 源类型值是
# google_news，正则只列 google 时来源恒为 unknown（11-16 per_source 有
# 1 条 unknown）。顺序不能乱：google 在前会先吞掉 "google" 前缀。
_SOURCE_RX = re.compile(r"^(?:rss|tophub|newsnow|google_news|google|百度|头条|rsshub)\s*·\s*([^\n·]+)", re.M)
_SCORE_EOL_RX = re.compile(r"(\d+(?:\.\d+)?)\s*/\s*10\s*$")
_HEADING_RX = re.compile(r"^#{2,4}\s", re.M)


def _unescape(text: str) -> str:
    text = html.unescape(text)
    return _MD_ESCAPE_RX.sub(r"\1", text)


def _extract_score(heading_line: str):
    """从条目标题行提取分数, 提不到返回 None。"""
    m = _SCORE_EMOJI_RX.search(heading_line)
    if m:
        return float(m.group(1))
    m = _SCORE_EOL_RX.search(heading_line.strip())
    if m:
        return float(m.group(1))
    return None


def _extract_video_score(heading_line: str):
    """从条目标题行提取视频分, 提不到返回 None（模型没输出时为 null）。"""
    m = _VIDEO_SCORE_RX.search(heading_line)
    return float(m.group(1)) if m else None


def _extract_video_feasibility(heading_line: str):
    """从条目标题行提取剧本可视化可行性, 返回 (档位|None, 原因|None)。

    原因由标题里的 counts 还原（"文章数字 5 个，可落位模板 3 种"）；
    标题无此字段（旧简报 / 未复核）时返回 (None, None)。
    """
    m = _FEASIBILITY_RX.search(heading_line)
    if not m:
        return None, None
    level = m.group(1)
    if m.group(2) is None or m.group(3) is None:
        return level, None
    return level, f"文章数字 {m.group(2)} 个，可落位模板 {m.group(3)} 种"


def _extract_category(body: str):
    """从正文首个“归属：X”取 5 真分类, 提不到返回 None。

    P318-3：白名单校验——enrichment.md 模板原文「归属：5真分类之一」
    被模型原样回吐，垃圾值已进生产 hotspots（11-16 第 3 条）。不在
    COLUMNS_5 内的取值记 None 并 WARN，不让它污染栏目统计。
    """
    m = _CATEGORY_RX.search(body)
    if not m:
        return None
    value = m.group(1).strip()
    if value not in COLUMNS_5:
        print(f"警告: 归属取值不在 5 真分类内，已丢弃：{value!r}",
              file=sys.stderr)
        return None
    return value


def _extract_source(body: str):
    """从正文来源行取来源名（如 36氪24小时热榜）, 提不到返回 None。"""
    m = _SOURCE_RX.search(body)
    return m.group(1).strip() if m else None


def _extract_url(heading_line: str):
    """从标题行 `[标题](url)` 取条目原文链接, 提不到返回 None。"""
    m = _URL_RX.search(heading_line)
    return m.group(1) if m else None


def _extract_cap(body: str):
    """从正文 `封顶：X` 行取封顶名, 未被封顶返回 None。"""
    m = _CAP_RX.search(body)
    return m.group(1) if m else None


# ★ P317：双评 / 五维 / 证据等级 / 事件组——summarizer 渲染的行
# （旧简报无这几行时一律为 None，不破坏旧契约）。
_DUAL_EVAL_RX = re.compile(
    r"^双评：\s*([0-9.]+(?:\s*\+\s*[0-9.]+)?)\s*（(dual|single_eval)）\s*$", re.M)
_DIMENSION_RX = re.compile(r"^五维：\s*(.+?)\s*$", re.M)
_EVIDENCE_RX = re.compile(
    r"^证据等级：\s*(title_only|unconfirmed|opinion_only|verified)\s*$", re.M)
_EVENT_GROUP_RX = re.compile(r"^事件组：\s*(evt:[^\s（]+)", re.M)
# P319-1：开头类型（66.1% 开头规律的度量口径）。summarizer 渲染
# “开头类型：热点/反常识/真实案例/其他”行，旧简报无此行为 None。
_OPENING_RX = re.compile(r"^开头类型：\s*(热点|反常识|真实案例|其他)\s*$", re.M)
_DIMENSION_NAMES = ("证据", "机制", "决策", "张力", "金句")


def _extract_dual_eval(body: str):
    """从正文 `双评：7 + 9（dual）` 行取 (score_rounds|None, dual_eval|None)。"""
    m = _DUAL_EVAL_RX.search(body)
    if not m:
        return None, None
    try:
        rounds = [float(x) for x in m.group(1).split("+")]
    except ValueError:
        return None, None
    if not rounds or len(rounds) > 2 or any(r < 0 or r > 10 for r in rounds):
        return None, None
    return rounds, m.group(2)


def _extract_dimension_scores(body: str):
    """从正文 `五维：证据 2 / 机制 1.5 / …` 行取 {维度: 分}|None。"""
    m = _DIMENSION_RX.search(body)
    if not m:
        return None
    scores = {}
    for part in m.group(1).split("/"):
        part = part.strip()
        if not part:
            continue
        name, _, value = part.rpartition(" ")
        try:
            num = float(value)
        except ValueError:
            return None
        if name not in _DIMENSION_NAMES or not 0 <= num <= 2:
            return None
        scores[name] = num
    return scores or None


def _extract_evidence_level(body: str):
    """从正文 `证据等级：unconfirmed` 行取等级名，未写返回 None。"""
    m = _EVIDENCE_RX.search(body)
    return m.group(1) if m else None


def _extract_event_group(body: str):
    """从正文 `事件组：evt:…` 行取事件组 id，未写返回 None。"""
    m = _EVENT_GROUP_RX.search(body)
    return m.group(1) if m else None


def _extract_opening_type(body: str):
    """P319-1：从正文 `开头类型：X` 行取开头类型（热点/反常识/
    真实案例/其他），未写返回 None。只加字段+透传，不做模型分类
    的二次判断；分类一致率须先在人工抽查上验准再谈纠偏。"""
    m = _OPENING_RX.search(body)
    return m.group(1) if m else None


def _norm(topic: str) -> str:
    """去重用归一化: 去空格与常见标点, 避免同一话题换个写法重复进。"""
    return re.sub(r"[\s·,，。！？!?:：、\-—_()\[\]\"\"''<>#*]", "", topic)


def _split_sections(text: str):
    """按 ##/### 标题切分简报, 返回 [(标题行, 正文)]。"""
    starts = [m.start() for m in _HEADING_RX.finditer(text)]
    if not starts:
        return []
    starts.append(len(text))
    sections = []
    for i in range(len(starts) - 1):
        chunk = text[starts[i]:starts[i + 1]]
        heading, _, body = chunk.partition("\n")
        sections.append((heading.strip(), body))
    return sections


def _block_topics(body: str):
    """抽取一个 section 内所有 hotspots 块的行, 保持原顺序。"""
    topics = []
    for block in HOTSPOTS_RX.findall(body):
        for line in block.splitlines():
            line = line.strip()
            m = re.match(r"^[-*]\s+(.+)$", line)
            if m:
                topic = m.group(1).strip()
                if topic:
                    topics.append(topic)
    return topics


def extract_scored_topics(md_path):
    """返回 [(score|None, video_score|None, category|None, source|None,
    url|None, cap|None, feasibility|None, feasibility_reason|None,
    [topics], extra), ...], 每项对应简报里的一个条目。
    extra 是 P317 详情 dict：score_rounds / dual_eval / dimension_scores /
    evidence_level / event_group（旧简报无对应行时值为 None）；P319-1
    再加 opening_type。

    P318-16：有分数无 hotspots 块的条目也返回（topics 为 []）——11-15
    最高分 7.0 条目（德国汽车）正因无块而被分布统计漏掉，简报称 7.0、
    提交物称 6.0 的口径打架根源就在这里。无分数又无块的 section
    （分栏小标题等）仍跳过。"""
    with open(md_path, encoding="utf-8") as f:
        text = _unescape(f.read())
    scored = []
    for heading, body in _split_sections(text):
        topics = _block_topics(body)
        score = _extract_score(heading)
        if not topics and score is None:
            continue
        feasibility, feasibility_reason = _extract_video_feasibility(heading)
        score_rounds, dual_eval = _extract_dual_eval(body)
        scored.append((score, _extract_video_score(heading),
                        _extract_category(body), _extract_source(body),
                        _extract_url(heading), _extract_cap(body),
                        feasibility, feasibility_reason, topics, {
                            "score_rounds": score_rounds,
                            "dual_eval": dual_eval,
                            "dimension_scores": _extract_dimension_scores(body),
                            "evidence_level": _extract_evidence_level(body),
                            "event_group": _extract_event_group(body),
                            "opening_type": _extract_opening_type(body),
                        }))
    return scored


def legacy_extract(md_path):
    """旧行为: 不分条目, 全量抽取。

    P318-5：主流程已不再回退到它（无分数即 exit 1）。保留供人工恢复
    时调用；任何自动流程调用它都是 bug。
    """
    with open(md_path, encoding="utf-8") as f:
        text = _unescape(f.read())
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


# ★ P314 Top10 第 7 项：占位标题（选题单里的待填行）一律丢弃，14.5%→0%。
#   源标题回退属 Horizon 上游 enricher 侧，不在本补丁内，见取材表规范。
_PLACEHOLDER_RX = re.compile(r"选题单|选题台账|待填|占位|⚠\s*待填")


def _is_placeholder(topic):
    """占位标题：空标题，或含选题单/选题台账/待填/占位/⚠ 待填。topics 为纯字符串。"""
    t = str(topic or "")
    return (not t.strip()) or bool(_PLACEHOLDER_RX.search(t))


def source_shares(sources):
    """来源占比：sources 为来源名列表，返回 [(来源, 条数, 占比)] 按条数降序。

    来源取不到记为 "unknown"。占比分母是传入列表长度；空列表返回 []。
    """
    total = len(sources)
    if total == 0:
        return []
    counts = {}
    for s in sources:
        key = (s or "unknown").strip() or "unknown"
        counts[key] = counts.get(key, 0) + 1
    return sorted(((k, v, v / total) for k, v in counts.items()),
                  key=lambda x: (-x[1], x[0]))


def quota_warnings(shares, threshold=SOURCE_QUOTA_WARN_PCT):
    """超配额来源：shares 为 source_shares 的返回，返回 [来源, ...]。

    只做判断，不删源、不改入选；调用方只打印 WARN，退出码保持 0。
    """
    return [src for src, _n, pct in shares if pct > threshold]


def column_stats(scored, sets):
    """五栏目分流统计：返回 {栏目: {"items": 简报条目数, "ge_7": ≥7.0 条目数,
    "selected": 入选话题数}}。栏目不在五栏内的（含取不到）归入 "其他"。"""
    stats = {c: {"items": 0, "ge_7": 0, "selected": 0} for c in COLUMNS_5}
    stats["其他"] = {"items": 0, "ge_7": 0, "selected": 0}
    for score, _v, category, _src, _u, _cap, _f, _fr, _t, _extra in scored:
        if score is None:
            continue
        key = category if category in stats else "其他"
        stats[key]["items"] += 1
        if score >= MIN_SCORE:
            stats[key]["ge_7"] += 1
    for s in sets:
        key = s.get("category_5") if isinstance(s, dict) else None
        stats[key if key in stats else "其他"]["selected"] += 1
    return stats


def _read_history_records(path):
    """容错读 score_distribution.json 的 records 列表：文件缺失/损坏返回 []。"""
    try:
        with open(path, encoding="utf-8") as f:
            history = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    records = history.get("records", []) if isinstance(history, dict) else []
    return [r for r in records if isinstance(r, dict)]


def _read_funnel(dist_path):
    """P318-6：从 data/fetch_report.json 取本轮抓取漏斗的 analyzed 数。

    文件缺失（旧跑 / 手动跑 / orchestrator 未写）时返回 None，分布
    只记抓取与过线数，不编 analyzed。返回 {"analyzed": int} 或 None。
    """
    fetch_path = os.path.join(
        os.path.dirname(os.path.abspath(dist_path)), "fetch_report.json"
    )
    try:
        with open(fetch_path, encoding="utf-8") as f:
            report = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(report, dict):
        return None
    analyzed = report.get("analyzed")
    return {"analyzed": analyzed} if isinstance(analyzed, int) else None


def _write_step_summary_alerts(column_alerts, quota_record):
    """P318-8：配额/栏目告警写 $GITHUB_STEP_SUMMARY（workflow 内可见）。

    本地跑（无该变量）时只打印，不写盘；无告警时什么都不写。
    """
    if not column_alerts and not (
        quota_record and quota_record.get("source_quota_warn")
    ):
        return
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    lines = ["## 雷达配额 / 栏目告警", ""]
    for col in column_alerts:
        lines.append(
            f"- 栏目「{col}」连续 {COLUMN_ZERO_ALERT_DAYS} 日入选为 0，"
            f"请按取材表补源（R5）"
        )
    if quota_record:
        for src in quota_record.get("source_quota_warn", []):
            lines.append(
                f"- 来源「{src}」占比超 {SOURCE_QUOTA_WARN_PCT:.0%}"
                f"（R4 目标：百度 <40%）"
            )
    try:
        with open(summary_path, "a", encoding="utf-8") as f:
            f.write("\n" + "\n".join(lines) + "\n")
    except OSError:
        pass


def column_zero_alerts(records, days=COLUMN_ZERO_ALERT_DAYS, today=None):
    """栏目连续零入选告警：返回 [栏目, ...]。

    只看以 today 结尾的连续自然日：某栏目在最近 days 天（含当日）每天
    的 per_column.selected 均为 0 即告警。缺某日记录、或当日记录缺
    per_column（旧记录），都算“无法确认”，断开连续计数，不误报。
    """
    if today is None:
        today = datetime.date.today()
    by_date = {}
    for r in records:
        per_column = r.get("per_column")
        if not isinstance(per_column, dict):
            continue
        try:
            d = datetime.date.fromisoformat(str(r.get("date", "")))
        except ValueError:
            continue
        by_date[d] = per_column
    alerts = []
    for col in COLUMNS_5:
        run = 0
        for i in range(days):
            day = today - datetime.timedelta(days=i)
            per_column = by_date.get(day)
            if per_column is None:
                break
            col_stat = per_column.get(col)
            if not isinstance(col_stat, dict):
                break
            if col_stat.get("selected", 0) != 0:
                break
            run += 1
        if run >= days:
            alerts.append(col)
    return alerts


def filter_topics(scored):
    """硬过滤, 返回 (topics, sets, stats)。

    topics: 字符串数组（旧契约, 原样保留）。
    sets: 与 topics 同一批保留话题, 每项 {topic, score, video_score,
    video_feasibility, video_feasibility_reason, category_5, source,
    source_url, source_name, verified, cap_triggered,
    score_rounds, dual_eval, dimension_scores, evidence_level,
    event_group, rumor_stance}（P317 新增的 6 个键：旧简报无对应行时为
    null；rumor_stance 只在被封顶时为 "传闻/待核实"，其余为 null）。
    P319-1 再加 opening_type（旧简报无对应行时为 null）。
    video_score 为 None 表示 radar run 未输出，由助理按 rubric 补评。
    video_feasibility 为 None 表示该条未复核（旧简报无此字段 / 分析
    失败）；有值时为 可视化可行 / 部分可行 / 不可行，原因见
    video_feasibility_reason（"文章数字 N 个，可落位模板 M 种"）。
    verified: 有分数且未被封顶为 True，被封顶为 False，无分数为 None。
    stats: kept_sections / dropped_sections / fallback_score(None=未回退) /
    fallback_capped_sections（干旱日回退时同档被截掉的条目数，P315 R8）。
    """
    stats = {"kept_sections": 0, "dropped_sections": 0,
             "placeholder_dropped": 0, "fallback_score": None,
             "fallback_capped_sections": 0}
    # P318-16：无 hotspots 块的条目（topics 为 []）不参与选话题——它们
    # 只进分布统计（total/max/counts），不参与分档回退的 tier 计算。
    # 否则 11-15 那种“7.0 条目无块”的日子会把回退线抬到 7.0，导致
    # 有块的 6.0 条目全被丢弃、整轮空跑。
    with_score = [(s, v, c, src, u, cap, feas, feas_reason, t, e)
                  for s, v, c, src, u, cap, feas, feas_reason, t, e in scored
                  if s is not None and t]
    if not with_score:
        return None, None, stats  # 调用方回退旧行为

    tiers = sorted({s for s, *_ in with_score}, reverse=True)
    line = MIN_SCORE
    if all(s < MIN_SCORE for s in tiers):
        line = tiers[0]  # 当日无过线条目: 回退到最高分档
        stats["fallback_score"] = line

    kept = []  # (score, video_score, category, source, url, cap, feasibility, reason, order, topic, extra)
    order = 0
    fallback_kept = False  # P315 R8：干旱日回退是否已取过最高分档第 1 个条目
    for score, video_score, category, source, url, cap, feas, feas_reason, topics, extra in with_score:
        if score < line:
            stats["dropped_sections"] += 1
            continue
        kept_topics = [t for t in topics[:MAX_PER_ITEM]
                     if not _is_placeholder(t)]
        stats["placeholder_dropped"] += (
            len(topics[:MAX_PER_ITEM]) - len(kept_topics))
        if not kept_topics:
            stats["dropped_sections"] += 1
            continue
        if stats["fallback_score"] is not None and fallback_kept:
            # ★ P315 R8：干旱日回退只取最高分档第 1 个条目，其余同档条目
            #   不进简报（不算 dropped_sections：它们并未低于分数线）。
            stats["fallback_capped_sections"] += 1
            continue
        fallback_kept = True
        stats["kept_sections"] += 1
        for topic in kept_topics:
            kept.append((score, video_score, category, source, url, cap,
                         feas, feas_reason, order, topic, extra))
            order += 1

    seen = set()
    topics = []
    sets = []
    for score, video_score, category, source, url, cap, feas, feas_reason, _, topic, extra in sorted(
            kept, key=lambda x: (-x[0], x[8])):
        if _norm(topic) in seen:
            continue
        seen.add(_norm(topic))
        topics.append(topic)
        sets.append({
            "topic": topic,
            "score": score,
            "video_score": video_score,
            "video_feasibility": feas,
            "video_feasibility_reason": feas_reason,
            "category_5": category,
            "source": source,
            "source_url": url,
            "source_name": source,
            "verified": (cap is None),
            "cap_triggered": cap,
            # P317：双评 / 五维 / 证据等级 / 事件组进 sets（旧简报无对应行时为 null）。
            "score_rounds": extra.get("score_rounds"),
            "dual_eval": extra.get("dual_eval"),
            "dimension_scores": extra.get("dimension_scores"),
            "evidence_level": extra.get("evidence_level"),
            "event_group": extra.get("event_group"),
            # P319-1：开头类型进 sets（旧简报无对应行时为 null）——66.1%
            # 开头规律从此可度量；分类本身只透传模型输出，不二次判断。
            "opening_type": extra.get("opening_type"),
            # P317：传闻口径单独字段——被封顶（verified=false）的条目只能按
            # 传闻/待核实拍，不能只靠 video_reason 文字；未封顶为 null。
            "rumor_stance": "传闻/待核实" if cap is not None else None,
        })
    return topics, sets, stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--summaries", required=True, help="Horizon 简报目录 (data/summaries)")
    ap.add_argument("--out", required=True, help="输出的 hotspots.json 路径")
    args = ap.parse_args()

    # P317：按写入时间取最新简报——文件名排序在跨日重命名/迟到重跑后会取错。
    files = sorted(glob.glob(os.path.join(args.summaries, "horizon-*.md")),
                   key=os.path.getmtime)
    min_score = MIN_SCORE
    fallback_score = None
    scored = []
    column_record = None  # P315 R5：五栏目分流统计（仅正常过滤分支产出）
    quota_record = None   # P315 R4：入选话题来源占比 + 超线告警清单
    if not files:
        print("未找到简报文件, hotspots.json 写空 topics", file=sys.stderr)
        topics = []
        sets = []
    else:
        latest = files[-1]
        print(f"读取最新简报: {latest}")
        scored = extract_scored_topics(latest)
        topics, sets, stats = filter_topics(scored)
        if topics is None:
            # P318-5：简报里没有任何可解析的条目分数（格式变了）即失败，
            # 不再回退全量抽取——2026-10-27 的 90 话题事故正是这条回退
            # 路径（legacy_extract，无过滤、exit 0、min_score 仍写 7.0）
            # 的复刻条件，只差一次简报格式漂移。格式变了就该失败（与空
            # 硬门同级），触发 watchdog 补跑，而不是静默全量污染 hotspots。
            # legacy_extract() 保留供人工恢复时调用，主流程不再用它。
            print("错误：简报中未解析到任何条目分数（简报格式可能已变），"
                  "拒绝回退全量抽取", file=sys.stderr)
            return 1
        else:
            fallback_score = stats["fallback_score"]
            if fallback_score is not None:
                # BUG-2 已修：min_score 保持 7.0，不改写为回退分档。
                print(f"当日无 {MIN_SCORE} 分以上条目, 回退到最高分档 {fallback_score} 分")
            print(f"硬过滤: 保留 {stats['kept_sections']} 个条目, "
                  f"丢弃 {stats['dropped_sections']} 个低于 {min_score} 分的条目, "
                  f"占位标题丢弃 {stats['placeholder_dropped']} 条, "
                  f"抽取到 {len(topics)} 个话题")
            if fallback_score is not None:
                print(f"干旱日回退收敛: 最高分档只取第 1 个条目, "
                      f"同档其余 {stats['fallback_capped_sections']} 个条目未进简报, "
                      f"话题上限 {MAX_PER_ITEM} 条")
            # ★ P315 R4：来源配额检查（入选话题 + ≥7.0 条目两口径），超线只 WARN。
            kept_sources = [s.get("source") for s in sets]
            ge7_sources = [src for s, _v, _c, src, *_r in scored
                           if s is not None and s >= MIN_SCORE]
            kept_shares = source_shares(kept_sources)
            quota_warn = set()
            for _label, _shares, _total in (
                    ("入选话题", kept_shares, len(kept_sources)),
                    ("≥7.0 条目", source_shares(ge7_sources), len(ge7_sources))):
                if not _shares:
                    print(f"来源配额（{_label}）: 无数据")
                    continue
                _line = "、".join(f"{_s} {_n} 条（{_p:.0%}）"
                                 for _s, _n, _p in _shares)
                print(f"来源配额（{_label}，共 {_total} 条）: {_line}")
                for _src in quota_warnings(_shares):
                    quota_warn.add(_src)
                    print(f"来源配额 WARN: {_src} 占比超 "
                          f"{SOURCE_QUOTA_WARN_PCT:.0%}（{_label}口径），"
                          f"只告警、不删源（R4 目标：百度 <40%）")
            quota_record = {
                "kept_source_shares": [
                    {"source": _s, "count": _n, "share": round(_p, 4),
                     # P317：单位标注——kept_source_shares 按话题计，与
                     # per_source（按条目计）单位不同，不要再混读。
                     "unit": "话题数（入选 hotspots 话题，按话题计）"}
                    for _s, _n, _p in kept_shares],
                "source_quota_warn": sorted(quota_warn),
            }
            # ★ P315 R5：五栏目分流统计（当日入选数 / ≥7.0 数）。
            column_record = column_stats(scored, sets)
            for _col in list(COLUMNS_5) + ["其他"]:
                _cs = column_record[_col]
                print(f"栏目分流（{_col}）: 简报 {_cs['items']} 条、"
                      f"≥7.0 {_cs['ge_7']} 条、入选 {_cs['selected']} 个话题")

    if not topics:
        # P317 硬门：hotspots 为空即失败——空简报不再静默 success，不写空
        # 文件、不更新分布、不走到提交步，上一次好结果原样保留（11-13
        # 迟到重跑与 11-14 两跑空简报的根因：空照常提交、job 照常 success）。
        print("错误：hotspots 为空（无简报文件，或当日无可抽取话题），"
              "拒绝写空文件", file=sys.stderr)
        return 1

    today = datetime.date.today().isoformat()
    note = ("由爆款雷达每日简报自动抽取, 供 v3.4.6 阶段3.5 gen_viral_content 使用。"
            "已按条目分数硬过滤 (低于 min_score 的条目不进), 使用前请人工过一遍: "
            "只选用与人性/商业判断主题真实相关的话题, 不硬蹭无关热点。"
            "Set 规则：同一话题的文章+视频构成一个 Set；文章线按 score 挑，"
            "视频线只拍 video_score >= 7 的话题；video_score 为 null 表示"
            " radar run 未输出，由助理按 rubric 补评后再定拍不拍。"
            "video_feasibility 为 null 表示未复核；“不可行”表示文章没有"
            "可上画面的数字，剧本无米下锅，选题会前先看这档。")
    if fallback_score is not None:
        # BUG-3 已修：回退时 note 必须写明回退，不能再写“已按条目分数硬过滤”。
        note += (f"【回退】当日无 {MIN_SCORE} 分以上条目，已按当日最高分档 "
                 f"{fallback_score} 分抽取（只取该档第 1 个条目、最多 3 个话题，"
                 f"低分凑数不再出简报）；min_score 仍为 {MIN_SCORE}，"
                 f"不是 {fallback_score}，使用前请重点核实事实（干旱日规则）。"
                 f"verified=false 的条目被封顶（标题级/未证实/纯观点），"
                 f"只可按传闻/待核实口径使用。")
    data = {
        "date": today,
        "scope": "人性台账爆款选题雷达·每日选题 (Horizon renxing profile, 图文源)",
        "queries": ["职场 OR 创业 OR 搞钱 OR 人性 OR 认知 OR 商业"],
        "topics": topics,
        "sets": sets,
        "min_score": min_score,
        "fallback_score": fallback_score,
        "note": note,
    }
    # ★ v4 迭代（2026-11-23）：R-P1-1 打标 / R-P1-2 同母题提示 / R-P0-2 实绩诊断。
    #   只加新键、只写简报尾部诊断行，不改任何分数与过滤结果；失败只告警。
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import radar_v4 as _rv4
        _data_dir = os.path.dirname(os.path.abspath(args.summaries))
        _actuals = _rv4.load_actuals(os.path.join(_data_dir, "feedback_actuals.jsonl"))
        _flog = _rv4.load_actuals(os.path.join(_data_dir, "feedback_log.jsonl"))
        with open(latest, encoding="utf-8") as _f:
            _md = _unescape(_f.read())
        _rv4.annotate_sets(sets, _md, _actuals, _flog)
        _n_act = _rv4.count_with_metrics(_actuals)
        if _n_act >= 30:
            _diag = _rv4.diagnose(_actuals)
            data["feedback_diag"] = dict(_diag, line=_rv4.diag_line(_diag), actual_records=_n_act)
            _lines = [_rv4.diag_line(_diag)]
            _reps = [f"《{x['topic']}》{x['repeat_hint']}" for x in sets if x.get("repeat_hint")]
            if _reps:
                _lines.append("同母题提示（R-P1-2）：" + "；".join(_reps))
            _posts = os.path.join(os.path.dirname(_data_dir), "docs", "_posts")
            _hit = _rv4.append_brief_tail(_posts, today, _lines)
            print(data["feedback_diag"]["line"] + (f"（已写入 {_hit} 尾部）" if _hit else "（当日简报文件不在，未写尾部）"))
        else:
            print(f"R-P0-2：有实绩记录 {_n_act} 条（<30），不出诊断行")
        print("R-P1-1 打标：" + "、".join(f"{x['topic']}={x['topic_type']}/{x['video_fit']}" for x in sets))
    except Exception as _exc:  # noqa: BLE001 - 打标与诊断是观测手段，不能拦主流程
        print(f"警告: v4 打标/诊断失败 ({_exc})，hotspots 分数与过滤不受影响", file=sys.stderr)

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(f"已写入 {args.out}")

    # 评分分布留存（终轮整改 2026-11-12）：每轮顺带把当日分数分布追加到
    # data/score_distribution.json，供“选题荒还是标准收紧”有数可查。
    # 分布写失败不得影响 hotspots.json（已经写完），只告警。
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from score_distribution import build_distribution, update_history
        dist_path = os.path.join(
            os.path.dirname(os.path.abspath(args.summaries)),
            "score_distribution.json",
        )
        # P318-6：漏斗的 analyzed 数取自 data/fetch_report.json（本轮
        # orchestrator 写）；文件缺失时 funnel 只有抓取与过线数。
        record = build_distribution(
            scored, md_path=latest if files else None,
            funnel=_read_funnel(dist_path),
        )
        # ★ P315 R4/R5：配额与栏目统计随当日分布记录一并留存（该文件已由
        #   workflow 提交回仓库，栏目连续零入选告警靠它跨日比对，不另建文件）。
        if quota_record is not None:
            record["kept_source_shares"] = quota_record["kept_source_shares"]
            record["source_quota_warn"] = quota_record["source_quota_warn"]
        if column_record is not None:
            record["per_column"] = column_record
        # P318-8：栏目连续零入选告警进当日分布记录（column_zero_alerts），
        # 并写 job summary。先算后写：keep-best 可能留旧记录，告警按
        # 本轮视角算，不等落盘后再读。
        column_alerts = []
        if column_record is not None:
            try:
                _alert_day = datetime.date.fromisoformat(record["date"])
            except ValueError:
                _alert_day = None
            if _alert_day is not None:
                column_alerts = column_zero_alerts(
                    _read_history_records(dist_path) + [record],
                    today=_alert_day,
                )
                if column_alerts:
                    record["column_zero_alerts"] = column_alerts
        update_history(dist_path, record)
        print(f"评分分布已更新 {dist_path}（{record['date']}，共 {record['total_items']} 条，"
              f"最高 {record['max_score']}，≥7.0 有 {record['count_ge_7']} 条）")
        for _col in column_alerts:
            print(f"栏目告警: {_col} 连续 {COLUMN_ZERO_ALERT_DAYS} 日入选为 0，"
                  f"请按取材表补源（R5）")
        _write_step_summary_alerts(column_alerts, quota_record)
    except Exception as exc:  # noqa: BLE001 - 分布是观测手段，不能拦主流程
        print(f"警告: 评分分布更新失败 ({exc})，hotspots.json 不受影响", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
