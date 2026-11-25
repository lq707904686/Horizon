"""IP 契合度 ip_fit（2026-11-25 G1）：只并列展示，不改主分、不改排序。"""
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS_DIR = ROOT / "tools"
sys.path.insert(0, str(TOOLS_DIR))
for _name in ("radar_v4", "spread_score", "ip_fit_score"):
    _spec = importlib.util.spec_from_file_location(_name, TOOLS_DIR / f"{_name}.py")
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules[_name] = _mod
    _spec.loader.exec_module(_mod)
ipf = sys.modules["ip_fit_score"]
sp = sys.modules["spread_score"]
rv4 = sys.modules["radar_v4"]

HOLDOUT = json.loads((ROOT / "tests" / "fixtures" / "ip_fit_holdout_20261125.json").read_text(encoding="utf-8"))["rows"]
VALID = ("高", "中")

BRIEF = """> 从 3 条内容中筛选出 2 条重要资讯。

1. [Anthropic IPO 股权造富：25 位华人接住期权核弹](#item-renxing-1) ⭐️ 5.25/10
2. [主播私闯果园糟蹋千斤苹果仅愿赔 6 千元](#item-renxing-2) ⭐️ 5.0/10

---

### [Anthropic IPO 股权造富：25 位华人接住期权核弹](https://a.example/1) ⭐️ 5.25/10 🎬️ 6.5/12 · 可视化可行（33数/5模板）
双评：5.5 + 7（dual）

据报道，Anthropic 登陆纳斯达克，发行价 83 美元。

### [主播私闯果园糟蹋千斤苹果仅愿赔 6 千元](https://a.example/2) ⭐️ 5.0/10 🎬️ 10.0/12 · 可视化可行（3数/2模板）

果农称损失上万元，主播只愿赔 6 千元。母题：贪
"""


def test_holdout_binary_accuracy_at_least_80():
    hit = sum((ipf.ip_fit(r["title"])["ip_fit"] in VALID) == (r["human"] in VALID) for r in HOLDOUT)
    assert len(HOLDOUT) == 100
    assert hit >= 80, hit


def test_port_matches_local_ip_fit_v1_row_by_row():
    # 移植逐字：与 动态监测/ip_fit.py 冻结时的规则判逐条一致
    diff = [r["title"] for r in HOLDOUT if ipf.ip_fit(r["title"])["ip_fit"] != r["rule_v1"]]
    assert diff == []


def test_levels_and_fields():
    r = ipf.ip_fit("35岁被裁的他，靠摆摊月入2万")
    assert r["ip_fit"] == "高" and "人" in r["ip_fit_hits"] and r["ip_fit_src"] == "ip-fit-v1"
    assert ipf.ip_fit("AI芯片早报：英伟达发布新GPU")["ip_fit"] == "无"
    assert all(":" not in h and "：" not in h for h in r["ip_fit_hits"])


def test_annotate_sets_does_not_touch_score_or_order():
    sets = [{"topic": "AI芯片早报：英伟达发布新GPU", "score": 7.5, "spread_score": 1.0},
            {"topic": "主播私闯果园糟蹋千斤苹果仅愿赔 6 千元", "score": 7.0}]
    before = [(s["topic"], s["score"], s.get("spread_score")) for s in sets]
    ipf.annotate_sets(sets)
    assert [(s["topic"], s["score"], s.get("spread_score")) for s in sets] == before
    assert sets[0]["ip_fit"] == "无" and sets[1]["ip_fit"] in VALID


def test_brief_line_idempotent_and_spread_still_rerunnable():
    once = ipf.annotate_brief_text(sp.annotate_brief_text(BRIEF))
    assert ipf.annotate_brief_text(once) == once  # 幂等
    assert once.count("> \U0001f9ed IP 契合") == 2
    for h, _b in rv4.brief_sections(once).values():  # 等级标记与规则一致（「无」= 🔴）
        r = ipf.ip_fit(h)
        assert f"IP 契合 {ipf.LEVEL_MARK[r['ip_fit']]} {r['ip_fit']}" in once
    assert ipf.LEVEL_MARK["无"] == "\U0001f534"
    # 传播分重跑不重复、不被 IP 行干扰
    assert sp.annotate_brief_text(once) == once
    # 标题行、⭐ 分数、母题解析不受影响
    secs0, secs1 = rv4.brief_sections(sp.annotate_brief_text(BRIEF)), rv4.brief_sections(once)
    assert [v[0] for v in secs0.values()] == [v[0] for v in secs1.values()]
    assert [rv4.motifs_of(v[1]) for v in secs1.values()] == [rv4.motifs_of(v[1]) for v in secs0.values()]
    assert [sp.summary_text(v[1]) for v in secs1.values()] == [sp.summary_text(v[1]) for v in secs0.values()]
