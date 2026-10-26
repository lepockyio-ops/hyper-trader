"""事件日历测试：用 2026-10-26 抓取的官方原文（tests/fixtures）离线验证解析、时区、检查、合并与交易程序读取。"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from s03 import events_calendar as ec

FX = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 10, 26, 6, 0, tzinfo=timezone.utc)


def _texts():
    return {"fed": (FX / "fomc.html").read_text("utf-8"), "bls": (FX / "bls.ics").read_text("utf-8"),
            "bea": (FX / "bea.ics").read_text("utf-8")}


def test_fomc_parsed_with_dst():
    ev = ec.parse_fomc(_texts()["fed"])
    dec = {e["time_utc"] for e in ev if e["kind"] == "FOMC_DECISION"}
    assert "2026-10-28T18:00:00Z" in dec            # 10 月：美东夏令时 14:00 = UTC 18:00
    assert "2026-12-09T19:00:00Z" in dec            # 12 月：美东冬令时 14:00 = UTC 19:00
    assert sum(1 for t in dec if t.startswith("2026")) == 8


def test_bls_bea_times_and_whitelist():
    bls = ec.parse_ics(_texts()["bls"], "bls")
    bea = ec.parse_ics(_texts()["bea"], "bea")
    nfp = [e for e in bls if e["kind"] == "US_NFP" and e["time_utc"].startswith("2026-11")]
    assert nfp and nfp[0]["time_utc"] == "2026-11-06T13:30:00Z"   # 11 月已切冬令时：08:30 美东
    assert {e["kind"] for e in bls} == {"US_CPI", "US_NFP", "US_PPI"}
    assert any(e["time_utc"] == "2026-10-29T12:30:00Z" and e["kind"] == "US_PCE" for e in bea)
    assert {e["kind"] for e in bea} <= {"US_GDP", "US_PCE"}


def test_build_window_and_sanity():
    d = ec.build(_texts(), None, NOW, 35)
    assert d["ok"] and all(s["ok"] for s in d["sources"].values())
    kinds = [e["kind"] for e in d["events"]]
    assert kinds.count("FOMC_DECISION") == 1 and "US_CPI" in kinds and "US_NFP" in kinds
    assert all("2026-10-25" <= e["time_utc"] <= "2026-11-30T23:59:59Z" for e in d["events"])


def test_failed_source_keeps_previous_and_reports():
    prev = ec.build(_texts(), None, NOW, 35)
    t = _texts()
    t["bls"] = None
    d = ec.build(t, prev, NOW, 35, {"bls": "PermissionError"})
    assert not d["ok"] and d["sources"]["bls"]["stale"]
    assert [e for e in d["events"] if e["source"] == "bls"] == [e for e in prev["events"] if e["source"] == "bls"]


def test_format_change_is_detected():
    t = _texts()
    t["fed"] = "<html>page redesigned</html>"
    d = ec.build(t, None, NOW, 35)
    assert not d["sources"]["fed"]["ok"] and "fed" in d["sources"]["fed"]["issues"][0]


def test_diff_detects_moved_event():
    prev = ec.build(_texts(), None, NOW, 35)
    t = _texts()
    t["bls"] = t["bls"].replace("DTSTART;TZID=US-Eastern:20261110T083000", "DTSTART;TZID=US-Eastern:20261112T083000")
    d = ec.build(t, prev, NOW, 35)
    moved = [c for c in d["changes"] if c["type"] == "moved"]
    assert any(c["kind"] == "US_CPI" and c["time_utc"] == "2026-11-12T13:30:00Z" for c in moved)


def test_guard_reads_event_file_and_blocks_when_stale(tmp_path):
    from s03.config import RiskConfig, StrategyConfig
    from s03.risk.guard import RiskGuard

    f = tmp_path / "events.json"
    ec.write_atomic(str(f), ec.build(_texts(), None, NOW, 35))
    g = RiskGuard(RiskConfig(), StrategyConfig(event_file=str(f)), str(tmp_path / "KILL"))
    assert g.in_event_window(pd.Timestamp("2026-10-28T17:00:00Z"))       # FOMC 前 60 分钟
    assert not g.in_event_window(pd.Timestamp("2026-10-27T12:00:00Z"))
    assert not g.event_calendar_stale()
    os.utime(f, (0, 0))
    assert g.event_calendar_stale()
    g2 = RiskGuard(RiskConfig(), StrategyConfig(event_file=str(tmp_path / "missing.json")), str(tmp_path / "KILL"))
    assert g2.event_calendar_stale()
