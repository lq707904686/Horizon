"""v3 雷达落地（2026-11-23）：L7 热榜补全文 / L8 简报版本守护 / L12 48 小时口径 / L13 分渠道传播分 / L3 四值 topic_type。"""
import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace as NS

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
sys.path.insert(0, str(TOOLS))


def _load(name):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


pl = _load("person_lexicon")
rv4 = _load("radar_v4")
sp = _load("spread_score")
bg = _load("brief_guard")
bf = _load("backfill_reads")
mh = _load("make_hotspots")
from src.processing import fulltext_fill as ff  # noqa: E402

LONG = ("百万粉丝主播被指私闯果园带货糟蹋苹果，果农称千斤苹果被糟蹋，主播仅愿赔偿 6 千元。据当地乡镇通报，"
        + "主播误入相邻果园，摘下苹果拍摄带货，果农陈先生称损失约 181 斤。" * 12)


def _it(i, title, content, cat="社会", feed="澎湃·热闻"):
    return NS(id=i, title=title, content=content, url=f"https://x/{i}",
              metadata={"category": cat, "feed_name": feed})


# ---------------- L7 ----------------
def test_l7_fills_hotlist_from_local_pool_without_network(monkeypatch):
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("不许联网")))
    hot = _it("h1", "主播私闯果园糟蹋千斤苹果仅愿赔 6 千元", "热度 198 万", cat="热榜", feed="头条热榜")
    full = _it("f1", "百万粉丝主播被指私闯果园带货糟蹋苹果？官方通报：系误入相邻果园，已赔偿并道歉", LONG)
    other = _it("f2", "新一轮冷空气将影响广东 全省气温继续下降", "冷空气" * 200, feed="中新网·社会")
    stats = ff.fill_hotlist_fulltext([hot, full, other])
    assert stats["filled"] == 1 and stats["filled_hotlist"] == 1
    assert ff.FILL_MARKER in hot.content and "181 斤" in hot.content
    assert hot.metadata["fulltext_fill"]["src"] == "澎湃·热闻"
    # 补完后 analyzer 口径的正文 ≥300 字 → 不再触发 P318-2 标题级封顶
    from src.processing.content import split_content
    assert len(split_content(hot.content).main) >= ff.MIN_FULLTEXT_CHARS
    assert full.content == LONG  # 全文池条目不被改


def test_l7_no_false_match_and_comments_kept():
    hot = _it("h1", "侠盗猎车手6 第一集，GTA 6 Part 1", "B站视频\n--- Top Comments ---\n好看", cat="热榜")
    digest = _it("f1", "8点1氪丨黄仁勋遭董事会罢免主要原因曝光；PS5 Pro涨至1.6万元", "GTA 6 侠盗猎车手 第一集 " * 40)
    assert ff.fill_hotlist_fulltext([hot, digest])["filled"] == 0
    hot2 = _it("h2", "主播私闯果园糟蹋千斤苹果仅愿赔 6 千元", "短\n--- Top Comments ---\n评论A", cat="热榜")
    full = _it("f2", "百万粉丝主播被指私闯果园带货糟蹋苹果？官方通报：系误入相邻果园，已赔偿并道歉", LONG)
    ff.fill_hotlist_fulltext([hot2, full])
    from src.processing.content import split_content
    parts = split_content(hot2.content)
    assert "181 斤" in parts.main and parts.comments == "评论A"


def test_l7_cap_rule_unchanged():
    from src.ai import analyzer
    assert analyzer.ContentAnalyzer.TITLE_ONLY_MAX_CONTENT_CHARS == 300 == ff.MIN_FULLTEXT_CHARS


# ---------------- L8 ----------------
BRIEF_GOOD = """> 从 5 条内容中筛选出 2 条重要资讯。

### [果园带货翻车](https://a/1) ⭐️ 8.25/10

正文

**「热点选题」** \\`\\`\\`hotspots
\\- 切 1000 斤苹果只赔 6000
\\`\\`\\`

### [求职陪跑](https://a/2) ⭐️ 7.25/10

正文

**「热点选题」** \\`\\`\\`hotspots
\\- 花 1 万 2 买情绪价值
\\`\\`\\`
"""
BRIEF_BAD = """> 从 3 条内容中筛选出 1 条重要资讯。

### [果园主播](https://a/1) ⭐️ 5.0/10

正文

**「热点选题」** \\`\\`\\`hotspots
\\- 主播糟蹋千斤苹果
\\`\\`\\`
"""


def test_l8_worse_kept_better_overwritten():
    good = bg.stats_of(BRIEF_GOOD, mh.extract_scored_topics)
    bad = bg.stats_of(BRIEF_BAD, mh.extract_scored_topics)
    assert good == {"items": 2, "ge7": 2, "max": 8.25} and bad["ge7"] == 0
    d = bg.decide(bad, BRIEF_GOOD, mh.extract_scored_topics)
    assert d["action"] == "keep" and d["kept"] == 1 and d["gen"] == 2
    d2 = bg.decide(good, BRIEF_BAD, mh.extract_scored_topics)   # 11-23：0 → 10 条，允许覆盖
    assert d2["action"] == "overwrite" and d2["version"] == 2 and d2["overwrites"] == 1
    assert bg.decide(good, None, mh.extract_scored_topics)["action"] == "first"
    # 相等不算更差；≥7.0 少但最高分更高也不算更差
    assert not bg.is_worse({"items": 2, "ge7": 2, "max": 8.25}, good)
    assert not bg.is_worse({"items": 1, "ge7": 1, "max": 9.0}, good)


def test_l8_footer_roundtrip_and_idempotent(tmp_path):
    p = tmp_path / "2026-11-23-summary-zh.md"
    p.write_text(BRIEF_GOOD, encoding="utf-8")
    d = bg.decide(bg.stats_of(BRIEF_GOOD, mh.extract_scored_topics), None, mh.extract_scored_topics)
    bg.write_footer(str(p), "2026-11-23", d, commit="abcdef1234")
    bg.write_footer(str(p), "2026-11-23", d, commit="abcdef1234")
    txt = p.read_text(encoding="utf-8")
    assert txt.count("> 简报版本：") == 1 and "v1｜当天第 1 次生成" in txt and "代码 abcdef1" in txt
    st = bg.read_state(txt)
    assert st["gen"] == 1 and st["version"] == 1
    # 版本行不改变分数抽取
    assert bg.stats_of(txt, mh.extract_scored_topics) == bg.stats_of(BRIEF_GOOD, mh.extract_scored_topics)
    d2 = bg.decide(bg.stats_of(BRIEF_BAD, mh.extract_scored_topics), txt, mh.extract_scored_topics)
    assert d2["gen"] == 2 and d2["action"] == "keep"


def test_l8_make_hotspots_keeps_published_when_worse(tmp_path, monkeypatch):
    """端到端：本地 bare 仓当 origin，gh-pages 上有好简报；本轮生成差简报 → 不覆盖。"""
    import datetime
    day = datetime.date.today().isoformat()
    g = lambda *a, cwd: subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True)  # noqa: E731
    origin = tmp_path / "origin.git"
    g("init", "-q", "--bare", str(origin), cwd=tmp_path)
    pub = tmp_path / "pub"
    pub.mkdir()
    g("init", "-q", cwd=pub)
    g("checkout", "-q", "-b", "gh-pages", cwd=pub)
    (pub / "_posts").mkdir()
    (pub / "_posts" / f"{day}-summary-zh.md").write_text(BRIEF_GOOD, encoding="utf-8")
    g("add", ".", cwd=pub)
    g("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "p", cwd=pub)
    g("push", "-q", str(origin), "gh-pages", cwd=pub)
    work = tmp_path / "work"
    (work / "data" / "summaries").mkdir(parents=True)
    (work / "docs" / "_posts").mkdir(parents=True)
    g("init", "-q", cwd=work)
    g("remote", "add", "origin", str(origin), cwd=work)
    (work / "data" / "summaries" / f"horizon-{day}.md").write_text(BRIEF_BAD, encoding="utf-8")
    (work / "docs" / "_posts" / f"{day}-summary-zh.md").write_text(BRIEF_BAD, encoding="utf-8")
    (work / "data" / "score_distribution.json").write_text('{"records": []}', encoding="utf-8")
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    out = work / "hotspots.json"
    monkeypatch.setattr(sys, "argv", ["mh", "--summaries", str(work / "data" / "summaries"), "--out", str(out)])
    assert mh.main() == 0
    hs = json.loads(out.read_text(encoding="utf-8"))
    assert max(s["score"] for s in hs["sets"]) == 8.25
    post = (work / "docs" / "_posts" / f"{day}-summary-zh.md").read_text(encoding="utf-8")
    assert "果园带货翻车" in post and "拦下 1 次" in post
    dist = json.loads((work / "data" / "score_distribution.json").read_text(encoding="utf-8"))
    assert dist["records"][-1]["count_ge_7"] == 2 and dist["brief_runs"][day]["kept"] == 1


def test_l8_update_history_default_still_keep_best(tmp_path):
    sd = _load("score_distribution")
    p = tmp_path / "d.json"
    good = {"date": "2026-11-23", "total_items": 15, "count_ge_7": 10, "max_score": 8.25}
    bad = {"date": "2026-11-23", "total_items": 10, "count_ge_7": 0, "max_score": 6.5}
    sd.update_history(str(p), good)
    sd.update_history(str(p), bad)
    assert json.loads(p.read_text())["records"][0]["count_ge_7"] == 10
    sd.update_history(str(p), bad, final=True)
    assert json.loads(p.read_text())["records"][0]["count_ge_7"] == 0


# ---------------- L12 ----------------
def test_l12_stamp_window():
    r = bf.stamp_window({"date": "2026-11-22"}, "2026-11-23T08:00")
    assert r["window"] == "发布后48小时" and r["unstable"] is True and r["obs_lag_days"] == 1
    assert bf.stamp_window({"date": "2026-11-21"}, "2026-11-23T08:00")["unstable"] is False
    assert bf.stamp_window({"date": ""}, "2026-11-23T08:00")["unstable"] is None


def test_l12_merge_keeps_old_rows_and_freezes_stable(tmp_path):
    p = tmp_path / "a.jsonl"
    old = {"key": "k-old", "as_of": "2026-11-22T22:19", "reads": 3}
    stable = {"key": "k-s", "as_of": "2026-11-23T08:00", "reads": 9, "window": "发布后48小时", "unstable": False}
    p.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in (old, stable)) + "\n", encoding="utf-8")
    new = [dict(old, as_of="2026-11-25T08:00", reads=99, window="发布后48小时", unstable=False),
           dict(stable, as_of="2026-11-25T08:00", reads=50)]
    bf.merge(str(p), new)
    rows = {json.loads(x)["key"]: json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()}
    assert rows["k-old"] == old and rows["k-s"]["reads"] == 9


def test_l12_diagnose_skips_unstable():
    base = [{"kind": "wx", "radar_score": 7.5, "reads": 10, "date": "2026-11-20"}] * 3
    unst = [{"kind": "wx", "radar_score": 7.5, "reads": 9999, "date": "2026-11-22", "unstable": True}] * 5
    from datetime import date
    assert rv4.diagnose(base, today=date(2026, 11, 23)) == rv4.diagnose(base + unst, today=date(2026, 11, 23))


# ---------------- L13 ----------------
def test_l13_channel_scores_and_spread_v1_unchanged():
    t = "女主播没赚到钱反被索赔十万元"
    v, ti, parts = sp.channel_scores(t)
    assert parts["money"] == 3.0 and ti >= 6
    assert 0 <= v <= 10 and 0 <= ti <= 10
    assert sp.channel_scores("Token 工厂大爆发选题")[:2] == (0.0, 0.0)
    sets = [{"source": "https://a/1", "topic": "x", "score": 8.0}]
    md = "### [主播私闯果园糟蹋千斤苹果仅愿赔 6 千元](https://a/1) ⭐️ 8.0/10\n\n正文\n"
    sp.annotate_sets(sets, md)
    s = sets[0]
    assert s["spread_src"] == "spread-v1" and set(s["spread_parts"]) == {"person", "hook", "family", "audience"}
    assert s["spread_channel"]["src"] == "spread-ch-v1" and "spread_video" in s and "spread_tietu" in s


def test_l13_brief_line_keeps_changwen_regex_and_idempotent():
    import re
    md = "### [主播私闯果园糟蹋千斤苹果仅愿赔 6 千元](https://a/1) ⭐️ 8.0/10 🎬️ 10.0/12\n\n正文 6000 元\n"
    a = sp.annotate_brief_text(md)
    assert sp.annotate_brief_text(a) == a
    # 长文 _run_horizon.py 的两条正则（⭐ 原分、📣 传播分）照样能读
    star = re.compile(r"⭐\ufe0f?\s*([\d.]+)/10")
    spread = re.compile(r"\U0001f4e3\s*传播\s*([\d.]+)/10（人([\d.]+)\s*开头([\d.]+)\s*家人([\d.]+)\s*40\+([\d.]+)）")
    assert star.search(a).group(1) == "8.0" and spread.search(a) and "分渠道 视频" in a


# ---------------- L3/L4 ----------------
def test_l3_lexicon_bytes_identical_and_topic_type_4_values():
    h = hashlib.sha256((TOOLS / "person_lexicon.json").read_bytes()).hexdigest()
    assert h == "2a5248be94807010355ed302bd8c3373638122c63f5302c119eea657ef754127"
    assert rv4.TOPIC_TYPES == ("人/处境", "公司/资本", "政策/制度", "行业冷账")
    assert rv4.topic_type("", headline="54岁副教授被裁") == "人/处境"
    assert rv4.topic_type("", headline="财报很好，股价不认") == "公司/资本"
    assert rv4.topic_type("", headline="延迟退休新规出台") == "政策/制度"
    assert rv4.topic_type("", headline="光伏组件价格跌穿成本") == "行业冷账"
    assert not pl.has_person("你月供两万二")  # exclude：「你」不算人
    sets = [{"source": "https://a/1", "topic": "x", "score": 7.0}]
    rv4.annotate_sets(sets, "### [陈先生的果园](https://a/1) ⭐️ 7.0/10\n\n正文\n", [], [])
    assert sets[0]["topic_type"] in rv4.TOPIC_TYPES and sets[0]["topic_type_legacy"] in ("人/处境", "公司/资本", "其他")
    assert sets[0]["topic_type_src"] == "tt4-v1"
