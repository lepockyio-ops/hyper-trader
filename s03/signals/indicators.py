"""纯函数指标。回测与实盘共用，不做任何 I/O。

约定：输入 DataFrame 的列为 open/high/low/close/volume，索引为 UTC 时间、升序、
只包含已收盘的 K 线。
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def ema(series: pd.Series, n: int) -> pd.Series:
    return series.ewm(span=n, adjust=False).mean()


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [df["high"] - df["low"], (df["high"] - prev_close).abs(), (df["low"] - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def pivot_lows(df: pd.DataFrame, span: int = 3) -> pd.Series:
    """摆动低点：左右各 span 根内的最低点。最后 span 根无法确认，返回 NaN。"""
    low = df["low"]
    win = low.rolling(2 * span + 1, center=True).min()
    out = low.where(low == win)
    out.iloc[-span:] = np.nan
    return out.dropna()


def pivot_highs(df: pd.DataFrame, span: int = 3) -> pd.Series:
    high = df["high"]
    win = high.rolling(2 * span + 1, center=True).max()
    out = high.where(high == win)
    out.iloc[-span:] = np.nan
    return out.dropna()


def avwap(df: pd.DataFrame, anchor_idx) -> float:
    """从 anchor_idx（含）到最后一根的成交量加权均价，典型价 = (H+L+C)/3。"""
    seg = df.loc[anchor_idx:]
    tp = (seg["high"] + seg["low"] + seg["close"]) / 3
    vol = seg["volume"]
    if vol.sum() <= 0:
        return float("nan")
    return float((tp * vol).sum() / vol.sum())


def range_pct(df: pd.DataFrame) -> pd.Series:
    return (df["high"] - df["low"]) / df["close"] * 100
