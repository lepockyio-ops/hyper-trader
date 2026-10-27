"""核心纯函数测试：精度、仓位与杠杆、指标、状态机、风控。运行：pytest -q"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from s03.config import CostConfig, RiskConfig, StrategyConfig  # noqa: E402
from s03.execution.cloid import make_cloid_hex  # noqa: E402
from s03.execution.precision import next_px_above, round_px, round_sz  # noqa: E402
from s03.research import sim  # noqa: E402
from s03.risk.guard import AccountSnapshot, MarketSnapshot, RiskGuard  # noqa: E402
from s03.risk.sizing import isolated_leverage, liq_distance, maintenance_rate, size_position  # noqa: E402
from s03.signals import indicators as ind  # noqa: E402


# ------------------------------------------------------------ 精度
def test_round_px_sig_and_decimals():
    assert round_px(80123.45, 5) == 80123.0 or round_px(80123.45, 5) == 80124.0   # BTC szDecimals=5 → 0 位小数
    assert round_px(100.6789, 2, "up") == 100.68          # SOL szDecimals=2 → 最多 4 位小数，5 位有效
    assert round_px(2331.049, 4, "down") == 2331.0        # ETH：5 位有效数字 → 2331.0
    assert round_px(123456.7, 5, "down") == 123456.0      # 整数价格不受 5 位有效数字限制


def test_next_px_above_strictly_greater():
    for px, d in [(100.69, 2), (80272.0, 5), (737.52, 3), (2331.04, 4)]:
        n = next_px_above(px, d)
        assert n > px
        assert round_px(n, d) == n


def test_round_sz():
    assert round_sz(15.3846, 2) == 15.38
    assert round_sz(0.08569, 5) == 0.08569


def test_cloid_deterministic():
    a = make_cloid_hex("SOL", "entry", "k1")
    assert a == make_cloid_hex("SOL", "entry", "k1") and a.startswith("0x") and len(a) == 34
    assert a != make_cloid_hex("SOL", "stop", "k1")


# ------------------------------------------------------------ 仓位与杠杆（手册第 7 节数字）
def test_manual_example_sol():
    plan = size_position(10_000, 100.0, 97.20, 20, RiskConfig(), CostConfig(slippage=0.0003))
    assert plan.size == pytest.approx(15.38, abs=0.02)
    assert plan.leverage == 9                       # 1/(3×2.8%×0.975+2.5%)=9.35 → 9
    assert plan.liq_dist_pct == pytest.approx(8.83, abs=0.05)
    assert plan.risk_usd == pytest.approx(50.0, abs=0.01)


def test_liq_distance_matches_manual_table():
    assert liq_distance(30, maintenance_rate(40)) * 100 == pytest.approx(2.11, abs=0.01)
    assert liq_distance(25, maintenance_rate(25)) * 100 == pytest.approx(2.04, abs=0.01)
    assert liq_distance(20, maintenance_rate(20)) * 100 == pytest.approx(2.56, abs=0.01)
    assert liq_distance(10, maintenance_rate(10)) * 100 == pytest.approx(5.26, abs=0.01)


@pytest.mark.parametrize("coin_max,expect", [(40, 9), (25, 9), (20, 8), (10, 7)])
def test_isolated_leverage_formula(coin_max, expect):
    assert isolated_leverage(0.03, coin_max, RiskConfig()) == expect


def test_liq_always_beyond_stop():
    for mx in (40, 25, 20, 10):
        for stop in (0.008, 0.015, 0.025, 0.03):
            lev = isolated_leverage(stop, mx, RiskConfig())
            assert liq_distance(lev, maintenance_rate(mx)) >= 3 * stop - 1e-9


# ------------------------------------------------------------ 指标
def _df(closes, spread=0.01, vol=1.0):
    idx = pd.date_range("2026-01-01", periods=len(closes), freq="D", tz="UTC")
    c = np.asarray(closes, float)
    return pd.DataFrame({"open": c, "high": c * (1 + spread), "low": c * (1 - spread), "close": c, "volume": vol},
                        index=idx)


def test_avwap_constant_price():
    df = _df([100] * 10)
    assert ind.avwap(df, df.index[3]) == pytest.approx(100.0)


def test_pivots():
    closes = [10, 9, 8, 7, 8, 9, 10, 9, 8, 7.5, 8, 9, 10, 11]
    lows = ind.pivot_lows(_df(closes, 0.0), 3)
    assert list(lows.values) == [7, 7.5]


# ------------------------------------------------------------ 成交模拟
def test_entry_fill_gap_above_limit_no_fill():
    bar = pd.Series({"open": 101.0, "high": 102, "low": 100.9, "close": 101.5})
    assert sim.entry_fill_a(bar, 100.4, 100.5) is None
    bar2 = pd.Series({"open": 100.0, "high": 100.6, "low": 99.9, "close": 100.5})
    assert sim.entry_fill_a(bar2, 100.4, 100.5) == 100.4


def test_stop_first_when_both_hit():
    bar = pd.Series({"open": 100.0, "high": 120, "low": 90, "close": 100})
    ev = sim.bar_exits(bar, 95.0, [("tp1", 110.0, 1 / 3)], 0.0)
    assert len(ev) == 1 and ev[0].kind == "stop"


# ------------------------------------------------------------ 风控
def _acct(**kw):
    base = dict(equity=10_000, day_start_equity=10_000, week_start_equity=10_000, open_positions={},
                open_risk_usd=0.0, consecutive_losses=0, last_loss_time=None, rate_budget_left=5000)
    base.update(kw)
    return AccountSnapshot(**base)


def test_guard_limits(tmp_path):
    g = RiskGuard(RiskConfig(), StrategyConfig(), str(tmp_path / "KILL"))
    now = pd.Timestamp.now(tz="UTC")
    assert g.global_block(_acct(), now).ok
    assert "daily_loss_halt" in g.global_block(_acct(equity=9_790), now).reasons
    assert "rate_budget_low" in g.global_block(_acct(rate_budget_left=100), now).reasons
    assert not g.check_entry("SOL", 1_500, 60, _acct()).ok                  # 风险超 0.5%
    full = {c: {"notional": 1000, "risk_usd": 50} for c in ("BTC", "ETH", "SOL")}
    assert "max_positions" in g.check_entry("BNB", 1_000, 50, _acct(open_positions=full, open_risk_usd=150)).reasons
    g.trip_kill("test")
    assert "kill_switch" in g.global_block(_acct(), now).reasons


def test_market_block():
    g = RiskGuard(RiskConfig(), StrategyConfig(), "/nonexistent/KILL")
    ok = MarketSnapshot("SOL", 0.0000125, 0.02, 3, 5_000_000)
    assert g.market_block(ok, 1_500).ok
    assert "funding_crowded_long" in g.market_block(MarketSnapshot("SOL", 0.0002, 0.02, 3, 5e6)).reasons
    assert "thin_book" in g.market_block(MarketSnapshot("SOL", 0.0, 0.02, 3, 10_000), 1_500).reasons
    assert "ref_price_mismatch_or_missing" in g.market_block(MarketSnapshot("SOL", 0.0, None, 3, 5e6)).reasons
