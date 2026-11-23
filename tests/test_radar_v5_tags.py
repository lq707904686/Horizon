"""v2 第八节（2026-11-23）雷达只打标：finance_redline / share_family，分数不动。"""
import importlib.util
import sys
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"
spec = importlib.util.spec_from_file_location("radar_v4", TOOLS_DIR / "radar_v4.py")
rv = importlib.util.module_from_spec(spec)
sys.modules["radar_v4"] = rv
spec.loader.exec_module(rv)


def _sets():
    return [{"topic": "巴西信贷吃掉购买力", "score": 6.5, "source_url": "u1"},
            {"topic": "68 岁老人被保健品骗走养老钱", "score": 7.5, "source_url": "u2"},
            {"topic": "1867 户的小区，物业走了", "score": 7.0, "source_url": "u3"}]


def test_tags_only_no_score_change():
    s = _sets()
    before = [x["score"] for x in s]
    rv.annotate_sets(s, "")
    assert [x["score"] for x in s] == before
    assert [x["finance_redline"] for x in s] == [True, False, False]
    assert [x["share_family"] for x in s] == [False, True, True]
    assert s[0]["video_fit"] == "低" and s[0]["tag_src"] == "rule-v5"


def test_finance_same_rule_as_changwen():
    p = Path.home() / "workspace/changwen-review2/changwen-e2e-pipeline-v3/scripts"
    if not p.is_dir():
        return
    sys.path.insert(0, str(p))
    import iteration_v5 as C
    for c in [("财报很好，股价不认", ""), ("黄仁勋走了", "市值蒸发，AMD 涨百分之十八，博通跌百分之八，市值少一万亿，利息"),
              ("涨价 697%，法院不信 AI 的借口", "涨了百分之六百九十七")]:
        assert rv.finance_hits(*c) == C.finance_hits(*c)


if __name__ == "__main__":
    test_tags_only_no_score_change()
    test_finance_same_rule_as_changwen()
    print("ALL PASS 2")
