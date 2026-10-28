"""内置因子库：只用 OHLCV（可选资金费率）就能算、适合加密永续的因子。

- q158_*：按 Qlib Alpha158 的公式定义重写（Apache-2.0 的公式，非代码复制），窗口取 5 / 20 / 60。
- a101_*：按 Kakushadze (2015) "101 Formulaic Alphas" 的公式重写，只收录不需要行业 / 市值数据的几个。
- crypto_*：本框架自写（资金费率、波动结构）。

约定：compute(panel) → 宽表（index 时间，columns 标的）；只能使用当前及以前的数据。
想要 Vibe-Trading 的全量 478 个因子，用 factors.vt_bridge（需单独安装 Vibe-Trading）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import numpy as np
import pandas as pd

from fwresearch.factors import ops as o

Panel = dict[str, pd.DataFrame]


@dataclass(frozen=True)
class Factor:
    id: str
    theme: str
    formula: str
    compute: Callable[[Panel], pd.DataFrame]
    columns: tuple[str, ...] = ("close",)
    source: str = "builtin"
    notes: str = field(default="")


REGISTRY: dict[str, Factor] = {}


def _reg(f: Factor) -> None:
    if f.id in REGISTRY:
        raise ValueError(f"重复的因子 id：{f.id}")
    REGISTRY[f.id] = f


def _q158(n: int) -> None:
    c = lambda p: p["close"]  # noqa: E731
    _reg(Factor(f"q158_roc{n}", "momentum", f"Ref(close,{n})/close", lambda p, n=n: o.ref(c(p), n) / c(p)))
    _reg(Factor(f"q158_ma{n}", "trend", f"Mean(close,{n})/close", lambda p, n=n: o.ts_mean(c(p), n) / c(p)))
    _reg(Factor(f"q158_std{n}", "volatility", f"Std(close,{n})/close", lambda p, n=n: o.ts_std(c(p), n) / c(p)))
    _reg(Factor(f"q158_beta{n}", "momentum", f"Slope(close,{n})/close", lambda p, n=n: o.ts_slope(c(p), n) / c(p)))
    _reg(Factor(f"q158_max{n}", "range", f"Max(high,{n})/close", lambda p, n=n: o.ts_max(p["high"], n) / c(p),
                ("high", "close")))
    _reg(Factor(f"q158_min{n}", "range", f"Min(low,{n})/close", lambda p, n=n: o.ts_min(p["low"], n) / c(p),
                ("low", "close")))
    _reg(Factor(f"q158_rsv{n}", "range", f"(close-Min(low,{n}))/(Max(high,{n})-Min(low,{n}))",
                lambda p, n=n: o.safe_div(c(p) - o.ts_min(p["low"], n), o.ts_max(p["high"], n) - o.ts_min(p["low"], n)),
                ("high", "low", "close")))
    _reg(Factor(f"q158_imax{n}", "timing", f"IdxMax(high,{n})/{n}", lambda p, n=n: o.ts_argmax(p["high"], n) / n,
                ("high",)))
    _reg(Factor(f"q158_imin{n}", "timing", f"IdxMin(low,{n})/{n}", lambda p, n=n: o.ts_argmin(p["low"], n) / n,
                ("low",)))
    _reg(Factor(f"q158_corr{n}", "volume", f"Corr(close,Log(volume+1),{n})",
                lambda p, n=n: o.ts_corr(c(p), np.log1p(p["volume"].astype(float)), n), ("close", "volume")))
    _reg(Factor(f"q158_cntp{n}", "momentum", f"Mean(close>Ref(close,1),{n})",
                lambda p, n=n: o.ts_mean((o.delta(c(p), 1) > 0).astype(float).where(o.delta(c(p), 1).notna()), n)))
    _reg(Factor(f"q158_sump{n}", "momentum", f"Sum(Greater(close-Ref(close,1),0),{n})/Sum(Abs(close-Ref(close,1)),{n})",
                lambda p, n=n: o.safe_div(o.ts_sum(o.delta(c(p), 1).clip(lower=0), n), o.ts_sum(o.delta(c(p), 1).abs(), n))))
    _reg(Factor(f"q158_vma{n}", "volume", f"Mean(volume,{n})/volume",
                lambda p, n=n: o.safe_div(o.ts_mean(p["volume"], n), p["volume"]), ("volume",)))
    _reg(Factor(f"q158_vstd{n}", "volume", f"Std(volume,{n})/volume",
                lambda p, n=n: o.safe_div(o.ts_std(p["volume"], n), p["volume"]), ("volume",)))


for _n in (5, 20, 60):
    _q158(_n)

_reg(Factor("q158_kmid", "candle", "(close-open)/open", lambda p: (p["close"] - p["open"]) / p["open"], ("open", "close")))
_reg(Factor("q158_klen", "candle", "(high-low)/open", lambda p: (p["high"] - p["low"]) / p["open"], ("open", "high", "low")))
_reg(Factor("q158_ksft", "candle", "(2*close-high-low)/open",
            lambda p: (2 * p["close"] - p["high"] - p["low"]) / p["open"], ("open", "high", "low", "close")))

# ---- Kakushadze 101（只收不需要行业、市值的）
_reg(Factor("a101_006", "volume", "-1*Corr(open,volume,10)", lambda p: -o.ts_corr(p["open"], p["volume"], 10),
            ("open", "volume")))
_reg(Factor("a101_012", "reversal", "Sign(Delta(volume,1))*(-1*Delta(close,1))",
            lambda p: np.sign(o.delta(p["volume"], 1)) * (-o.delta(p["close"], 1)), ("close", "volume")))


def _a101_009(p: Panel) -> pd.DataFrame:
    d = o.delta(p["close"], 1)
    lo, hi = o.ts_min(d, 5), o.ts_max(d, 5)
    out = (-d).where(~((lo > 0) | (hi < 0)), d)
    return out.where(lo.notna() & hi.notna())


_reg(Factor("a101_009", "reversal", "(0<TsMin(Δclose,5)) ? Δclose : ((TsMax(Δclose,5)<0) ? Δclose : -Δclose)", _a101_009))
_reg(Factor("a101_101", "candle", "(close-open)/((high-low)+0.001*close)",
            lambda p: (p["close"] - p["open"]) / ((p["high"] - p["low"]) + 0.001 * p["close"]),
            ("open", "high", "low", "close"), notes="原式分母 +0.001 针对股票价格量级，这里改为 0.001×close"))

# ---- 加密专用（本框架自写）
for _n in (3, 21):
    _reg(Factor(f"crypto_funding_mean{_n}", "funding", f"Mean(funding,{_n})",
                lambda p, n=_n: o.ts_mean(p["funding"], n), ("funding",),
                notes="资金费率按 K 线周期对齐后的均值；正值 = 多头付费、拥挤"))
    _reg(Factor(f"crypto_funding_z{_n * 4}", "funding", f"ZScore_ts(funding,{_n * 4})",
                lambda p, n=_n * 4: o.safe_div(p["funding"] - o.ts_mean(p["funding"], n), o.ts_std(p["funding"], n)),
                ("funding",)))
_reg(Factor("crypto_vol_ratio", "volatility", "Std(ret,5)/Std(ret,60)",
            lambda p: o.safe_div(o.ts_std(p["close"].pct_change(fill_method=None), 5),
                                 o.ts_std(p["close"].pct_change(fill_method=None), 60))))
_reg(Factor("crypto_range_pos60", "range", "(close-Min(low,60))/(Max(high,60)-Min(low,60))",
            lambda p: o.safe_div(p["close"] - o.ts_min(p["low"], 60), o.ts_max(p["high"], 60) - o.ts_min(p["low"], 60)),
            ("high", "low", "close")))


def available(panel: Panel, ids: list[str] | None = None) -> list[Factor]:
    """返回 panel 列齐全的因子（缺资金费率时自动跳过 crypto_funding_*）。"""
    out = []
    for f in REGISTRY.values():
        if ids and f.id not in ids:
            continue
        if all(c in panel for c in f.columns):
            out.append(f)
    return out


def compute(f: Factor, panel: Panel) -> pd.DataFrame:
    df = f.compute(panel)
    if not isinstance(df, pd.DataFrame):
        raise TypeError(f"{f.id} 必须返回 DataFrame")
    return df.astype(float).replace([np.inf, -np.inf], np.nan)
