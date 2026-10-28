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
    assert json.loads(r.stdout)["verdict"] in ("通过", "不通过")
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
