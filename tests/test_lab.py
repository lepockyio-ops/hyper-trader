"""hyper-trader 的研究前端接入：研究矩阵逐笔文件格式、run.py lab 的默认参数与边界。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import research_matrix as rm  # noqa: E402


def test_save_trades_standard_columns(tmp_path, monkeypatch):
    monkeypatch.setattr(rm, "RES", tmp_path)
    t = pd.DataFrame({"coin": "BTC", "entry_time": pd.date_range("2024-01-01", periods=3, tz="UTC"),
                      "exit_time": pd.date_range("2024-01-02", periods=3, tz="UTC"), "R": [1.0, -1.0, 2.0],
                      "pnl": [50.0, -50.0, 100.0], "fees": 0.0})
    p = rm.save_trades("BTC-2024-A-base", t)
    df = pd.read_csv(p)
    assert list(df.columns) == ["job", "coin", "year", "impl", "variant", "entry_time", "time", "R", "pnl"]
    assert df["variant"].unique().tolist() == ["base"] and np.isclose(df["R"].sum(), 2.0)
    assert len(pd.read_csv(rm.save_trades("BTC-2025-A-base", pd.DataFrame()))) == 0


def _run(*args, cwd):
    return subprocess.run([sys.executable, str(ROOT / "scripts" / "run.py"), "lab", *args], cwd=cwd,
                          capture_output=True, text=True, timeout=300, env={**os.environ, "PYTHONPATH": str(ROOT)})


def test_lab_stats_and_guard_refusal(tmp_path):
    rng = np.random.default_rng(0)
    pd.DataFrame({"time": pd.date_range("2023-01-01", periods=120, freq="3D", tz="UTC"),
                  "R": rng.normal(0.4, 1, 120)}).to_csv(tmp_path / "t.csv", index=False)
    r = _run("stats", "--trades", "t.csv", "--out-dir", "lab", cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["verdict"] in ("通过", "不通过", "证据不足")
    assert _run("mcp-guard", "--", "true", cwd=tmp_path).returncode == 2


def test_lab_factors_defaults_to_research_data(tmp_path):
    d = tmp_path / "research" / "data"
    d.mkdir(parents=True)
    idx = pd.date_range("2023-01-01", periods=300, freq="D", tz="UTC")
    rng = np.random.default_rng(1)
    for c in ("BTC", "ETH"):
        close = 100 * np.exp(np.cumsum(rng.normal(0, 0.02, 300)))
        pd.DataFrame({"open": close, "high": close * 1.01, "low": close * 0.99, "close": close, "volume": 1000.0},
                     index=pd.Index(idx, name="time")).to_csv(d / f"{c}_1Dutc.csv")
    r = _run("factors", "--coins", "BTC,ETH", "--ids", "q158_roc20,q158_kmid", "--out-dir", "lab", cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["mode"] == "time_series"


def test_lab_news_blocks_disabled_by_default(tmp_path):
    r = _run("news", "blocks", "--guard-out", str(tmp_path / "none.json"), cwd=tmp_path)
    assert r.returncode == 0 and json.loads(r.stdout)["status"] == "disabled"


# ------------------------------------------------------------------ v2.6：研究流程可复核、组合回测
def test_base_variant_is_pinned_and_variants_change_one_thing():
    base, floor = rm.settings_for("base"), rm.settings_for("stop_floor")
    assert base.strategy.stop_min_mode == "skip" and floor.strategy.stop_min_mode == "floor"
    assert base.strategy.event_file == ""
    c2, f2 = rm.settings_for("cost2x"), rm.settings_for("funding2x")
    assert c2.cost.slippage == base.cost.slippage * 3 and c2.cost.funding_per_hour_est == base.cost.funding_per_hour_est
    assert f2.cost.funding_per_hour_est == base.cost.funding_per_hour_est * 2 and f2.cost.slippage == base.cost.slippage


def _synthetic_data(d, coins=("BTC", "SOL")):
    d.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(1)
    for c in coins:
        for bar, freq, n in (("1Dutc", "D", 500), ("15m", "15min", 3000)):
            idx = pd.date_range("2024-01-01" if bar == "15m" else "2022-09-01", periods=n, freq=freq, tz="UTC")
            px = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
            pd.DataFrame({"open": px, "high": px * 1.01, "low": px * 0.99, "close": px, "volume": 1.0},
                         index=idx).to_csv(d / f"{c}_{bar}.csv")


def test_plan_manifest_aggregate_excludes_other_versions_and_portfolio_runs(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(rm, "ROOT", tmp_path / "research")
    monkeypatch.setattr(rm, "RES", tmp_path / "research" / "results")
    monkeypatch.setattr(rm, "DATA", tmp_path / "research" / "data")
    _synthetic_data(rm.DATA)
    ns = type("A", (), {})
    a = ns(); a.coins, a.years, a.impl, a.variants = "BTC", "2024-2024", "A", "base"
    assert rm.cmd_plan(a) == 0
    m = rm.load_manifest()
    assert m["jobs"] == ["BTC-2024-A-base"] and len(m["fingerprint"]) == 16
    a.job = "BTC-2024-A-base"
    assert rm.cmd_run(a) == 0
    assert rm.cmd_aggregate(a) == 0
    meta = json.loads((rm.RES / "BTC-2024-A-base.json").read_text("utf-8"))
    meta["fingerprint"] = "other-version"
    (rm.RES / "BTC-2024-A-base.json").write_text(json.dumps(meta), "utf-8")
    assert rm.cmd_aggregate(a) == 1                       # 版本不一致：不汇总
    p = ns(); p.coins, p.since, p.impl, p.variant = "BTC,SOL", "2024-01-01", "A", "base"
    assert rm.cmd_portfolio(p) == 0
    res = json.loads((rm.RES / "portfolio" / "base.json").read_text("utf-8"))
    assert res["per_coin_signals"].keys() == {"BTC", "SOL"} and "近似" in res["note"]
