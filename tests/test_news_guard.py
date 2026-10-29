"""BlockBeats 新闻事件守卫在风控层的行为：按币种、只收紧、文件过期则忽略。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from fwresearch import blockbeats as bb
from s03.config import RiskConfig, StrategyConfig
from s03.risk.guard import AccountSnapshot, RiskGuard

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)


def _acct():
    return AccountSnapshot(equity=10_000, day_start_equity=10_000, week_start_equity=10_000, open_positions={},
                           open_risk_usd=0, consecutive_losses=0, last_loss_time=None, rate_budget_left=None)


def _guard(tmp_path, blocks, generated=NOW, venue="hyperliquid"):
    f = tmp_path / "news_blocks.json"
    bb.write_guard(f, blocks, generated, ok=True)
    return RiskGuard(RiskConfig(), StrategyConfig(news_file=str(f), news_venue=venue), str(tmp_path / "KILL"))


def _blk(target, cat="security", hours=24):
    return {"target": target, "category": cat, "until_utc": (NOW + timedelta(hours=hours)).isoformat(),
            "news_id": 1, "title": "t", "link": "", "received_at": NOW.isoformat()}


def test_coin_block_only_affects_that_coin(tmp_path):
    g = _guard(tmp_path, [_blk("SOL")])
    t = pd.Timestamp(NOW + timedelta(minutes=5))
    v = g.check_entry("SOL", 100, 10, _acct(), now=t)
    assert not v.ok and "news_block:security:SOL" in v.reasons
    assert g.check_entry("BTC", 100, 10, _acct(), now=t).ok


def test_venue_block_affects_all_coins_on_that_venue(tmp_path):
    t = pd.Timestamp(NOW + timedelta(minutes=5))
    g = _guard(tmp_path, [_blk("@hyperliquid", "withdrawal_halt")])
    assert not g.check_entry("BTC", 100, 10, _acct(), now=t).ok
    g2 = _guard(tmp_path, [_blk("@binance", "withdrawal_halt")])           # 别的交易所出事不影响本框架
    assert g2.check_entry("BTC", 100, 10, _acct(), now=t).ok


def test_stale_or_missing_file_never_blocks(tmp_path):
    g = _guard(tmp_path, [_blk("SOL")], generated=NOW - timedelta(hours=2))
    v, status = g.news_block("SOL", pd.Timestamp(NOW))
    assert v.ok and status == "stale"
    g0 = RiskGuard(RiskConfig(), StrategyConfig(), str(tmp_path / "KILL"))   # 默认不启用
    v0, st0 = g0.news_block("SOL")
    assert v0.ok and st0 == "disabled"


def test_expired_block_is_ignored(tmp_path):
    g = _guard(tmp_path, [_blk("SOL", hours=1)])
    assert g.news_block("SOL", pd.Timestamp(NOW + timedelta(minutes=29)))[0].ok is False
    g2 = _guard(tmp_path, [_blk("SOL", hours=1)], generated=NOW + timedelta(hours=2))
    assert g2.news_block("SOL", pd.Timestamp(NOW + timedelta(hours=2, minutes=1)))[0].ok
