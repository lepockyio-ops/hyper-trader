"""仓位与单仓杠杆（手册第 7 节）。纯函数，回测与实盘共用。"""
from __future__ import annotations

import math
from dataclasses import dataclass

from s03.config import CostConfig, RiskConfig


@dataclass(frozen=True)
class SizePlan:
    size: float               # 合约数量（未按 szDecimals 截断）
    notional: float
    risk_usd: float
    per_unit_risk: float
    stop_dist_pct: float
    leverage: int             # 逐仓杠杆设定
    liq_dist_pct: float       # 该杠杆下的强平距离（做多，未计费用）


def maintenance_rate(platform_max_leverage: int) -> float:
    """维持保证金率 = 最大杠杆下初始保证金的一半（官方清算文档）。"""
    return 1.0 / (2 * platform_max_leverage)


def liq_distance(leverage: float, mm: float) -> float:
    """逐仓做多强平距离（占入场价比例）：(1/L − mm) / (1 − mm)。"""
    return (1.0 / leverage - mm) / (1 - mm)


def isolated_leverage(stop_dist: float, platform_max: int, risk: RiskConfig) -> int:
    """L = min(用户上限, 平台上限, floor(1 / (k × 止损距离 × (1 − mm) + mm)))，至少 1。"""
    mm = maintenance_rate(platform_max)
    raw = 1.0 / (risk.liq_to_stop_multiple * stop_dist * (1 - mm) + mm)
    return max(1, min(risk.max_position_leverage, platform_max, math.floor(raw)))


def size_position(
    equity: float,
    entry: float,
    stop: float,
    platform_max_leverage: int,
    risk: RiskConfig,
    cost: CostConfig,
    risk_frac: float | None = None,
) -> SizePlan:
    if stop >= entry:
        raise ValueError("只做多：止损必须低于入场价")
    rf = risk.risk_per_trade if risk_frac is None else risk_frac
    risk_usd = equity * rf
    stop_frac = (entry - stop) / entry
    funding = min(cost.funding_per_hour_est * cost.expected_hold_hours, cost.funding_cap_frac_of_stop * stop_frac)
    rt_cost = 2 * (cost.taker_fee + cost.slippage) + funding
    per_unit = (entry - stop) + entry * rt_cost
    size = risk_usd / per_unit
    notional = size * entry
    cap = equity * risk.max_notional_frac
    if notional > cap:
        size, notional = cap / entry, cap
    stop_dist = (entry - stop) / entry
    lev = isolated_leverage(stop_dist, platform_max_leverage, risk)
    return SizePlan(
        size=size,
        notional=notional,
        risk_usd=size * per_unit,
        per_unit_risk=per_unit,
        stop_dist_pct=stop_dist * 100,
        leverage=lev,
        liq_dist_pct=liq_distance(lev, maintenance_rate(platform_max_leverage)) * 100,
    )
