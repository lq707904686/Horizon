"""Tests for P318-6 / P318-7 in tools/fetch_hotlists.py.

  - P318-6: a 0-item source writes an empty file (not a valid RSS), so
    the workflow's BUG-1 check (test -s) can actually catch a dead source.
  - P318-7: pubDate is the first-seen date, not now; the seen state
    round-trips through data/hotlists_seen.json and prunes after 30 days.
"""
import datetime
import importlib.util
import sys
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"


def _load_tool_module(name):
    spec = importlib.util.spec_from_file_location(name, TOOLS_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fetch_hotlists = _load_tool_module("fetch_hotlists")


def _item(guid, title="标题"):
    return {"title": title, "link": "https://example.com/x",
            "desc": "desc", "guid": guid}


def test_write_rss_empty_writes_empty_file(tmp_path):
    path = tmp_path / "baidu.xml"
    fetch_hotlists.write_rss([], "百度热搜", str(path))
    assert path.exists()
    assert path.stat().st_size == 0


def test_write_rss_uses_first_seen_pubdate(tmp_path):
    path = tmp_path / "baidu.xml"
    items = [
        dict(_item("baidu-a", "旧条目"),
             first_seen=datetime.date(2026, 11, 10)),
        _item("baidu-b", "新条目"),
    ]
    fetch_hotlists.write_rss(items, "百度热搜", str(path))
    text = path.read_text(encoding="utf-8")
    assert text.count("10 Nov 2026") == 1
    assert "baidu-a" in text and "baidu-b" in text


def test_apply_first_seen_assigns_and_keeps():
    today = datetime.date(2026, 11, 16)
    seen = {}
    items, seen = fetch_hotlists.apply_first_seen(
        [_item("baidu-a")], seen, today)
    assert seen == {"baidu-a": "2026-11-16"}
    assert items[0]["first_seen"] == today
    later = datetime.date(2026, 11, 17)
    items, seen = fetch_hotlists.apply_first_seen(
        [_item("baidu-a")], seen, later)
    assert seen == {"baidu-a": "2026-11-16"}
    assert items[0]["first_seen"] == datetime.date(2026, 11, 16)


def test_prune_seen_drops_stale_not_live():
    today = datetime.date(2026, 11, 16)
    seen = {
        "old-gone": "2026-10-01",   # 46 days ago, not live -> drop
        "old-live": "2026-10-01",   # 46 days ago, still live -> keep
        "recent-gone": "2026-11-10",  # 6 days ago -> keep
    }
    kept = fetch_hotlists.prune_seen(seen, {"old-live"}, today)
    assert set(kept) == {"old-live", "recent-gone"}


def test_load_save_seen_roundtrip_and_corrupt(tmp_path):
    path = tmp_path / "hotlists_seen.json"
    assert fetch_hotlists.load_seen(str(path)) == {}
    fetch_hotlists.save_seen(str(path), {"baidu-a": "2026-11-16"})
    assert fetch_hotlists.load_seen(str(path)) == {"baidu-a": "2026-11-16"}
    path.write_text("{not json", encoding="utf-8")
    assert fetch_hotlists.load_seen(str(path)) == {}


def test_main_writes_rss_and_seen_state(monkeypatch, tmp_path):
    """Full main() with stubbed fetchers: RSS files carry first-seen
    pubDates and the seen state lands next to the out dir."""
    monkeypatch.setattr(fetch_hotlists, "fetch_baidu",
                        lambda: [_item("baidu-a", "百度条目")])
    monkeypatch.setattr(fetch_hotlists, "fetch_toutiao", lambda: [])
    monkeypatch.setattr(fetch_hotlists, "fetch_tophub", lambda: {})
    out_dir = tmp_path / "data" / "hotlists"
    monkeypatch.setattr(sys, "argv", ["fetch_hotlists.py",
                                      "--out-dir", str(out_dir)])
    fetch_hotlists.main()
    baidu_xml = (out_dir / "baidu.xml").read_text(encoding="utf-8")
    assert "百度条目" in baidu_xml
    # P318-6: the empty Toutiao source leaves an empty file.
    assert (out_dir / "toutiao.xml").stat().st_size == 0
    seen_path = tmp_path / "data" / "hotlists_seen.json"
    seen = fetch_hotlists.load_seen(str(seen_path))
    assert seen == {"baidu-a": datetime.date.today().isoformat()}
