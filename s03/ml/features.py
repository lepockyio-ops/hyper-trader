"""机器学习特征：只用信号发出时刻已知的数据（无未来信息）。

数据集构建（events.py）与实盘门控（gate.py）调用同一个函数，保证训练与使用口径一致。
借鉴 FreqAI 的 include_corr_pairlist：给每个标的加入 BTC 的状态特征。
"""
from __future__ import annotations

import math

import pandas as pd

from s03.signals import indicators as ind
from s03.signals.s03 import DailyContext

FEATURES = [
    "ret30", "ret90", "ret180", "range_ratio", "dist_ema9_atr", "dist_ema21_atr", "dist_ema50_atr",
    "zone_width_pct", "zone_members", "retests", "dist_avwap_atr", "stop_dist_pct", "bar_range_pct",
    "vol_z15", "hour_sin", "hour_cos", "btc_ret30", "btc_above_ema21", "funding_bps",
]


def build_features(ctx: DailyContext, bars15: pd.DataFrame, stop_px: float, trigger_px: float,
                   btc_daily: pd.DataFrame | None = None, funding_per_hour: float | None = None) -> dict:
    d = ctx.detail
    atr = ctx.atr if ctx.atr > 0 else float("nan")
    b = bars15.iloc[-1]
    vol = bars15["volume"].iloc[-96:]
    z = (float(b["volume"]) - vol.mean()) / vol.std() if vol.std() > 0 else 0.0
    hour = bars15.index[-1].hour + bars15.index[-1].minute / 60
    f = {
        "ret30": d["returns_pct"].get(30, float("nan")),
        "ret90": d["returns_pct"].get(90, float("nan")),
        "ret180": d["returns_pct"].get(180, float("nan")),
        "range_ratio": d["range10"] / d["range30"] if d["range30"] else float("nan"),
        "dist_ema9_atr": (ctx.close - ctx.ema_fast) / atr,
        "dist_ema21_atr": (ctx.close - ctx.ema_mid) / atr,
        "dist_ema50_atr": (ctx.close - ctx.ema_slow) / atr,
        "zone_width_pct": (ctx.zone.hi / ctx.zone.lo - 1) * 100 if ctx.zone else float("nan"),
        "zone_members": len(ctx.zone.members) if ctx.zone else 0,
        "retests": ctx.retests,
        "dist_avwap_atr": (ctx.close - d["avwap"]) / atr,
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
    return f
