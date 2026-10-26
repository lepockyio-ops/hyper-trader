"""vectorbt 日线近似扫参（G4 参数稳健性的快速筛查，不替代 15 分钟逐笔回放）。

近似口径：当日线满足 S-03 第一层且最低价进入支撑区、收盘高于开盘时，次日开盘买入；
止损用 stop_max_pct，止盈 tp2_r × 止损；资金费率用基准值粗略扣除。
结论只用于排除“稍微改参数就由正转负”的组合，最终以 replay.py 为准。
vectorbt 为可选依赖：pip install vectorbt（许可证含 Commons Clause，自用可，不能转售）。
"""
from __future__ import annotations

import itertools
from dataclasses import replace

import pandas as pd

from s03.config import Settings
from s03.signals.s03 import daily_context


def daily_signals(coin: str, daily: pd.DataFrame, settings: Settings, warmup: int = 200) -> pd.Series:
    s = settings.strategy
    sig = pd.Series(False, index=daily.index)
    for i in range(warmup, len(daily) - 1):
        ctx = daily_context(coin, daily.iloc[:i], s)
        bar = daily.iloc[i]
        if ctx.eligible and bar["low"] <= ctx.zone.hi and bar["close"] > bar["open"]:
            sig.iloc[i + 1] = True
    return sig


def sweep(coin: str, daily: pd.DataFrame, settings: Settings, grid: dict[str, list]) -> pd.DataFrame:
    import vectorbt as vbt

    rows = []
    keys = list(grid)
    for combo in itertools.product(*grid.values()):
        st = replace(settings.strategy, **dict(zip(keys, combo)))
        s2 = replace(settings, strategy=st)
        entries = daily_signals(coin, daily, s2)
        sl = st.stop_max_pct / 100
        pf = vbt.Portfolio.from_signals(
            daily["open"], entries=entries, exits=pd.Series(False, index=daily.index),
            sl_stop=sl, tp_stop=sl * st.tp2_r,
            fees=settings.cost.taker_fee, slippage=settings.cost.slippage, freq="1D",
        )
        rows.append({**dict(zip(keys, combo)), "trades": int(pf.trades.count()),
                     "total_return": float(pf.total_return()), "max_dd": float(pf.max_drawdown())})
    return pd.DataFrame(rows)
