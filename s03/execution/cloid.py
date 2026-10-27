"""确定性 cloid：同一策略版本 + 标的 + 用途 + 信号 K 线时间 → 同一个 cloid。

程序重启后先按 cloid 查询订单状态，避免重复下单（手册第 12 节）。
"""
from __future__ import annotations

import hashlib

STRATEGY_VERSION = "S03-framework/v2.0"


def make_cloid_hex(coin: str, purpose: str, key: str, version: str = STRATEGY_VERSION) -> str:
    """purpose: entry / stop / tp1 / tp2 等；key 通常是布防时间或持仓开仓时间。"""
    h = hashlib.sha256(f"{version}|{coin}|{purpose}|{key}".encode()).hexdigest()
    return "0x" + h[:32]


def make_cloid(coin: str, purpose: str, key: str):
    from hyperliquid.utils.types import Cloid

    return Cloid.from_str(make_cloid_hex(coin, purpose, key))
