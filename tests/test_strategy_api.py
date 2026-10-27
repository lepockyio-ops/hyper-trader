"""策略插件接口测试：空白模板不产生入场；示例插件能走通“挂单 → 改价 → 撤单”；框架止损护栏与回放能跑通。"""
from __future__ import annotations

import numpy as np
import pandas as pd

from s03.config import Settings, StrategyConfig
from s03.research.replay import run_replay
from s03.strategy.api import ArmState, Context, Position, StrategyPlugin, check_stop, load_strategy

EXAMPLE = "s03.strategies.examples.sma_cross:SmaCrossExample"


def _daily(n=120, drift=0.004, seed=1):
    rng = np.random.default_rng(seed)
    c = 100 * np.exp(np.cumsum(rng.normal(drift, 0.02, n)))
    idx = pd.date_range("2026-01-01", periods=n, freq="D", tz="UTC")
    return pd.DataFrame({"open": c, "high": c * 1.01, "low": c * 0.99, "close": c, "volume": 1.0}, index=idx)


def _bars(closes, start="2026-05-01"):
    c = np.asarray(closes, dtype=float)
    o = np.r_[c[0], c[:-1]]
    idx = pd.date_range(start, periods=len(c), freq="15min", tz="UTC")
    return pd.DataFrame({"open": o, "high": np.maximum(o, c) * 1.002, "low": np.minimum(o, c) * 0.995,
                         "close": c, "volume": 1.0}, index=idx)


def test_default_is_blank_template_and_never_enters():
    s = load_strategy(StrategyConfig())
    assert s.is_template and s.name == "blank"
    ctx = s.context("BTC", _daily())
    assert not ctx.eligible and ctx.checks == {"strategy_configured": False}
    st, it = s.step(ArmState("BTC"), ctx, _bars(np.linspace(100, 110, 50)))
    assert it is None and s.take_profit_legs(Position("BTC", 100, 98, 1, pd.Timestamp.now(tz="UTC"))) == []


def test_load_rejects_non_plugin():
    import pytest

    with pytest.raises(TypeError):
        load_strategy(StrategyConfig(plugin="s03.config:StrategyConfig"))


def test_example_place_modify_cancel():
    s = load_strategy(StrategyConfig(plugin=EXAMPLE))
    ctx = Context("SOL", pd.Timestamp("2026-04-30", tz="UTC"), True, {}, {"sma": 90.0}, 100.0)
    closes = list(np.linspace(105, 100, 40)) + [101.5]          # 下跌后收盘上穿 EMA20
    st, it = s.step(ArmState("SOL"), ctx, _bars(closes))
    assert it is not None and it.kind == "place" and st.phase == "confirmed" and it.stop_px < it.trigger_px
    st2, it2 = s.step(st, ctx, _bars(closes + [101.8]))
    assert it2.kind == "modify"
    st3, it3 = s.step(st2, ctx, _bars(closes + [101.8, 99.0]))
    assert it3.kind == "cancel" and st3.phase == "idle"
    st4, it4 = s.step(st, ctx, _bars(closes), blocked=True)
    assert it4.kind == "cancel"


def test_framework_stop_guard():
    cfg = StrategyConfig()
    assert check_stop(100, 99.5, cfg) is None                     # 0.5% < 0.8%：skip
    assert abs(check_stop(100, 99.5, StrategyConfig(stop_min_mode="floor")) - 99.2) < 1e-9
    assert check_stop(100, 96.0, cfg) is None                      # 4% > 3%
    assert check_stop(100, 98.0, cfg) == 98.0
    assert check_stop(100, 101.0, cfg) is None


class _AlwaysBuy(StrategyPlugin):
    """测试用：每天都合格；每根 K 线在高点挂单，止损 2%。"""
    name = "always"
    warmup_days = 5

    def context(self, coin, daily):
        return Context(coin, daily.index[-1], True, {}, {}, float(daily["close"].iloc[-1]))

    def step(self, state, ctx, bars, blocked=False):
        from dataclasses import replace

        from s03.strategy.api import EntryIntent

        if state.phase == "in_position":
            return state, None
        b = bars.iloc[-1]
        trig, stop = float(b["high"]), float(b["high"]) * 0.98
        kind = "modify" if state.phase == "confirmed" else "place"
        return replace(state, phase="confirmed", trigger_px=trig, limit_px=trig * 1.001, stop_px=stop), \
            EntryIntent(kind, ctx.coin, trig, trig * 1.001, stop, bars.index[-1])

    def take_profit_legs(self, pos):
        return [(pos.entry_px + 2 * pos.r, 0.5)]


def test_replay_runs_with_any_plugin():
    rng = np.random.default_rng(7)
    daily = _daily(60)
    c = 100 * np.exp(np.cumsum(rng.normal(0.0002, 0.004, 96 * 20)))
    bars = _bars(c, start=str(daily.index[10].date()))
    tdf, summary = run_replay("SOL", daily, bars, Settings(), 20, strategy=_AlwaysBuy(StrategyConfig()), warmup_days=10)
    assert summary["trades"] > 0 and {"R", "pnl", "stop"} <= set(tdf.columns)
    risk = (tdf["entry"] - tdf["stop"]) / tdf["entry"] * 100
    assert ((risk >= 0.8 - 1e-9) & (risk <= 3.0 + 1e-9)).all()     # 回放同样经过框架止损护栏
    _, s_blank = run_replay("SOL", daily, bars, Settings(), 20, warmup_days=10)
    assert s_blank["trades"] == 0                                   # 空白模板：没有交易
