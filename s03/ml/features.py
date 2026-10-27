"""机器学习特征：只用信号发出时刻已知的数据（无未来信息）。框架提供通用特征，策略可用 ml_features() 追加。

数据集构建（events.py）与实盘门控（gate.py）调用同一个函数，保证训练与使用口径一致。
借鉴 FreqAI 的 include_corr_pairlist：给每个标的加入 BTC 的状态特征。
"""
from __future__ import annotations

import math

import pandas as pd

from s03.signals import indicators as ind

FEATURES = [
    "ret_1d", "ret_7d", "ret_30d", "vol_14d", "dist_ema20_15m_pct", "stop_dist_pct", "bar_range_pct",
    "vol_z15", "hour_sin", "hour_cos", "btc_ret30", "btc_above_ema21", "funding_bps",
]


def build_features(ctx, bars15: pd.DataFrame, stop_px: float, trigger_px: float,
                   btc_daily: pd.DataFrame | None = None, funding_per_hour: float | None = None,
                   extra: dict | None = None) -> dict:
    b = bars15.iloc[-1]
    c15 = bars15["close"]
    vol = bars15["volume"].iloc[-96:]
    z = (float(b["volume"]) - vol.mean()) / vol.std() if vol.std() > 0 else 0.0
    hour = bars15.index[-1].hour + bars15.index[-1].minute / 60
    day_close = c15.resample("1D").last().dropna()

    def ret(n):
        return (float(day_close.iloc[-1]) / float(day_close.iloc[-1 - n]) - 1) * 100 if len(day_close) > n else float("nan")

    f = {
        "ret_1d": ret(1), "ret_7d": ret(7), "ret_30d": ret(30),
        "vol_14d": float(day_close.pct_change().iloc[-14:].std() * 100) if len(day_close) > 15 else float("nan"),
        "dist_ema20_15m_pct": (float(b["close"]) / float(ind.ema(c15, 20).iloc[-1]) - 1) * 100,
        "stop_dist_pct": (trigger_px - stop_px) / trigger_px * 100,
        "bar_range_pct": (float(b["high"]) - float(b["low"])) / float(b["close"]) * 100,
        "vol_z15": z,
        "hour_sin": math.sin(2 * math.pi * hour / 24),
        "hour_cos": math.cos(2 * math.pi * hour / 24),
        "btc_ret30": float("nan"),
        "btc_above_ema21": float("nan"),
        "funding_bps": funding_per_hour * 1e4 if funding_per_hour is not None else float("nan"),
    }
    if btc_daily is not None and len(btc_daily) > 31:
        c = btc_daily["close"]
        f["btc_ret30"] = (float(c.iloc[-1]) / float(c.iloc[-31]) - 1) * 100
        f["btc_above_ema21"] = float(c.iloc[-1] > ind.ema(c, 21).iloc[-1])
    for k, v in (extra or {}).items():
        f[f"s_{k}"] = v
    return f
