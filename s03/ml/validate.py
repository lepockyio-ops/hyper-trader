"""净化滚动验证 + Monte Carlo + 验收（第 14 节）。

方法来源：
- FreqAI：滑动训练窗口（train_period_days）、按段重训（backtest_period_days）、shuffle=False、
  训练集离群检测（SVM / DI），离群样本在使用时退回“无过滤”。
- Vibe-Trading：walk-forward、purged CV、Monte Carlo 显著性；算不出的值写“未知”。

输出是“改动提案”，只写到 var/ml/，绝不修改 config/settings.toml。
"""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pandas as pd

from s03.config import MLConfig
from s03.ml.features import FEATURES


def make_model():
    try:
        from lightgbm import LGBMClassifier                    # 与 FreqAI 示例相同的模型族（可选）

        return LGBMClassifier(n_estimators=200, learning_rate=0.03, num_leaves=15, min_child_samples=20,
                              subsample=0.8, colsample_bytree=0.8, verbose=-1)
    except ImportError:
        from sklearn.ensemble import HistGradientBoostingClassifier

        return HistGradientBoostingClassifier(max_depth=3, learning_rate=0.05, max_iter=200,
                                              min_samples_leaf=20, l2_regularization=1.0)


def make_outlier_detector(method: str):
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    if method == "svm":
        from sklearn.svm import OneClassSVM

        return make_pipeline(StandardScaler(), OneClassSVM(nu=0.05, gamma="scale"))
    from sklearn.ensemble import IsolationForest

    return make_pipeline(StandardScaler(), IsolationForest(contamination=0.05, random_state=0))


def purged_walk_forward(events: pd.DataFrame, cfg: MLConfig):
    """产出 (train_idx, test_idx)。训练集只含 exit_time 早于 “测试开始 − purge − embargo” 的事件。"""
    ev = events.sort_values("signal_time")
    t_min, t_max = ev["signal_time"].min(), ev["signal_time"].max()
    test_start = t_min + pd.Timedelta(days=cfg.train_period_days)
    while test_start < t_max:
        test_end = test_start + pd.Timedelta(days=cfg.test_period_days)
        cutoff = test_start - pd.Timedelta(days=cfg.purge_days + cfg.embargo_days)
        train = ev[(ev["signal_time"] >= test_start - pd.Timedelta(days=cfg.train_period_days))
                   & (ev["exit_time"] < cutoff)]
        test = ev[(ev["signal_time"] >= test_start) & (ev["signal_time"] < test_end)]
        if len(train) and len(test):
            yield train.index, test.index
        test_start = test_end


def _stats(r: pd.Series) -> dict:
    if len(r) == 0:
        return {"n": 0, "expectancy_R": None, "win_rate": None, "profit_factor": None}
    wins, losses = r[r > 0].sum(), -r[r <= 0].sum()
    return {"n": int(len(r)), "expectancy_R": round(float(r.mean()), 3),
            "win_rate": round(float((r > 0).mean()), 3),
            "profit_factor": round(float(wins / losses), 2) if losses > 0 else None}


def evaluate(events: pd.DataFrame, cfg: MLConfig, seed: int = 0) -> dict:
    if len(events) < cfg.min_events:
        return {"verdict": "证据不足", "reason": f"事件数 {len(events)} < {cfg.min_events}",
                "base": _stats(events["R"]) if len(events) else _stats(pd.Series(dtype=float)),
                "ml": None, "p_value": "未知"}
    ev = events.sort_values("signal_time").reset_index(drop=True)
    take = pd.Series(np.nan, index=ev.index)
    folds = 0
    for tr, te in purged_walk_forward(ev, cfg):
        Xtr, ytr = ev.loc[tr, FEATURES], (ev.loc[tr, "R"] > 0).astype(int)
        if ytr.nunique() < 2 or len(tr) < 50:
            continue
        model = make_model().fit(Xtr.fillna(Xtr.median()), ytr)
        od = make_outlier_detector(cfg.outlier_method).fit(Xtr.fillna(Xtr.median()))
        Xte = ev.loc[te, FEATURES].fillna(Xtr.median())
        p = model.predict_proba(Xte)[:, 1]
        inlier = od.predict(Xte) == 1
        take.loc[te] = np.where(inlier, p >= cfg.take_threshold, True).astype(float)   # 离群：退回无过滤
        folds += 1
    tested = take.dropna().index
    if folds == 0 or len(tested) == 0:
        return {"verdict": "证据不足", "reason": "没有可用的验证段", "base": _stats(ev["R"]), "ml": None,
                "p_value": "未知"}
    base_r = ev.loc[tested, "R"]
    sel = take.loc[tested].astype(bool)
    ml_r = base_r[sel]
    take_rate = float(sel.mean())
    rng = np.random.default_rng(seed)
    k = int(sel.sum())
    rand = np.array([base_r.sample(n=k, random_state=int(rng.integers(1e9))).mean() for _ in range(cfg.mc_trials)]) if k else np.array([])
    p_value = float((rand >= ml_r.mean()).mean()) if k else 1.0
    base_s, ml_s = _stats(base_r), _stats(ml_r)
    gain = (ml_s["expectancy_R"] or 0) - (base_s["expectancy_R"] or 0)
    checks = {
        "expectancy_gain_ok": gain >= cfg.min_expectancy_gain_r,
        "take_rate_ok": take_rate >= cfg.min_take_rate,
        "p_value_ok": p_value <= cfg.max_p_value,
        "ml_positive": (ml_s["expectancy_R"] or 0) > 0,
    }
    return {"verdict": "可提交人工审批" if all(checks.values()) else "不采用", "folds": folds,
            "base": base_s, "ml": ml_s, "take_rate": round(take_rate, 3), "expectancy_gain_R": round(gain, 3),
            "p_value": round(p_value, 4), "checks": checks}


def write_proposal(result: dict, cfg: MLConfig, out_dir: str = "var/ml") -> Path:
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    stamp = pd.Timestamp.now(tz="UTC").strftime("%Y%m%dT%H%M")
    path = Path(out_dir) / f"proposal_{stamp}.json"
    path.write_text(json.dumps({"created": stamp, "config": asdict(cfg), "result": result,
                                "note": "只是提案：需你审批，并重新通过 G2–G8 后才能把 ml.enabled 设为 true"},
                               ensure_ascii=False, indent=2, default=str), "utf-8")
    return path


def train_final(events: pd.DataFrame, cfg: MLConfig) -> dict:
    """用最近 train_period_days 的事件训练最终模型（只在验收通过后调用）。"""
    cutoff = events["signal_time"].max() - pd.Timedelta(days=cfg.train_period_days)
    tr = events[events["signal_time"] >= cutoff]
    X = tr[FEATURES]
    med = X.median()
    model = make_model().fit(X.fillna(med), (tr["R"] > 0).astype(int))
    od = make_outlier_detector(cfg.outlier_method).fit(X.fillna(med))
    return {"model": model, "outlier": od, "medians": med.to_dict(), "features": FEATURES,
            "trained_at": pd.Timestamp.now(tz="UTC"), "n_train": len(tr)}
