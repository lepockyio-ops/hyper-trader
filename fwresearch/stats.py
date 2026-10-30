"""统计检验：一条策略的回测结果，有多少是真实优势、有多少是运气和过度搜索。

输入是逐笔交易的 R（盈亏 ÷ 初始风险）。两个框架的回测都能给出它：
  hyper-trader：s03/research/replay.py 的 trades["R"]
  freq-trader ：research/matrix.py 保存的逐笔 R（按 stop_loss_abs 计算）

检验分三组（阈值全部是“示例值”，写在 DEFAULT_GATES，使用者可以改，改了要在研究记录里写明）：
  1. 这条策略有没有优势：      Bootstrap 期望 R 置信区间、符号翻转检验
  2. 优势稳不稳：              滚动分段一致性、Bootstrap Sharpe 区间、Monte Carlo 排列（路径）
  3. 是不是搜出来的运气：      Deflated Sharpe（按尝试次数折扣）、PBO（CSCV 过拟合概率）、BH 多重检验

移植自 Vibe-Trading（MIT）的 backtest/validation.py 与 src/quantlib/multipletesting.py，见 NOTICE.md。
正态分布用标准库 statistics.NormalDist，不依赖 scipy。
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from itertools import combinations
from statistics import NormalDist
from typing import Any, Sequence

import numpy as np
import pandas as pd

_N = NormalDist()
EULER_MASCHERONI = 0.5772156649015329
GAUSSIAN_KURTOSIS = 3.0          # 非超额峰度：正态分布为 3，不是 0
MIN_OBSERVATIONS = 30            # PSR / DSR 至少需要的样本数（上游同值）

# 判定阈值：全部为“示例值”，未经数据验证。
DEFAULT_GATES: dict[str, Any] = {
    "min_trades": 30,            # 少于此数 → 证据不足，不做检验
    "pass_min_trades": 100,      # 判“通过”至少需要的笔数（G1：≥ 100 笔；30–99 笔最多判“证据不足”）
    "holdout_min_trades": 10,    # 样本外段至少的笔数
    "block_freq": "W",           # 按时间块（周）汇总 R 再做 Bootstrap：同时持仓、行情聚集时比逐笔 Bootstrap 保守
    "confidence": 0.95,          # 置信水平
    "boot_lower_R": 0.0,         # 期望 R 的 Bootstrap 区间下限必须 > 此值
    "sign_flip_p": 0.05,         # 符号翻转检验 p 值必须 < 此值
    "wf_consistency": 0.6,       # 滚动分段中期望 R > 0 的窗口占比必须 ≥ 此值
    "dsr": 0.95,                 # Deflated Sharpe 必须 > 此值（尝试次数 ≥ 2 时才判）
    "pbo": 0.5,                  # 过拟合概率必须 < 此值（方案数 ≥ 2 时才判）
}


# ======================================================================== 基础
def _clean(x: Sequence[float] | np.ndarray | pd.Series) -> np.ndarray:
    a = np.asarray(x, dtype=float).ravel()
    return a[np.isfinite(a)]


def sharpe_ratio(returns, risk_free: float = 0.0, ddof: int = 1) -> float:
    """每笔（每期）Sharpe，不年化。DSR / PSR 必须用这个口径，年化后再配样本数会把显著性放大约 sqrt(252) 倍。"""
    v = _clean(returns)
    if v.size < 2:
        raise ValueError(f"sharpe_ratio 至少需要 2 个观测，实际 {v.size}")
    ex = v - risk_free
    sd = float(ex.std(ddof=ddof))
    if not np.isfinite(sd) or sd <= 0:
        return float("nan")
    return float(ex.mean() / sd)


def moments(returns) -> tuple[float, float]:
    """偏度与非超额峰度（正态 = 0 与 3）。"""
    v = _clean(returns)
    if v.size < 3:
        return 0.0, GAUSSIAN_KURTOSIS
    m, sd = v.mean(), v.std(ddof=0)
    if sd <= 0:
        return 0.0, GAUSSIAN_KURTOSIS
    z = (v - m) / sd
    return float((z ** 3).mean()), float((z ** 4).mean())


# ======================================================================== 1. 有没有优势
def bootstrap_mean_ci(r, n_boot: int = 5000, confidence: float = 0.95, seed: int = 42) -> dict[str, Any]:
    """期望 R 的 Bootstrap 置信区间（本框架新增：手册以期望 R 作为核心指标）。"""
    v = _clean(r)
    if v.size < 5:
        return {"error": "至少需要 5 笔交易"}
    rng = np.random.default_rng(seed)
    means = rng.choice(v, size=(n_boot, v.size), replace=True).mean(axis=1)
    a = (1 - confidence) / 2
    return {"mean_R": round(float(v.mean()), 4), "ci_lower": round(float(np.quantile(means, a)), 4),
            "ci_upper": round(float(np.quantile(means, 1 - a)), 4), "prob_positive": round(float((means > 0).mean()), 4),
            "n": int(v.size), "n_boot": n_boot, "confidence": confidence}


def sign_flip_test(r, n_perm: int = 10000, seed: int = 42) -> dict[str, Any]:
    """单侧检验 H0：期望 R ≤ 0。随机翻转每笔 R 的符号，看平均值达到实际值的比例（本框架新增）。
    与 Monte Carlo 排列不同：打乱交易顺序不改变平均值，所以排列检验检的是“路径”，这个检的是“优势”。"""
    v = _clean(r)
    if v.size < 5:
        return {"error": "至少需要 5 笔交易", "p_value": 1.0}
    rng = np.random.default_rng(seed)
    obs = v.mean()
    signs = rng.choice((-1.0, 1.0), size=(n_perm, v.size))
    sims = (signs * np.abs(v)).mean(axis=1)
    p = (1 + int((sims >= obs).sum())) / (n_perm + 1)
    return {"mean_R": round(float(obs), 4), "p_value": round(p, 5), "n": int(v.size), "n_perm": n_perm}


# ======================================================================== 2. 稳不稳（移植自 validation.py）
def _path_metrics(pnls: np.ndarray, capital: float, periods_per_year: float) -> dict[str, float]:
    equity = np.concatenate(([capital], capital + np.cumsum(pnls)))
    prev = equity[:-1]
    rets = np.where(prev != 0, np.diff(equity) / np.where(prev != 0, prev, 1.0), 0.0) if len(equity) > 1 else np.zeros(1)
    sharpe = float(rets.mean() / (rets.std() + 1e-10) * math.sqrt(periods_per_year))
    peak = np.maximum.accumulate(equity)
    return {"sharpe": sharpe, "max_dd": float(((equity - peak) / np.where(peak > 0, peak, 1.0)).min())}


def monte_carlo_permutation(r, risk_fraction: float = 0.005, n_sim: int = 1000, seed: int = 42,
                            periods_per_year: float = 252) -> dict[str, Any]:
    """打乱逐笔顺序，看实际的 Sharpe / 最大回撤是否好于随机顺序（上游 monte_carlo_test）。
    盈亏单位：每笔 R × 单笔风险比例，初始资金 = 1。注意：它检验的是回撤路径，不检验平均优势。"""
    v = _clean(r)
    if v.size < 3:
        return {"error": "至少需要 3 笔交易", "p_value_sharpe": 1.0}
    pnls = v * risk_fraction
    act = _path_metrics(pnls, 1.0, periods_per_year)
    rng = np.random.default_rng(seed)
    sh, dd = [], []
    for _ in range(n_sim):
        m = _path_metrics(rng.permutation(pnls), 1.0, periods_per_year)
        sh.append(m["sharpe"])
        dd.append(m["max_dd"])
    sh_a, dd_a = np.array(sh), np.array(dd)
    return {"actual_sharpe": round(act["sharpe"], 4), "actual_max_dd": round(act["max_dd"], 4),
            "p_value_sharpe": round(float((sh_a >= act["sharpe"]).mean()), 4),
            "p_value_max_dd": round(float((dd_a >= act["max_dd"]).mean()), 4),
            "simulated_max_dd_p5": round(float(np.percentile(dd_a, 5)), 4),
            "simulated_max_dd_median": round(float(np.median(dd_a)), 4),
            "n_sim": n_sim, "n": int(v.size)}


def bootstrap_sharpe_ci(returns, n_boot: int = 2000, confidence: float = 0.95, periods_per_year: float = 1.0,
                        seed: int = 42) -> dict[str, Any]:
    """重抽样收益估计 Sharpe 区间（上游 bootstrap_sharpe_ci）。periods_per_year=1 时为每笔 Sharpe。"""
    v = _clean(returns)
    if v.size < 5:
        return {"error": "至少需要 5 个观测"}

    def sh(x):
        return float(x.mean() / (x.std() + 1e-10) * math.sqrt(periods_per_year))

    rng = np.random.default_rng(seed)
    boots = np.array([sh(rng.choice(v, size=v.size, replace=True)) for _ in range(n_boot)])
    a = (1 - confidence) / 2
    return {"observed_sharpe": round(sh(v), 4), "ci_lower": round(float(np.percentile(boots, a * 100)), 4),
            "ci_upper": round(float(np.percentile(boots, (1 - a) * 100)), 4),
            "median_sharpe": round(float(np.median(boots)), 4), "prob_positive": round(float((boots > 0).mean()), 4),
            "confidence": confidence, "n_boot": n_boot}


def walk_forward(trades: pd.DataFrame, n_windows: int = 5, time_col: str = "time") -> dict[str, Any]:
    """分段一致性：把同一批结果按时间切成不重叠窗口，看每段的期望 R 是否一致为正。
    名字沿用上游 walk_forward_analysis，但它**不是**“训练 → 冻结参数 → 样本外测试”的滚动验证；
    样本外检验见 evaluate_trades 的 holdout_start（报告里叫“分段一致性”）。"""
    if n_windows < 1:
        return {"error": "n_windows 必须 ≥ 1"}
    df = trades.dropna(subset=["R"]).sort_values(time_col)
    if len(df) < n_windows * 2:
        return {"error": f"{n_windows} 个窗口至少需要 {n_windows * 2} 笔交易"}
    t = pd.to_datetime(df[time_col], utc=True)
    edges = pd.date_range(t.min(), t.max(), periods=n_windows + 1)
    wins = []
    for i in range(n_windows):
        lo, hi = edges[i], edges[i + 1]
        m = (t >= lo) & ((t < hi) if i < n_windows - 1 else (t <= hi))
        r = df.loc[m.values, "R"].to_numpy(float)
        wins.append({"window": i + 1, "start": f"{lo:%Y-%m-%d}", "end": f"{hi:%Y-%m-%d}", "trades": int(r.size),
                     "expectancy_R": round(float(r.mean()), 4) if r.size else None,
                     "sum_R": round(float(r.sum()), 3), "win_rate": round(float((r > 0).mean()), 3) if r.size else None})
    used = [w for w in wins if w["trades"] > 0]
    good = sum(1 for w in used if w["expectancy_R"] > 0)
    exps = [w["expectancy_R"] for w in used]
    return {"n_windows": n_windows, "windows": wins, "windows_with_trades": len(used), "profitable_windows": good,
            "consistency_rate": round(good / len(used), 4) if used else 0.0,
            "expectancy_mean": round(float(np.mean(exps)), 4) if exps else None,
            "expectancy_std": round(float(np.std(exps)), 4) if exps else None}


def block_bootstrap_mean_ci(trades: pd.DataFrame, time_col: str = "time", freq: str = "W", n_boot: int = 5000,
                            confidence: float = 0.95, seed: int = 42) -> dict[str, Any]:
    """按时间块汇总 R（默认每周，按平仓时间；没有交易的周记 0），再对“块”做 Bootstrap。
    多币同时持仓、行情阶段聚集时逐笔并不独立，逐笔 Bootstrap 的区间会偏窄；按块抽样更保守（本框架新增）。"""
    df = trades.dropna(subset=["R"])
    if len(df) < 5 or time_col not in df:
        return {"error": "至少需要 5 笔交易和时间列"}
    t = pd.to_datetime(df[time_col], utc=True)
    per = pd.Series(df["R"].to_numpy(float), index=t).resample(freq).sum()
    v = per.to_numpy(float)
    if v.size < 5:
        return {"error": f"按 {freq} 汇总后只有 {v.size} 个时间块"}
    rng = np.random.default_rng(seed)
    means = rng.choice(v, size=(n_boot, v.size), replace=True).mean(axis=1)
    a = (1 - confidence) / 2
    return {"freq": freq, "blocks": int(v.size), "mean_R_per_block": round(float(v.mean()), 4),
            "ci_lower": round(float(np.quantile(means, a)), 4), "ci_upper": round(float(np.quantile(means, 1 - a)), 4),
            "confidence": confidence, "n_boot": n_boot}


# ======================================================================== 3. 是不是搜出来的（移植自 multipletesting.py）
def probabilistic_sharpe_ratio(observed_sharpe: float, n_observations: int, benchmark_sharpe: float = 0.0,
                               skew: float = 0.0, kurtosis: float = GAUSSIAN_KURTOSIS) -> float:
    """真实 Sharpe 超过基准的置信度。Sharpe 必须是每期口径；峰度必须是非超额（正态为 3）。"""
    for name, val in (("observed_sharpe", observed_sharpe), ("n_observations", n_observations),
                      ("benchmark_sharpe", benchmark_sharpe), ("skew", skew), ("kurtosis", kurtosis)):
        if not np.isfinite(val):
            raise ValueError(f"{name} 必须是有限数，实际 {val}")
    if n_observations < MIN_OBSERVATIONS:
        raise ValueError(f"至少需要 {MIN_OBSERVATIONS} 个观测，实际 {n_observations}")
    if kurtosis < 1.0:
        raise ValueError(f"峰度必须是非超额峰度（≥ 1，正态为 3），实际 {kurtosis}；可能误传了超额峰度")
    var_term = 1.0 - skew * observed_sharpe + (kurtosis - 1.0) / 4.0 * observed_sharpe ** 2
    if var_term <= 0:
        raise ValueError(f"Sharpe 方差项为 {var_term:.4f}，偏度与峰度和该 Sharpe 互相矛盾")
    stat = (observed_sharpe - benchmark_sharpe) * math.sqrt(n_observations - 1) / math.sqrt(var_term)
    return float(_N.cdf(stat))


def expected_maximum_sharpe(n_trials: int, trial_sharpe_std: float) -> float:
    """只靠运气，n_trials 次尝试里最好的那个预期能达到的 Sharpe（每期口径）。"""
    if not (np.isfinite(n_trials) and np.isfinite(trial_sharpe_std)):
        raise ValueError("n_trials 与 trial_sharpe_std 必须是有限数")
    if n_trials < 1 or trial_sharpe_std < 0:
        raise ValueError("n_trials 必须 ≥ 1，trial_sharpe_std 必须 ≥ 0")
    if n_trials == 1 or trial_sharpe_std == 0:
        return 0.0
    qa = _N.inv_cdf(1.0 - 1.0 / n_trials)
    qb = _N.inv_cdf(1.0 - 1.0 / (n_trials * math.e))
    return float(trial_sharpe_std * ((1 - EULER_MASCHERONI) * qa + EULER_MASCHERONI * qb))


@dataclass(frozen=True)
class DeflatedSharpeResult:
    observed_sharpe: float
    expected_maximum_sharpe: float
    deflated_sharpe_ratio: float
    n_trials: int
    n_observations: int
    trial_sharpe_std: float
    skew: float
    kurtosis: float
    survives: bool
    confidence: float


def deflated_sharpe_ratio(observed_sharpe: float, n_trials: int, n_observations: int, trial_sharpe_std: float,
                          skew: float = 0.0, kurtosis: float = GAUSSIAN_KURTOSIS,
                          confidence: float = 0.95) -> DeflatedSharpeResult:
    """把基准抬高到“n_trials 次尝试中最好者的运气水平”后的 PSR。n_trials 必须如实记录。"""
    if not 0 < confidence < 1:
        raise ValueError("confidence 必须在 (0, 1)")
    bar = expected_maximum_sharpe(n_trials, trial_sharpe_std)
    dsr = probabilistic_sharpe_ratio(observed_sharpe, n_observations, bar, skew, kurtosis)
    return DeflatedSharpeResult(observed_sharpe, bar, dsr, n_trials, n_observations, trial_sharpe_std, skew, kurtosis,
                                dsr > confidence, confidence)


@dataclass(frozen=True)
class FDRResult:
    rejected: np.ndarray
    adjusted_p_values: np.ndarray
    n_rejected: int
    threshold: float
    fdr: float


def benjamini_hochberg(p_values, fdr: float = 0.1) -> FDRResult:
    """控制错误发现率：一次筛很多因子时，用它判断“哪些是真的”。"""
    if not 0 < fdr < 1:
        raise ValueError("fdr 必须在 (0, 1)")
    v = np.asarray(p_values, dtype=float).ravel()
    if v.size == 0 or not np.isfinite(v).all() or ((v < 0) | (v > 1)).any():
        raise ValueError("p 值必须非空、有限且在 [0, 1]")
    n = v.size
    order = np.argsort(v, kind="stable")
    sp = v[order]
    ranks = np.arange(1, n + 1)
    below = sp <= ranks / n * fdr
    cut = int(ranks[below].max()) if below.any() else 0
    thr = float(sp[cut - 1]) if cut else 0.0
    adj_sorted = np.clip(np.minimum.accumulate((n / ranks * sp)[::-1])[::-1], 0, 1)
    rej, adj = np.empty(n, bool), np.empty(n, float)
    rej[order], adj[order] = ranks <= cut, adj_sorted
    return FDRResult(rej, adj, int(rej.sum()), thr, fdr)


@dataclass(frozen=True)
class CSCVResult:
    pbo: float
    n_splits: int
    n_strategies: int
    n_observations: int
    dropped_observations: int
    performance_degradation: float


def probability_of_backtest_overfitting(performance: pd.DataFrame | np.ndarray, n_splits: int = 16,
                                        ddof: int = 1) -> CSCVResult:
    """CSCV：把样本切成 n_splits 段，每种“一半样本内 / 一半样本外”的组合里，样本内最好的方案
    在样本外排到中位数以下的比例。> 0.5 说明“挑最好的”这一步本身没有信息量。行 = 时间，列 = 方案。"""
    if n_splits < 4 or n_splits % 2:
        raise ValueError("n_splits 必须是 ≥ 4 的偶数")
    m = pd.DataFrame(performance).to_numpy(dtype=float)
    rows, k = m.shape
    if k < 2:
        raise ValueError("至少需要 2 个方案")
    size = rows // n_splits
    if size < 2:
        raise ValueError(f"{rows} 行切 {n_splits} 段，每段不足 2 行")
    used = size * n_splits
    subs = [m[i * size:(i + 1) * size] for i in range(n_splits)]
    logits, ins, outs = [], [], []
    allidx = set(range(n_splits))
    for chosen in combinations(range(n_splits), n_splits // 2):
        rest = sorted(allidx - set(chosen))
        a, b = np.vstack([subs[i] for i in chosen]), np.vstack([subs[i] for i in rest])
        si = np.array([_safe_sharpe(a[:, j], ddof) for j in range(k)])
        if not np.isfinite(si).any():
            continue
        best = int(np.nanargmax(np.where(np.isfinite(si), si, -np.inf)))
        so = np.array([_safe_sharpe(b[:, j], ddof) for j in range(k)])
        fin = np.isfinite(so)
        if not fin[best] or fin.sum() < 2:
            continue
        rk = pd.Series(np.where(fin, so, np.nan)).rank(method="average")
        w = min(max(float(rk.iloc[best] / (fin.sum() + 1)), 1e-12), 1 - 1e-12)
        logits.append(math.log(w / (1 - w)))
        ins.append(float(si[best]))
        outs.append(float(so[best]))
    if not logits:
        raise ValueError("没有任何一种切分得到可用的 Sharpe")
    ia, oa = np.array(ins), np.array(outs)
    deg = float(np.polyfit(ia, oa, 1)[0]) if ia.size >= 2 and ia.std() > 0 else float("nan")
    return CSCVResult(float((np.array(logits) <= 0).mean()), len(logits), k, used, rows - used, deg)


def _safe_sharpe(x, ddof):
    try:
        return sharpe_ratio(x, ddof=ddof)
    except ValueError:
        return float("nan")


# ======================================================================== 汇总判定
def evaluate_trades(trades: pd.DataFrame, risk_fraction: float = 0.005, n_trials: int = 1,
                    trial_sharpe_std: float | None = None, gates: dict | None = None, time_col: str = "time",
                    seed: int = 42, holdout_start: str | None = None) -> dict[str, Any]:
    """对一组逐笔交易（列 R 与 time_col）跑全部检验，给出“通过 / 不通过 / 证据不足”。
    判定（v2.6 起）：任一必需检验不通过 → 不通过；任一必需检验无法判定（缺样本外段、缺 DSR 所需数据等）→ 证据不足；
    笔数 < pass_min_trades（100）→ 证据不足；其余 → 通过。
    必需检验：Bootstrap 期望 R、符号翻转、按时间块 Bootstrap、分段一致性、Deflated Sharpe、样本外段。
    n_trials：为了得到这条策略总共试过多少种方案（含想法登记簿里回测过的想法）；只试过 1 种时 DSR 退化为 PSR。
    holdout_start：样本外段起点（UTC 日期）；之前的数据用于选参数，之后的数据只看一次。"""
    g = {**DEFAULT_GATES, **(gates or {})}
    r = _clean(trades["R"]) if "R" in trades else np.array([])
    out: dict[str, Any] = {"n_trades": int(r.size), "gates": g, "gate_label": "示例值（未经数据验证）"}
    if r.size < g["min_trades"]:
        out.update(verdict="证据不足", reason=f"交易 {r.size} 笔 < {int(g['min_trades'])} 笔")
        if r.size >= 5:
            out["bootstrap_mean_R"] = bootstrap_mean_ci(r, confidence=g["confidence"], seed=seed)
        return out

    checks = {}
    bm = bootstrap_mean_ci(r, confidence=g["confidence"], seed=seed)
    checks["bootstrap_mean_R"] = {"pass": bm["ci_lower"] > g["boot_lower_R"], **bm}
    sf = sign_flip_test(r, seed=seed)
    checks["sign_flip"] = {"pass": sf["p_value"] < g["sign_flip_p"], **sf,
                           "note": "假设零假设下 R 关于 0 对称；止损截断、盈利长尾时只作参考，以 Bootstrap 为主"}
    bb = block_bootstrap_mean_ci(trades, time_col, g["block_freq"], confidence=g["confidence"], seed=seed) \
        if time_col in trades else {"error": f"缺少时间列 {time_col}"}
    checks["block_bootstrap"] = {"pass": (bb["ci_lower"] > 0) if "error" not in bb else None, **bb}
    wf = walk_forward(trades, 5, time_col) if time_col in trades else {"error": f"缺少时间列 {time_col}"}
    checks["segment_consistency"] = {"pass": (wf["consistency_rate"] >= g["wf_consistency"]) if "error" not in wf else None, **wf}

    sr = sharpe_ratio(r)
    sk, ku = moments(r)
    info = {"sharpe_per_trade": round(sr, 4), "skew": round(sk, 3), "kurtosis": round(ku, 3)}
    try:
        info["psr_vs_0"] = round(probabilistic_sharpe_ratio(sr, int(r.size), 0.0, sk, ku), 4)
    except ValueError as e:
        info["psr_error"] = str(e)
    if n_trials >= 2 and trial_sharpe_std is not None and np.isfinite(sr):
        try:
            d = deflated_sharpe_ratio(sr, int(n_trials), int(r.size), float(trial_sharpe_std), sk, ku, g["confidence"])
            checks["deflated_sharpe"] = {"pass": d.deflated_sharpe_ratio > g["dsr"],
                                         **{k: (round(v, 4) if isinstance(v, float) else v) for k, v in asdict(d).items()}}
        except ValueError as e:
            checks["deflated_sharpe"] = {"pass": None, "error": str(e)}
    elif n_trials < 2 and "psr_vs_0" in info:
        checks["deflated_sharpe"] = {"pass": info["psr_vs_0"] > g["dsr"], "deflated_sharpe_ratio": info["psr_vs_0"],
                                     "n_trials": 1, "note": "只登记了 1 种方案：DSR 退化为 PSR（请如实登记尝试次数）"}
    else:
        checks["deflated_sharpe"] = {"pass": None, "note": "尝试 ≥ 2 次但缺各方案 Sharpe 离散度：无法折扣"}

    if holdout_start and time_col in trades:
        t = pd.to_datetime(trades[time_col], utc=True)
        ho = _clean(trades.loc[(t >= pd.Timestamp(holdout_start, tz="UTC")).values, "R"])
        if ho.size < g["holdout_min_trades"]:
            checks["holdout"] = {"pass": None, "n": int(ho.size), "start": holdout_start,
                                 "error": f"样本外段只有 {ho.size} 笔 < {int(g['holdout_min_trades'])} 笔"}
        else:
            checks["holdout"] = {"pass": float(ho.mean()) > 0, "n": int(ho.size), "start": holdout_start,
                                 "mean_R": round(float(ho.mean()), 4)}
    else:
        checks["holdout"] = {"pass": None, "note": "未指定样本外段（--holdout-start）"}

    out["sharpe"] = info
    out["bootstrap_sharpe"] = bootstrap_sharpe_ci(r, confidence=g["confidence"], seed=seed)
    out["monte_carlo_path"] = monte_carlo_permutation(r, risk_fraction, seed=seed)
    out["checks"] = checks
    out["failed"] = [k for k, c in checks.items() if c.get("pass") is False]
    out["undetermined"] = [k for k, c in checks.items() if c.get("pass") is None]
    if out["failed"]:
        out["verdict"] = "不通过"
    elif out["undetermined"]:
        out.update(verdict="证据不足", reason="无法判定：" + "、".join(out["undetermined"]))
    elif r.size < g["pass_min_trades"]:
        out.update(verdict="证据不足", reason=f"交易 {r.size} 笔 < {int(g['pass_min_trades'])} 笔（G1）")
    else:
        out["verdict"] = "通过"
    return out


def combine_verdicts(parts: dict[str, str | bool | None]) -> tuple[str, list[str], list[str]]:
    """多个判定合成一个：任一“不通过”/False → 不通过；任一“证据不足”/None → 证据不足；否则通过。"""
    failed = [k for k, v in parts.items() if v is False or v == "不通过"]
    missing = [k for k, v in parts.items() if v is None or v == "证据不足"]
    return ("不通过" if failed else "证据不足" if missing else "通过"), failed, missing


def evaluate_trials(per_trial: dict[str, pd.DataFrame], time_col: str = "time", freq: str = "MS",
                    n_splits: int | None = None) -> dict[str, Any]:
    """多方案（研究矩阵里的各 variant）：每个方案的每笔 Sharpe、离散度（供 DSR），以及按月汇总 R 后的 PBO。"""
    rows, monthly = {}, {}
    for name, df in per_trial.items():
        r = _clean(df["R"]) if len(df) else np.array([])
        rows[name] = {"n_trades": int(r.size), "expectancy_R": round(float(r.mean()), 4) if r.size else None,
                      "sharpe_per_trade": round(_safe_sharpe(r, 1), 4) if r.size >= 2 else None}
        if len(df) and time_col in df:
            t = pd.to_datetime(df[time_col], utc=True)
            monthly[name] = pd.Series(df["R"].to_numpy(float), index=t).resample(freq).sum()
    sh = [v["sharpe_per_trade"] for v in rows.values() if v["sharpe_per_trade"] is not None and np.isfinite(v["sharpe_per_trade"])]
    out: dict[str, Any] = {"trials": rows, "n_trials": len(per_trial),
                           "trial_sharpe_std": round(float(np.std(sh, ddof=1)), 4) if len(sh) >= 2 else None}
    if len(monthly) >= 2:
        mat = pd.DataFrame(monthly).fillna(0.0)
        ns = n_splits or max(4, min(16, (len(mat) // 2) // 2 * 2))
        try:
            p = probability_of_backtest_overfitting(mat, ns)
            out["pbo"] = {**asdict(p), "months": len(mat)}
        except ValueError as e:
            out["pbo"] = {"error": str(e), "months": len(mat)}
    else:
        out["pbo"] = {"note": "方案少于 2 个，不计算 PBO"}
    return out
