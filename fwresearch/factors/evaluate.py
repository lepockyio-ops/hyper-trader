"""因子评估：IC / IR、alive / reversed / dead 分类、多重检验。

两种 IC：
- 截面 IC（cross_section，跨币种比较；v2.2 起为默认）：同一时间点、跨标的的 Rank IC，再对时间取平均。
  移植自 Vibe-Trading factor_analysis_core.compute_ic_series（MIT）。某一时刻至少 min_assets 个标的
  同时有因子值和前向收益才计入（上游为 5）。默认 5 个币（BTC、ETH、SOL、BNB、HYPE），所以只有 5 个币
  全部有数据的日期参与计算：HYPE 永续 2025-02 才上市，加上因子预热期，样本从 2025 年春起。
  它回答的是“这个因子高的币，之后是否比因子低的币涨得多”，对应在几个币之间挑强弱的策略；
  所有币同涨同跌的那部分被排序抵消，因此它不检验择时。每个时刻只有 5 个点排序，单期 IC 噪声很大，靠时间维度累积。
- 时间序列 IC（time_series，本框架新增）：回答“这个因子高的时候，这个币之后涨得多不多”，对应单标的择时型策略。
  做法：因子用过去 lookback 根 K 线的均值和标准差标准化，收益用过去 lookback 根的波动率标准化，
  两者都只用 t 时刻已知的数据；逐根相乘后按月取平均得到“每期 IC”，再对标的取平均。
  注意：不能在每个月内部做 Spearman / 相关系数——那样会用整月（含未来）的均值和方差去中心化、归一化，
  对 MA60 这类慢变因子会产生严重的虚假显著性（随机游走上 t≈15，见 tests 的 null 检验）。

前向收益：close[t+h]/close[t]-1，只保留不重叠的 h 期样本（与上游一致，避免 t 统计量被重叠样本放大约 sqrt(h) 倍）。
分类阈值（上游 bench_runner，按日频截面 IC 校准）：alive: IC>0.02 且正 IC 占比≥0.55 且 |t|>2；
reversed: IC<-0.02 且 |t|>2；其余 dead。对时间序列 IC 沿用同一组阈值，属于“示例值”。
"""
from __future__ import annotations

import math
from statistics import NormalDist
from typing import Any

import numpy as np
import pandas as pd

from fwresearch.factors import zoo
from fwresearch.stats import benjamini_hochberg

_N = NormalDist()
THRESHOLDS = {"ic": 0.02, "pos_ratio": 0.55, "t": 2.0, "fdr": 0.1}   # 示例值（上游默认）
# IC 观测数下限（本框架新增，示例值）：少于此数一律记为 insufficient，p 值记 1，不参与 FDR 显著。
# 实测教训：OKX 只能下载约 3 个月的资金费率，资金费因子只有 6 个截面观测，却得到 t=4.4、FDR 显著。
MIN_IC_OBS = {"cross_section": 60, "time_series": 12}


def forward_returns(close: pd.DataFrame, horizon: int = 1) -> pd.DataFrame:
    """对齐到因子时间 t 的 h 期前向收益；h>1 时只保留不重叠样本（从末尾倒数每 h 行取一行）。"""
    if horizon < 1:
        raise ValueError("horizon 必须 ≥ 1")
    fwd = close.shift(-horizon) / close - 1
    if horizon > 1:
        n = len(fwd)
        keep = [(n - 1 - i) % horizon == 0 for i in range(n)]
        fwd = fwd.where(pd.Series(keep, index=fwd.index), np.nan)
    return fwd


def cs_ic(factor: pd.DataFrame, ret: pd.DataFrame, min_assets: int = 5) -> pd.Series:
    """截面 Rank IC（每个时间点一个值）。"""
    d = factor.index.intersection(ret.index)
    c = factor.columns.intersection(ret.columns)
    if not len(d) or not len(c):
        return pd.Series(dtype=float)
    f, r = factor.loc[d, c], ret.loc[d, c]
    mask = f.notna() & r.notna()
    nvalid = mask.sum(axis=1)
    ic = f.where(mask).rank(axis=1).corrwith(r.where(mask).rank(axis=1), axis=1)
    return ic[nvalid >= min_assets].dropna().astype(float)


def ts_ic(factor: pd.DataFrame, close: pd.DataFrame, horizon: int = 1, freq: str = "MS", lookback: int = 120,
          min_obs: int = 8) -> pd.Series:
    """时间序列 IC（每期一个值）。只用 t 时刻已知的数据做标准化，避免期内中心化带来的前视偏差。"""
    ret = forward_returns(close, horizon)
    vol = close.pct_change(fill_method=None).rolling(lookback, min_periods=lookback).std() * np.sqrt(horizon)
    cols = factor.columns.intersection(close.columns)
    per_asset = []
    for c in cols:
        x = factor[c].astype(float)
        z = ((x - x.rolling(lookback, min_periods=lookback).mean())
             / x.rolling(lookback, min_periods=lookback).std().where(lambda s: s > 0)).clip(-4, 4)
        y = (ret[c] / vol[c].where(vol[c] > 0)).clip(-6, 6)
        prod = (z * y).dropna()
        if prod.empty:
            continue
        g = prod.groupby(pd.Grouper(freq=freq)).agg(["mean", "count"])
        per_asset.append(g["mean"][g["count"] >= min_obs].rename(c))
    if not per_asset:
        return pd.Series(dtype=float)
    return pd.concat(per_asset, axis=1).mean(axis=1).dropna()


def summarize_ic(ic: pd.Series, min_obs: int = 2) -> dict[str, Any]:
    n = int(len(ic))
    if n < max(min_obs, 2):
        out = {"ic_count": n, "category": "insufficient", "p_value": 1.0, "note": f"IC 观测 {n} 个 < {max(min_obs, 2)} 个"}
        if n >= 2:
            out.update(ic_mean=round(float(ic.mean()), 5), ir=0.0)
        return out
    m, sd = float(ic.mean()), float(ic.std())
    t = m / (sd / math.sqrt(n)) if sd > 0 else 0.0
    p = 2 * (1 - _N.cdf(abs(t)))
    pos = float((ic > 0).mean())
    th = THRESHOLDS
    cat = ("alive" if (m > th["ic"] and pos >= th["pos_ratio"] and abs(t) > th["t"])
           else "reversed" if (m < -th["ic"] and abs(t) > th["t"]) else "dead")
    return {"ic_mean": round(m, 5), "ic_std": round(sd, 5), "ir": round(m / sd, 4) if sd > 0 else 0.0,
            "ic_positive_ratio": round(pos, 4), "ic_count": n, "t": round(t, 3), "p_value": round(p, 5), "category": cat}


def evaluate(panel: zoo.Panel, factors: list[zoo.Factor] | None = None, horizon: int = 1, mode: str = "auto",
             freq: str = "MS", min_assets: int = 5, extra: dict[str, pd.DataFrame] | None = None,
             min_ic_obs: int | None = None) -> dict[str, Any]:
    """对 panel 跑一组因子。mode: auto（标的 ≥ min_assets 用截面，否则时间序列）/ cross_section / time_series。
    extra：外部算好的因子宽表（例如 Vibe-Trading 全量因子库的输出），id → DataFrame。"""
    close = panel["close"]
    n_assets = close.shape[1]
    use = mode if mode != "auto" else ("cross_section" if n_assets >= min_assets else "time_series")
    if use == "cross_section" and n_assets < min_assets:
        raise ValueError(f"截面 IC 至少需要 {min_assets} 个标的，当前 {n_assets} 个；请用 time_series")
    ret = forward_returns(close, horizon)
    coverage = _coverage(close, ret, min_assets) if use == "cross_section" else None
    rows, skipped = [], []
    items: list[tuple[str, str, str, pd.DataFrame | None, zoo.Factor | None]] = []
    for f in factors if factors is not None else zoo.available(panel):
        items.append((f.id, f.theme, f.formula, None, f))
    for fid, df in (extra or {}).items():
        items.append((fid, "external", "", df, None))
    for fid, theme, formula, df, f in items:
        try:
            fv = zoo.compute(f, panel) if f is not None else df.astype(float)
        except Exception as e:  # noqa: BLE001  因子出错只跳过，不中断整批
            skipped.append({"id": fid, "reason": str(e)[:200]})
            continue
        ic = cs_ic(fv, ret, min_assets) if use == "cross_section" else ts_ic(fv, close, horizon, freq)
        rows.append({"id": fid, "theme": theme, "formula": formula, **summarize_ic(ic, min_ic_obs or MIN_IC_OBS[use])})
    table = pd.DataFrame(rows)
    if not table.empty and "p_value" in table:
        pv = table["p_value"].fillna(1.0).to_numpy(float)
        bh = benjamini_hochberg(pv, THRESHOLDS["fdr"])
        table["p_adj_bh"] = np.round(bh.adjusted_p_values, 5)
        table["fdr_significant"] = bh.rejected
        if "ir" not in table:
            table["ir"] = 0.0
        table["ir"] = table["ir"].fillna(0.0)
        table = table.sort_values("ir", key=lambda s: s.abs(), ascending=False)
    counts = table["category"].value_counts().to_dict() if not table.empty else {}
    return {"mode": use, "horizon": horizon, "freq": freq if use == "time_series" else None, "n_assets": n_assets,
            "assets": list(close.columns), "start": str(close.index.min()), "end": str(close.index.max()),
            "n_factors": int(len(table)), "counts": counts, "n_fdr_significant": int(table.get("fdr_significant", pd.Series(dtype=bool)).sum()) if not table.empty else 0,
            "thresholds": {**THRESHOLDS, "min_ic_obs": min_ic_obs or MIN_IC_OBS[use]},
            "threshold_label": "示例值（分类阈值为上游按日频截面 IC 校准；观测数下限为本框架设定）",
            "coverage": coverage, "table": table.to_dict("records"), "skipped": skipped}


def _coverage(close: pd.DataFrame, ret: pd.DataFrame, min_assets: int) -> dict[str, Any]:
    """截面口径的样本覆盖：每个标的的首个有效日期，以及 ≥ min_assets 个标的同时有收益的日期数。"""
    ok = (close.notna() & ret.notna()).sum(axis=1) >= min_assets
    first = {c: (str(close[c].first_valid_index())[:10] if close[c].first_valid_index() is not None else None)
             for c in close.columns}
    return {"first_valid": first, "dates_usable": int(ok.sum()),
            "usable_from": str(ok[ok].index.min())[:10] if ok.any() else None, "min_assets": min_assets}


def truncation_safe(f: zoo.Factor, panel: zoo.Panel, cut: int) -> bool:
    """防前视检查：用前 cut 行算出的因子值，必须与用全量数据算出的前 cut 行完全一致。"""
    part = {k: v.iloc[:cut] for k, v in panel.items()}
    a = zoo.compute(f, part)
    b = zoo.compute(f, panel).iloc[:cut]
    return bool(np.allclose(a.to_numpy(float), b.to_numpy(float), equal_nan=True, rtol=1e-9, atol=1e-12))
