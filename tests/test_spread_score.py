"""传播分 spread_score（2026-11-23 雷达迭代）：只新增并列字段，原分与过滤不动。"""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS_DIR = ROOT / "tools"
sys.path.insert(0, str(TOOLS_DIR))
for _name in ("radar_v4", "spread_score"):
    _spec = importlib.util.spec_from_file_location(_name, TOOLS_DIR / f"{_name}.py")
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules[_name] = _mod
    _spec.loader.exec_module(_mod)
sp = sys.modules["spread_score"]

BRIEF = """> 从 3 条内容中筛选出 2 条重要资讯。

1. [Anthropic IPO 股权造富：25 位华人接住期权核弹](#item-renxing-1) ⭐️ 5.25/10
2. [主播私闯果园糟蹋千斤苹果仅愿赔 6 千元](#item-renxing-2) ⭐️ 5.0/10

---

### [Anthropic IPO 股权造富：25 位华人接住期权核弹](https://a.example/1) ⭐️ 5.25/10 🎬️ 6.5/12 · 可视化可行（33数/5模板）
双评：5.5 + 7（dual）

据报道，Anthropic 登陆纳斯达克，发行价 83 美元。

### [主播私闯果园糟蹋千斤苹果仅愿赔 6 千元](https://a.example/2) ⭐️ 5.0/10 🎬️ 10.0/12 · 可视化可行（3数/2模板）

果农称损失上万元，主播只愿赔 6 千元。
"""


def test_dimensions_and_range():
    p = sp.spread_parts("主播私闯果园糟蹋千斤苹果仅愿赔 6 千元")
    assert p["person"] == 3 and p["hook"] == 3
    assert sp.spread_score("Anthropic IPO 股权造富") <= 1.0  # 纯公司/资本：人记 0
    assert sp.spread_score("小区 277 户只有约 100 家一直交物业费") == 10.0
    for t in ("", "手账成年轻人氪金新赛道", "68 岁老人被保健品骗走养老钱，子女起诉"):
        assert 0 <= sp.spread_score(t) <= 10


def test_body_counts_half():
    assert sp.spread_parts("一个现象", "一位 52 岁的保安说")["person"] == 1.5


def test_annotate_sets_only_adds_keys():
    sets = [{"topic": "果园赔偿", "score": 5.0, "source_url": "https://a.example/2"}]
    before = dict(sets[0])
    sp.annotate_sets(sets, BRIEF)
    for k, v in before.items():
        assert sets[0][k] == v
    assert sets[0]["spread_src"] == sp.VERSION
    assert set(sets[0]["spread_parts"]) == {"person", "hook", "family", "audience"}
    assert sets[0]["spread_score"] >= 6


def test_brief_annotation_keeps_star_parse_and_idempotent():
    out = sp.annotate_brief_text(BRIEF)
    assert out == sp.annotate_brief_text(out)  # 幂等
    assert out.count("📣") == 4  # 目录 2 行 + 标题 2 行
    # ⭐ 分数仍是标题行里第一个 x/10（make_hotspots / backfill_reads 正则不受影响）
    import re
    star = re.compile(r"⭐\uFE0F?\s*(\d+(?:\.\d+)?)\s*/\s*10")
    assert [m.group(1) for m in star.finditer(out)] == ["5.25", "5.0", "5.25", "5.0"]
    toc = re.compile(r"^(\d+)\. \[(.+?)\]\(#item-renxing-\d+\) ⭐️ ([\d.]+)/10", re.M)
    assert len(toc.findall(out)) == 2
    # 除新增片段与行尾外，正文逐字不变
    stripped = re.sub(r" · 📣[^\n]*", "", out)
    assert stripped == BRIEF


def test_backtest_script_runs(tmp_path):
    out = tmp_path / "bt.json"
    subprocess.run([sys.executable, str(ROOT / "scripts" / "backtest_spread.py"), "--out", str(out)],
                   check=True, capture_output=True)
    d = json.loads(out.read_text(encoding="utf-8"))
    assert d["version"] == sp.VERSION
    assert {g["group"] for g in d["groups"]} >= {"视频号播放·全期", "公众号贴图阅读·推荐期"}
    assert "switch_main" in d["decision"]
