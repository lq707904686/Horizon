"""R-P0-1 / R-P0-2 / R-P1-1 / R-P1-2（四线迭代 v4，2026-11-23）。"""
import csv
import importlib.util
import json
import sys
from datetime import date
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, TOOLS_DIR / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


bf = _load("backfill_reads")
rv4 = _load("radar_v4")


def _write_csv(path, header, rows):
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def _changwen(tmp_path):
    rd = tmp_path / "cw" / "第052轮_1121_Horizon选题雷达"
    ed = rd / "条目" / "2928"
    ed.mkdir(parents=True)
    (ed / "03_标题与标签.json").write_text(json.dumps({"titles": {"main": "平台开食堂，你只剩交租"}}, ensure_ascii=False), encoding="utf-8")
    (rd / "_ID台账.json").write_text(json.dumps({"entries": [{"id": "2928", "score": 7.0}]}), encoding="utf-8")
    return tmp_path / "cw"


def test_backfill_matched_and_unmatched(tmp_path):
    cw = _changwen(tmp_path)
    wx = tmp_path / "wx.csv"
    _write_csv(wx, ["序号", "日期", "时间", "类型", "标题", "阅读", "点赞", "分享", "在看", "留言", "状态", "原创", "已修改"], [
        [1, "2026-11-21", "12:25", "贴图", "平台开食堂，你只剩交租", 5, 0, 0, 0, 0, "已发表", "False", "False"],
        [2, "2026-11-20", "08:00", "文章", "一个仓里根本没有的标题完全对不上", 3, 0, 0, 0, 0, "已发表", "True", "False"],
    ])
    ch = tmp_path / "ch.csv"
    _write_csv(ch, ["ch", "date", "title", "n", "pct"], [["推荐", "20261121", "平台开食堂，你只剩交租", 4, 0.1]])
    sph = tmp_path / "sph.csv"
    _write_csv(sph, ["视频描述", "视频ID", "发布时间", "完播率", "平均播放时长", "播放量", "推荐", "喜欢", "评论量", "分享量", "关注量"], [
        ["平台开食堂 你只剩交租\n\n正文", "export/abc", "2026/11/22", "9.65%", "19.51秒", "777", "1", "1", "0", "1", "2"],
    ])
    reg = tmp_path / "reg.json"
    reg.write_text(json.dumps({"entries": []}), encoding="utf-8")
    out = tmp_path / "act.jsonl"
    args = ["--registry", str(reg), "--changwen", str(cw), "--wx-csv", str(wx), "--wx-ch-csv", str(ch),
            "--sph-csv", str(sph), "--out", str(out), "--as-of", "2026-11-22T22:19", "--repo", str(tmp_path)]
    assert bf.main(args) == 0
    rows = [json.loads(x) for x in out.read_text(encoding="utf-8").splitlines()]
    by = {r["title"]: r for r in rows}
    hit = by["平台开食堂，你只剩交租"]
    assert hit["eid"] == "2928" and hit["radar_score"] == 7.0 and hit["reads"] == 5 and hit["rec"] == 4
    miss = by["一个仓里根本没有的标题完全对不上"]
    assert "eid" not in miss and "eid" in miss["unmatched"] and "radar_score" in miss["unmatched"]
    assert miss["rec"] is None  # 无渠道行 = 未知，不写 0
    v = [r for r in rows if r["kind"] == "video"][0]
    assert v["eid"] == "2928" and v["plays"] == 777 and v["completion_pct"] == 9.65 and v["avg_watch_s"] == 19.51
    assert v["date"] == "2026-11-22" and v["followers"] == 2
    # 合并：重跑不重复。v3 L12（48 小时口径）：as_of 2026-11-22 时 11-20 那条已满 2 天 → 稳定、冻结，
    #   不再被 11-23 的导出覆盖；11-21/11-22 两条未稳 → 被新 as_of 覆盖。
    assert by["一个仓里根本没有的标题完全对不上"]["unstable"] is False and hit["unstable"] is True
    assert bf.main(args[:-4] + ["--as-of", "2026-11-23T08:00", "--repo", str(tmp_path)]) == 0
    rows2 = [json.loads(x) for x in out.read_text(encoding="utf-8").splitlines()]
    by2 = {r["key"]: r for r in rows2}
    assert len(rows2) == len(rows)
    for r in rows:
        exp = "2026-11-22T22:19" if r["unstable"] is False else "2026-11-23T08:00"
        assert by2[r["key"]]["as_of"] == exp


def test_diagnose_medians():
    acts = [
        {"kind": "article", "date": "2026-11-20", "radar_score": 7.5, "reads": 10},
        {"kind": "article", "date": "2026-11-20", "radar_score": 7.0, "reads": 2},
        {"kind": "article", "date": "2026-11-21", "radar_score": 6.0, "reads": 3},
        {"kind": "article", "date": "2026-09-01", "radar_score": 9.0, "reads": 999},  # 窗口外
        {"kind": "video", "date": "2026-11-21", "radar_score": 6.5, "plays": 700},
    ]
    d = rv4.diagnose(acts, today=date(2026, 11, 23))
    assert d["article"]["n_ge7"] == 2 and d["article"]["median_ge7"] == 6
    assert d["article"]["median_lt7"] == 3 and d["video"]["median_lt7"] == 700 and d["video"]["n_ge7"] == 0
    assert "只诊断不改分" in rv4.diag_line(d)


def test_tags_and_repeat_hint():
    assert rv4.topic_type("", headline="农民工称被国企克扣工资：发工资凭运气") == "人/处境"
    assert rv4.topic_type("", headline="Anthropic 2 万亿 IPO 造富") == "公司/资本"
    # v3 L3/L4：topic_type 扩为 4 值，旧「其他」兜底改为「行业冷账」；旧口径留在 topic_type_legacy
    assert rv4.topic_type("", headline="户均 2.52 人，开发商却在卖三居") == "行业冷账"
    assert rv4.topic_type_legacy("", headline="户均 2.52 人，开发商却在卖三居") == "其他"
    assert rv4.video_fit("54 岁副教授，两个聘期没达标") == "高"
    assert rv4.video_fit("新能源汽车们进入华容道") == "低"
    h = rv4.repeat_hint("x", ["止损"], [], ["止损", "止损", "止损"])
    assert h and "止损" in h
    assert rv4.repeat_hint("x", ["止损"], [], ["止损", "止损"]) is None


def test_annotate_sets_does_not_touch_scores():
    md = "### [农民工称被国企克扣工资](https://e.com/a) ⭐️ 7.0/10\n母题：止损；\n"
    sets = [{"topic": "克扣工资", "score": 7.0, "source_url": "https://e.com/a"}]
    rv4.annotate_sets(sets, md, [], [], today=date(2026, 11, 23))
    s = sets[0]
    assert s["score"] == 7.0 and s["topic_type"] == "人/处境" and s["motifs"] == ["止损"] and s["repeat_hint"] is None


def test_append_brief_tail_idempotent(tmp_path):
    p = tmp_path / "2026-11-23-summary-zh.md"
    p.write_text("正文\n", encoding="utf-8")
    rv4.append_brief_tail(str(tmp_path), "2026-11-23", ["行1"])
    rv4.append_brief_tail(str(tmp_path), "2026-11-23", ["行2"])
    t = p.read_text(encoding="utf-8")
    assert t.count(rv4.DIAG_MARKER) == 1 and "行2" in t and "行1" not in t and t.startswith("正文")
