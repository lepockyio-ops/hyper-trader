"""随机性质测试（Hypothesis）：自动生成大量随机输入，检查“必须永远成立”的规则（手册第 5、7 节）。
框架护栏对任何策略插件都成立：止损距离、强平距离、风险预算、告警冷却。"""
from __future__ import annotations

import math

import pandas as pd
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from s03.alerts.core import AlertConfig, AlertEngine
from s03.config import CostConfig, RiskConfig, StrategyConfig
from s03.risk.sizing import isolated_leverage, liq_distance, maintenance_rate, size_position
from s03.strategy.api import check_stop

px = st.floats(min_value=1.0, max_value=200_000.0, allow_nan=False, allow_infinity=False)
frac = st.floats(min_value=0.0, max_value=0.06, allow_nan=False)


@settings(max_examples=2000, deadline=None)
@given(entry=px, drop=frac, mode=st.sampled_from(["skip", "floor"]))
def test_stop_guard_always_within_min_max(entry, drop, mode):
    """框架止损护栏：无论策略插件给出什么止损，通过检查的止损距离一定在 [stop_min_pct, stop_max_pct]。"""
    cfg = StrategyConfig(stop_min_mode=mode)
    stop = check_stop(entry, entry * (1 - drop), cfg)
    if stop is not None:
        dist = (entry - stop) / entry * 100
        assert cfg.stop_min_pct - 1e-9 <= dist <= cfg.stop_max_pct + 1e-9
    if mode == "floor" and 1e-6 < drop * 100 < cfg.stop_min_pct:
        assert stop is not None


@settings(max_examples=2000, deadline=None)
@given(stop_pct=st.floats(min_value=0.8, max_value=3.0), pmax=st.sampled_from([3, 5, 10, 20, 25, 40, 50]))
def test_liquidation_at_least_3x_stop(stop_pct, pmax):
    r = RiskConfig()
    lev = isolated_leverage(stop_pct / 100, pmax, r)
    assert 1 <= lev <= min(pmax, r.max_position_leverage)
    if lev > 1:
        assert liq_distance(lev, maintenance_rate(pmax)) >= r.liq_to_stop_multiple * stop_pct / 100 - 1e-12


@settings(max_examples=2000, deadline=None)
@given(equity=st.floats(min_value=1_000, max_value=1_000_000), entry=px,
       stop_pct=st.floats(min_value=0.8, max_value=3.0), funding_h=st.floats(min_value=0, max_value=0.0005))
def test_risk_budget_used_but_never_exceeded(equity, entry, stop_pct, funding_h):
    risk, cost = RiskConfig(), CostConfig(funding_per_hour_est=funding_h)
    stop = entry * (1 - stop_pct / 100)
    plan = size_position(equity, entry, stop, 40, risk, cost)
    budget = equity * risk.risk_per_trade
    assert plan.risk_usd <= budget * (1 + 1e-9)                       # 含成本的总风险不超过 0.5%
    loss_at_stop = plan.size * (entry - stop)
    if plan.notional < equity * risk.max_notional_frac * (1 - 1e-9):    # 未触及名义价值上限时
        assert loss_at_stop >= 0.7 * budget                             # 止损本身至少用掉预算的 70%


@settings(max_examples=300, deadline=None)
@given(rets=st.lists(st.floats(min_value=-0.03, max_value=0.03), min_size=300, max_size=600))
def test_alert_cooldown_never_violated(rets):
    cfg = AlertConfig()
    e = AlertEngine(cfg)
    p, fired = 100.0, []
    for i, r in enumerate(rets):
        p *= 1 + r
        assume(p > 1e-6)
        fired += e.on_bar_1m("BTC", i * 60, p)
    times = [a.ts for a in fired if a.rule == "shock_1m"]
    assert all(b - a >= cfg.cooldown_s for a, b in zip(times, times[1:]))
