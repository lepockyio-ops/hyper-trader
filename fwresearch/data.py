"""把两个框架各自的历史数据读成因子用的宽表 panel：{"open": 宽表, "high": ..., "funding": 宽表(可选)}。

- source="hl"       ：hyper-trader 的 research/data/{COIN}_{bar}.csv（scripts/research_matrix.py fetch 生成）
- source="freqtrade"：freq-trader 的 user_data/data/{exchange}/futures/{COIN}_USDT_USDT-{tf}-futures.feather
                      （freqtrade download-data 生成；同目录下的 -funding_rate.feather 会作为资金费率读入）
- source="csv"      ：任意目录下的 {COIN}.csv（列 time/open/high/low/close/volume，可选 funding）
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

COLS = ("open", "high", "low", "close", "volume")
HL_BAR = {"1d": "1Dutc", "15m": "15m", "1h": "1H", "4h": "4H"}


def _std(df: pd.DataFrame, time_col: str | None = None) -> pd.DataFrame:
    if time_col:
        df = df.set_index(time_col)
    df.index = pd.to_datetime(df.index, utc=True)
    df = df[~df.index.duplicated()].sort_index()
    return df


def _one(source: str, data_dir: Path, coin: str, tf: str) -> pd.DataFrame:
    if source == "hl":
        p = data_dir / f"{coin}_{HL_BAR.get(tf, tf)}.csv"
        df = _std(pd.read_csv(p, index_col=0))
    elif source == "freqtrade":
        p = data_dir / f"{coin}_USDT_USDT-{tf}-futures.feather"
        df = _std(pd.read_feather(p), "date")
        fp = sorted(data_dir.glob(f"{coin}_USDT_USDT-*-funding_rate.feather"))
        if fp:
            raw = _std(pd.read_feather(fp[0]), "date")
            fr = raw["funding_rate" if "funding_rate" in raw else "open"].astype(float)   # 新旧版 freqtrade 列名不同
            df["funding"] = _align_funding(fr, df.index, tf)
    elif source == "csv":
        p = data_dir / f"{coin}.csv"
        raw = pd.read_csv(p)
        df = _std(raw, "time" if "time" in raw else raw.columns[0])
    else:
        raise ValueError(f"未知数据源：{source}")
    return df


def _align_funding(fr: pd.Series, index: pd.DatetimeIndex, tf: str) -> pd.Series:
    """资金费率对齐到 K 线：K 线周期 ≥ 1 小时时，取该根 K 线内全部结算费率之和（= 持有一根 K 线的资金成本）。"""
    unit = {"m": "min", "h": "h", "d": "D", "w": "W"}[tf[-1].lower()]
    step = pd.Timedelta(int(tf[:-1]), unit=unit)
    if step >= pd.Timedelta("1h"):
        return fr.resample(step, label="left", closed="left").sum(min_count=1).reindex(index)
    return fr.reindex(index, method="ffill")


def load_panel(source: str, data_dir: str | Path, coins: list[str], tf: str = "1d",
               start: str | None = None, end: str | None = None) -> dict[str, pd.DataFrame]:
    data_dir = Path(data_dir)
    frames, missing = {}, []
    for c in coins:
        try:
            frames[c] = _one(source, data_dir, c, tf)
        except FileNotFoundError as e:
            missing.append(f"{c}（{Path(e.filename).name if e.filename else e}）")
    if missing:
        hint = {"hl": "python3 scripts/research_matrix.py fetch --coins …",
                "freqtrade": "freqtrade download-data -c <配置> --timeframes <周期> --pairs …"}.get(source, "")
        raise ValueError(f"{data_dir} 下缺少这些标的的数据：{'、'.join(missing)}。先下载（{hint}），或用 --coins 只选已有的标的")
    panel: dict[str, pd.DataFrame] = {}
    for col in COLS + ("funding",):
        parts = {c: f[col].astype(float) for c, f in frames.items() if col in f}
        if parts and (col != "funding" or len(parts) == len(frames)):
            panel[col] = pd.DataFrame(parts).sort_index()
    idx = panel["close"].index
    if start:
        idx = idx[idx >= pd.Timestamp(start, tz="UTC")]
    if end:
        idx = idx[idx < pd.Timestamp(end, tz="UTC")]
    return {k: v.reindex(idx) for k, v in panel.items()}
