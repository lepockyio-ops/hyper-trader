"""成交模拟：逐笔回放与影子模式共用（保证两者口径一致）。

保守假设：
- 同一根 K 线里既可能触发入场又可能打到止损时，按“先入场、后止损”处理。
- 止损和止盈在同一根 K 线都可能成交时，按止损先成交处理。
- 止损跳空时按开盘价成交，再扣滑点。
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


def entry_fill_a(bar: pd.Series, trigger: float, limit: float) -> float | None:
    """实现 A：突破触发的止损限价买单。"""
    if bar["open"] >= trigger:
        return float(bar["open"]) if bar["open"] <= limit else None   # 跳空高于上限：不成交
    if bar["high"] >= trigger:
        return float(trigger)
    return None


def b_signal(bar: pd.Series, trigger: float) -> bool:
    """实现 B：这根 15 分钟 K 线收盘价高于前一根高点（trigger）。"""
    return bool(bar["close"] > trigger)


def entry_fill_b(next_bar: pd.Series, limit: float) -> float | None:
    """实现 B：信号成立后，下一根开盘以 IOC 限价买入；开盘高于上限则不成交。"""
    return float(next_bar["open"]) if next_bar["open"] <= limit else None


@dataclass
class ExitEvent:
    kind: str          # stop / tp1 / tp2
    price: float
    frac_of_initial: float


def bar_exits(bar: pd.Series, stop: float, tps: list[tuple[str, float, float]], slippage: float) -> list[ExitEvent]:
    """tps: [(名称, 价格, 占初始仓位比例)]，已成交的不要再传入。"""
    if bar["low"] <= stop:
        px = min(stop, float(bar["open"])) * (1 - slippage)
        return [ExitEvent("stop", px, -1.0)]          # -1 表示剩余全部
    out = []
    for name, px, frac in tps:
        if bar["high"] >= px:
            out.append(ExitEvent(name, px, frac))
    return out


def funding_cost(notional: float, hours: float, rate_per_hour: float) -> float:
    """做多支付为正。实盘以 userFunding 实际值为准。"""
    return notional * rate_per_hour * hours
