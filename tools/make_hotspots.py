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
_SOURCE_RX = re.compile(r"^(?:rss|tophub|newsnow|google|百度|头条|rsshub)\s*·\s*([^\n·]+)", re.M)
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
    """从正文首个“归属：X”取 5 真分类, 提不到返回 None。"""
    m = _CATEGORY_RX.search(body)
    return m.group(1).strip() if m else None


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
    [topics]), ...], 每项对应简报里的一个条目。"""
    with open(md_path, encoding="utf-8") as f:
        text = _unescape(f.read())
    scored = []
    for heading, body in _split_sections(text):
        topics = _block_topics(body)
        if not topics:
            continue
        feasibility, feasibility_reason = _extract_video_feasibility(heading)
        scored.append((_extract_score(heading), _extract_video_score(heading),
                        _extract_category(body), _extract_source(body),
                        _extract_url(heading), _extract_cap(body),
                        feasibility, feasibility_reason, topics))
    return scored


def legacy_extract(md_path):
    """旧行为: 不分条目, 全量抽取 (仅当简报里完全没有可解析分数时回退用)。"""
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
    for score, _v, category, _src, _u, _cap, _f, _fr, _t in scored:
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
    source_url, source_name, verified, cap_triggered}。
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
    with_score = [(s, v, c, src, u, cap, feas, feas_reason, t)
                  for s, v, c, src, u, cap, feas, feas_reason, t in scored
                  if s is not None]
    if not with_score:
        return None, None, stats  # 调用方回退旧行为

    tiers = sorted({s for s, *_ in with_score}, reverse=True)
    line = MIN_SCORE
    if all(s < MIN_SCORE for s in tiers):
        line = tiers[0]  # 当日无过线条目: 回退到最高分档
        stats["fallback_score"] = line

    kept = []  # (score, video_score, category, source, url, cap, feasibility, reason, order, topic)
    order = 0
    fallback_kept = False  # P315 R8：干旱日回退是否已取过最高分档第 1 个条目
    for score, video_score, category, source, url, cap, feas, feas_reason, topics in with_score:
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
                         feas, feas_reason, order, topic))
            order += 1

    seen = set()
    topics = []
    sets = []
    for score, video_score, category, source, url, cap, feas, feas_reason, _, topic in sorted(
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
        })
    return topics, sets, stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--summaries", required=True, help="Horizon 简报目录 (data/summaries)")
    ap.add_argument("--out", required=True, help="输出的 hotspots.json 路径")
    args = ap.parse_args()

    files = sorted(glob.glob(os.path.join(args.summaries, "horizon-*.md")))
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
            # 简报里没有任何可解析的条目分数 (格式变了): 回退旧行为, 不静默丢数据
            print("警告: 简报中未解析到任何条目分数, 回退为全量抽取 (请检查简报格式)",
                  file=sys.stderr)
            topics = legacy_extract(latest)
            sets = [{"topic": t, "score": None, "video_score": None,
                     "video_feasibility": None,
                     "video_feasibility_reason": None,
                     "category_5": None, "source": None, "source_url": None,
                     "source_name": None, "verified": None,
                     "cap_triggered": None} for t in topics]
            # BUG-2 已修：回退时 min_score 不再改写为 0.0。
            # 无分数可滤时 min_score 仍为 MIN_SCORE，fallback_score 保持 None，
            # 下游以“topics 全量、min_score 7.0”识别这是一次格式回退。
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
                    {"source": _s, "count": _n, "share": round(_p, 4)}
                    for _s, _n, _p in kept_shares],
                "source_quota_warn": sorted(quota_warn),
            }
            # ★ P315 R5：五栏目分流统计（当日入选数 / ≥7.0 数）。
            column_record = column_stats(scored, sets)
            for _col in list(COLUMNS_5) + ["其他"]:
                _cs = column_record[_col]
                print(f"栏目分流（{_col}）: 简报 {_cs['items']} 条、"
                      f"≥7.0 {_cs['ge_7']} 条、入选 {_cs['selected']} 个话题")

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
        record = build_distribution(scored, md_path=latest if files else None)
        # ★ P315 R4/R5：配额与栏目统计随当日分布记录一并留存（该文件已由
        #   workflow 提交回仓库，栏目连续零入选告警靠它跨日比对，不另建文件）。
        if quota_record is not None:
            record["kept_source_shares"] = quota_record["kept_source_shares"]
            record["source_quota_warn"] = quota_record["source_quota_warn"]
        if column_record is not None:
            record["per_column"] = column_record
        update_history(dist_path, record)
        print(f"评分分布已更新 {dist_path}（{record['date']}，共 {record['total_items']} 条，"
              f"最高 {record['max_score']}，≥7.0 有 {record['count_ge_7']} 条）")
        if column_record is not None:
            try:
                _alert_day = datetime.date.fromisoformat(record["date"])
            except ValueError:
                _alert_day = None
            if _alert_day is not None:
                for _col in column_zero_alerts(
                        _read_history_records(dist_path), today=_alert_day):
                    print(f"栏目告警: {_col} 连续 {COLUMN_ZERO_ALERT_DAYS} 日入选为 0，"
                          f"请按取材表补源（R5）")
    except Exception as exc:  # noqa: BLE001 - 分布是观测手段，不能拦主流程
        print(f"警告: 评分分布更新失败 ({exc})，hotspots.json 不受影响", file=sys.stderr)


if __name__ == "__main__":
    main()
