"""机器学习层测试：防泄漏切分、真信号能被识别、纯噪声被拒绝、门控只降不升。"""
from __future__ import annotations

import os
import sys
from dataclasses import replace

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from s03.config import MLConfig  # noqa: E402
from s03.ml.features import FEATURES  # noqa: E402
from s03.ml.gate import MLGate, approve, save_bundle  # noqa: E402
from s03.ml.validate import evaluate, purged_walk_forward, train_final  # noqa: E402


def _events(n=900, signal=True, seed=1):
    rng = np.random.default_rng(seed)
    t = pd.date_range("2021-01-01", periods=n, freq="2D", tz="UTC")
    X = pd.DataFrame(rng.normal(size=(n, len(FEATURES))), columns=FEATURES)
    noise = rng.normal(scale=1.5, size=n)
    r = (1.2 * X["vol_14d"] if signal else 0) * -1 + noise - 0.1     # 波动越低越好（人为设定的测试信号）
    df = X.assign(signal_time=t, exit_time=t + pd.Timedelta(days=10), R=r, coin="SOL")
    return df


def test_purged_split_no_leakage():
    cfg = MLConfig()
    ev = _events()
    for tr, te in purged_walk_forward(ev, cfg):
        assert ev.loc[tr, "exit_time"].max() < ev.loc[te, "signal_time"].min() - pd.Timedelta(days=cfg.purge_days)


def test_detects_real_signal():
    res = evaluate(_events(signal=True), replace(MLConfig(), mc_trials=300))
    assert res["verdict"] == "可提交人工审批", res


def test_rejects_noise():
    res = evaluate(_events(signal=False, seed=7), replace(MLConfig(), mc_trials=300))
    assert res["verdict"] == "不采用", res


def test_insufficient_events():
    res = evaluate(_events(n=100), MLConfig())
    assert res["verdict"] == "证据不足"


def test_gate_never_scales_up_and_requires_approval(tmp_path):
    cfg = replace(MLConfig(), enabled=True, model_path=str(tmp_path / "m.pkl"))
    ev = _events()
    bundle = train_final(ev, cfg)
    save_bundle(bundle, cfg.model_path)
    hpath = str(tmp_path / "APPROVED")
    assert MLGate(cfg, hpath).status == "missing_model_or_approval"      # 未批准 → 不加载
    approve(cfg.model_path, hpath)
    g = MLGate(cfg, hpath)
    assert g.status == "loaded"
    for i in range(50):
        scale, _ = g.decide(ev.iloc[i][FEATURES].to_dict(), now=bundle["trained_at"])
        assert 0 <= scale <= 1.0
    late = bundle["trained_at"] + pd.Timedelta(hours=cfg.expiration_hours + 1)
    assert g.decide(ev.iloc[0][FEATURES].to_dict(), now=late) == (1.0, "model_expired")
    open(cfg.model_path, "ab").write(b"tamper")
    assert MLGate(cfg, hpath).status == "hash_mismatch"
