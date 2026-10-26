"""Hyperliquid 永续价格 / 数量精度（官方 tick-and-lot-size 文档）。

- 价格最多 5 位有效数字，且小数位 ≤ 6 − szDecimals；整数价格不受 5 位有效数字限制。
- 数量按 szDecimals 截断。
纯函数，回测与实盘共用。
"""
from __future__ import annotations

import math

MAX_DECIMALS_PERP = 6


def _round_sig(x: float, sig: int, direction: str) -> float:
    if x == 0:
        return 0.0
    exp = math.floor(math.log10(abs(x)))
    factor = 10 ** (sig - 1 - exp)
    v = x * factor
    v = math.ceil(v - 1e-9) if direction == "up" else math.floor(v + 1e-9) if direction == "down" else round(v)
    return v / factor


def round_px(px: float, sz_decimals: int, direction: str = "nearest") -> float:
    """direction: up（触发价、买入上限）/ down（止损、卖出下限）/ nearest。"""
    max_dec = MAX_DECIMALS_PERP - sz_decimals
    if px >= 10 ** 5 and abs(px - round(px)) < 1e-9:
        return float(round(px))
    if px >= 10 ** 5:                       # 5 位有效数字以上只能用整数
        return float(math.ceil(px) if direction == "up" else math.floor(px) if direction == "down" else round(px))
    v = _round_sig(px, 5, direction)
    q = 10 ** max_dec
    if direction == "up":
        v = math.ceil(v * q - 1e-9) / q
    elif direction == "down":
        v = math.floor(v * q + 1e-9) / q
    else:
        v = round(v * q) / q
    return float(f"{v:.{max_dec}f}")


def next_px_above(px: float, sz_decimals: int) -> float:
    """严格高于 px 的最小合法价格（“前高 + 1 个最小价位”）。"""
    up = round_px(px, sz_decimals, "up")
    if up > px:
        return up
    return round_px(px * (1 + 1e-6) + _tick_at(px, sz_decimals), sz_decimals, "up")


def _tick_at(px: float, sz_decimals: int) -> float:
    if px >= 10 ** 5:
        return 1.0
    exp = math.floor(math.log10(abs(px)))
    sig_tick = 10 ** (exp - 4)
    dec_tick = 10 ** -(MAX_DECIMALS_PERP - sz_decimals)
    return max(sig_tick, dec_tick)


def round_sz(sz: float, sz_decimals: int) -> float:
    q = 10 ** sz_decimals
    return math.floor(sz * q + 1e-9) / q
