"""因子算子。输入输出都是宽表：index = 时间（UTC），columns = 标的。

语义与 Vibe-Trading agent/src/factors/base.py 一致（MIT，见 NOTICE.md）：
- NaN 一律传播，不做 fillna(0)；窗口不满时为 NaN（min_periods = 窗口长度）。
- 不提供负位移：delta(d) 要求 d ≥ 1，因此任何算子都看不到未来。
- 不返回 ±inf：safe_div 在分母接近 0 时给 NaN。
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def _f(df: pd.DataFrame) -> pd.DataFrame:
    return df.astype(float)


def rank(df: pd.DataFrame) -> pd.DataFrame:
    """截面百分位排名（同一时间点跨标的）。"""
    return _f(df).rank(axis=1, pct=True)


def zscore(df: pd.DataFrame) -> pd.DataFrame:
    d = _f(df)
    sd = d.std(axis=1)
    return d.sub(d.mean(axis=1), axis=0).div(sd.where(sd > 0), axis=0)


def ts_mean(df, n):
    return _f(df).rolling(n, min_periods=n).mean()


def ts_sum(df, n):
    return _f(df).rolling(n, min_periods=n).sum()


def ts_std(df, n):
    return _f(df).rolling(n, min_periods=n).std()


def ts_max(df, n):
    return _f(df).rolling(n, min_periods=n).max()


def ts_min(df, n):
    return _f(df).rolling(n, min_periods=n).min()


def ts_rank(df, n):
    """当前值在过去 n 个值中的百分位（含当前）。"""
    return _f(df).rolling(n, min_periods=n).rank(pct=True)


def ts_argmax(df, n):
    """窗口内最大值的位置（1 = 最早，n = 当前），与 Qlib IdxMax 一致。"""
    return _f(df).rolling(n, min_periods=n).apply(lambda a: float(np.argmax(a) + 1) if np.isfinite(a).all() else np.nan,
                                                  raw=True)


def ts_argmin(df, n):
    return _f(df).rolling(n, min_periods=n).apply(lambda a: float(np.argmin(a) + 1) if np.isfinite(a).all() else np.nan,
                                                  raw=True)


def ts_corr(x, y, n):
    """滚动相关；窗口内常数时为 NaN（不是 0）。"""
    x, y = _f(x), _f(y)
    out = x.rolling(n, min_periods=n).corr(y)
    return out.replace([np.inf, -np.inf], np.nan)


def ts_cov(x, y, n):
    return _f(x).rolling(n, min_periods=n).cov(_f(y))


def ts_slope(df, n):
    """对时间做最小二乘的斜率（每根 K 线的变化量）。"""
    d = _f(df)
    t = pd.Series(np.arange(len(d), dtype=float), index=d.index)
    tt = pd.DataFrame({c: t for c in d.columns})
    cov = d.rolling(n, min_periods=n).cov(tt)
    var = tt.rolling(n, min_periods=n).var()
    return cov / var


def delta(df, d: int = 1):
    if d < 1:
        raise ValueError("delta 的 d 必须 ≥ 1（禁止看未来）")
    return _f(df) - _f(df).shift(d)


def ref(df, d: int):
    """d 根之前的值（Qlib 的 Ref；只允许 d ≥ 1）。"""
    if d < 1:
        raise ValueError("ref 的 d 必须 ≥ 1（禁止看未来）")
    return _f(df).shift(d)


def decay_linear(df, n):
    w = np.arange(1, n + 1, dtype=float)
    w /= w.sum()
    return _f(df).rolling(n, min_periods=n).apply(lambda a: float(np.dot(a, w)), raw=True)


def signed_power(df, p):
    d = _f(df)
    return np.sign(d) * d.abs() ** p


def safe_div(a, b, eps: float = 1e-12):
    a, b = _f(a), _f(b) if isinstance(b, pd.DataFrame) else b
    if isinstance(b, pd.DataFrame):
        b = b.where(b.abs() > eps)
    out = a / b
    return out.replace([np.inf, -np.inf], np.nan)
