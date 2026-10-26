"""录制与数据质量检查（手册第 3 节）。

HL 只保留最近 5,000 根 K 线，所以从第 1 周起每天录制并追加存档（CSV，便于检查）。
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from s03.data.hl_data import INTERVAL_MS, HLData


def store_path(data_dir: str, coin: str, interval: str) -> Path:
    return Path(data_dir) / "hl" / interval / f"{coin}.csv"


def load(data_dir: str, coin: str, interval: str) -> pd.DataFrame:
    p = store_path(data_dir, coin, interval)
    if not p.exists():
        return pd.DataFrame()
    df = pd.read_csv(p, parse_dates=["time"]).set_index("time")
    return df.sort_index()


def append(data_dir: str, coin: str, interval: str, new: pd.DataFrame) -> pd.DataFrame:
    old = load(data_dir, coin, interval)
    df = pd.concat([old, new]) if not old.empty else new
    df = df[~df.index.duplicated(keep="last")].sort_index()
    p = store_path(data_dir, coin, interval)
    p.parent.mkdir(parents=True, exist_ok=True)
    df.reset_index().to_csv(p, index=False)
    return df


def record(hl: HLData, data_dir: str, coins, intervals=("15m", "1h", "1d"), bars: int = 4999) -> None:
    for coin in coins:
        for iv in intervals:
            append(data_dir, coin, iv, hl.recent(coin, iv, bars))


def quality_report(df: pd.DataFrame, interval: str) -> dict:
    """缺失率、重复、异常 K 线（单根振幅 > 5 倍近 30 根中位数）。"""
    if df.empty:
        return {"rows": 0, "missing_pct": 100.0}
    step = pd.Timedelta(milliseconds=INTERVAL_MS[interval])
    expected = int((df.index[-1] - df.index[0]) / step) + 1
    rng = (df["high"] - df["low"]) / df["close"]
    spikes = rng > 5 * rng.rolling(30, min_periods=10).median()
    return {
        "rows": len(df),
        "expected": expected,
        "missing_pct": round((1 - len(df) / expected) * 100, 3),
        "spike_bars": [str(t) for t in df.index[spikes.fillna(False)]][-10:],
        "first": str(df.index[0]),
        "last": str(df.index[-1]),
    }


def cross_check_closes(a: pd.DataFrame, b: pd.DataFrame) -> dict:
    """两份同周期 K 线收盘价偏差（%）：用于 HL vs OKX。"""
    j = a[["close"]].join(b[["close"]], how="inner", lsuffix="_a", rsuffix="_b")
    if j.empty:
        return {"n": 0}
    d = (j["close_a"] / j["close_b"] - 1).abs() * 100
    return {"n": len(j), "max_pct": round(float(d.max()), 4), "mean_pct": round(float(d.mean()), 4),
            "p95_pct": round(float(d.quantile(0.95)), 4)}
