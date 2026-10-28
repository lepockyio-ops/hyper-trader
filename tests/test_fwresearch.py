"""fwresearch（研究前端）的测试：统计检验、因子防前视、想法登记簿流程、MCP 白名单代理、数据读取与命令行。
两个模板仓库共用同一份测试。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from fwresearch import stats
from fwresearch.factors import evaluate, zoo
from fwresearch.ideas import IdeaBook

ROOT = Path(__file__).resolve().parents[1]


# ------------------------------------------------------------------ 合成数据
def trades(mean_r: float, n: int, seed: int = 1, sd: float = 1.0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    t = pd.date_range("2022-01-01", periods=n, freq="3D", tz="UTC")
    return pd.DataFrame({"time": t, "R": rng.normal(mean_r, sd, n)})


def panel(n: int = 400, assets=("BTC", "ETH", "SOL", "BNB"), seed: int = 0) -> dict:
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2022-01-01", periods=n, freq="D", tz="UTC")
    out = {k: {} for k in ("open", "high", "low", "close", "volume", "funding")}
    for a in assets:
        c = 100 * np.exp(np.cumsum(rng.normal(0, 0.03, n)))
        o = c * (1 + rng.normal(0, 0.005, n))
        out["close"][a], out["open"][a] = c, o
        out["high"][a] = np.maximum(c, o) * (1 + rng.uniform(0, 0.02, n))
        out["low"][a] = np.minimum(c, o) * (1 - rng.uniform(0, 0.02, n))
        out["volume"][a] = rng.uniform(1e3, 1e4, n)
        out["funding"][a] = rng.normal(1e-4, 1e-4, n)
    return {k: pd.DataFrame(v, index=idx) for k, v in out.items()}


# ------------------------------------------------------------------ 统计检验
def test_bootstrap_and_sign_flip_detect_real_edge():
    r = trades(0.35, 300)["R"]
    b = stats.bootstrap_mean_ci(r)
    assert b["ci_lower"] > 0 and b["ci_lower"] < b["mean_R"] < b["ci_upper"]
    assert stats.sign_flip_test(r)["p_value"] < 0.01


def test_no_edge_is_not_significant():
    r = trades(0.0, 300, seed=7)["R"]
    assert stats.sign_flip_test(r)["p_value"] > 0.05
    assert stats.bootstrap_mean_ci(r)["ci_lower"] < 0


def test_permutation_keeps_mean_and_only_tests_path():
    r = trades(0.2, 100)["R"].to_numpy()
    rng = np.random.default_rng(0)
    assert np.isclose(rng.permutation(r).mean(), r.mean())
    m = stats.monte_carlo_permutation(r, n_sim=200)
    assert 0 <= m["p_value_max_dd"] <= 1 and m["n"] == 100


def test_evaluate_trades_verdicts():
    assert stats.evaluate_trades(trades(0.4, 20))["verdict"] == "证据不足"
    good = stats.evaluate_trades(trades(0.4, 300))
    assert good["verdict"] == "通过", good.get("failed")
    bad = stats.evaluate_trades(trades(-0.1, 300, seed=3))
    assert bad["verdict"] == "不通过" and "bootstrap_mean_R" in bad["failed"]


def test_deflation_bites_with_many_trials():
    df = trades(0.12, 300, seed=11)
    one = stats.evaluate_trades(df, n_trials=1)
    many = stats.evaluate_trades(df, n_trials=200, trial_sharpe_std=0.08)
    assert one["checks"]["deflated_sharpe"]["pass"] is None
    d = many["checks"]["deflated_sharpe"]
    assert d["expected_maximum_sharpe"] > 0 and d["deflated_sharpe_ratio"] < many["sharpe"]["psr_vs_0"]


def test_ported_formulas_known_values():
    # 期望最大 Sharpe：N=1 或离散度 0 时为 0；N 越大越高
    assert stats.expected_maximum_sharpe(1, 0.5) == 0.0
    assert stats.expected_maximum_sharpe(100, 0.1) > stats.expected_maximum_sharpe(10, 0.1) > 0
    # PSR：Sharpe 等于基准时为 0.5
    assert abs(stats.probabilistic_sharpe_ratio(0.1, 100, 0.1) - 0.5) < 1e-12
    with pytest.raises(ValueError):
        stats.probabilistic_sharpe_ratio(0.1, 100, kurtosis=0.0)       # 误传超额峰度
    # BH：经典例子
    res = stats.benjamini_hochberg([0.01, 0.04, 0.03, 0.005], fdr=0.05)
    assert res.n_rejected == 4 and np.allclose(res.adjusted_p_values, [0.02, 0.04, 0.04, 0.02])


def test_pbo_low_for_real_edge_high_for_noise():
    rng = np.random.default_rng(5)
    noise = pd.DataFrame(rng.normal(0, 1, (160, 8)))
    assert stats.probability_of_backtest_overfitting(noise, 8).pbo > 0.2
    edge = noise.copy()
    edge[0] += 0.8
    assert stats.probability_of_backtest_overfitting(edge, 8).pbo < 0.1


def test_walk_forward_windows():
    wf = stats.walk_forward(trades(0.3, 200), 5)
    assert wf["windows_with_trades"] == 5 and sum(w["trades"] for w in wf["windows"]) == 200


# ------------------------------------------------------------------ 因子
@pytest.mark.parametrize("fid", sorted(zoo.REGISTRY))
def test_every_builtin_factor_is_lookahead_free(fid):
    p = panel(260)
    f = zoo.REGISTRY[fid]
    assert evaluate.truncation_safe(f, p, 200), f"{fid} 看到了未来数据"
    v = zoo.compute(f, p)
    assert v.shape == p["close"].shape and not np.isinf(v.to_numpy()).any()


def test_truncation_check_catches_a_peeking_factor():
    p = panel(200)
    cheat = zoo.Factor("cheat", "x", "Ref(close,-1)", lambda q: q["close"].shift(-1) / q["close"])
    assert not evaluate.truncation_safe(cheat, p, 150)


def test_forward_returns_non_overlapping():
    c = panel(30)["close"]
    f = evaluate.forward_returns(c, 5)
    rows = f.notna().any(axis=1)
    assert rows.sum() <= 6 and not f.iloc[-1].notna().any()


def test_ts_ic_finds_planted_signal_and_cs_needs_five_assets():
    p = panel(600, seed=2)
    ret = evaluate.forward_returns(p["close"], 1)
    rng = np.random.default_rng(9)
    good = ret + rng.normal(0, ret.abs().mean().mean(), ret.shape)     # 与下一期收益相关、但只用到“已知”值的外部因子
    s = evaluate.summarize_ic(evaluate.ts_ic(good, p["close"]))
    assert s["category"] == "alive" and s["t"] > 5
    assert evaluate.cs_ic(good, ret, min_assets=5).empty                 # 4 个标的：截面 IC 不可用
    res = evaluate.evaluate(p, zoo.available(p)[:6], horizon=1)
    assert res["mode"] == "time_series" and res["n_factors"] == 6
    with pytest.raises(ValueError):
        evaluate.evaluate(p, zoo.available(p)[:2], mode="cross_section")


@pytest.mark.parametrize("fid", ["q158_ma60", "q158_roc60", "q158_rsv60", "q158_max60"])
def test_slow_factors_are_not_significant_on_random_walks(fid):
    """null 检验：随机游走上不应有因子显著。旧的“月内 Spearman”口径在这里 t≈15，全部误判为 alive / reversed。"""
    ts = [evaluate.summarize_ic(evaluate.ts_ic(zoo.compute(zoo.REGISTRY[fid], p), p["close"]))["t"]
          for p in (panel(900, seed=s) for s in range(6))]
    assert abs(float(np.mean(ts))) < 1.5 and sum(abs(t) > 3 for t in ts) <= 1, ts


@pytest.mark.parametrize("fid", ["q158_ma60", "q158_roc20", "q158_rsv60", "q158_std20"])
def test_cross_section_null_on_random_walks(fid):
    """null 检验（截面口径，5 个币）：随机游走上因子不应显著。"""
    ts = []
    for s in range(6):
        p = panel(500, assets=("BTC", "ETH", "SOL", "BNB", "HYPE"), seed=s)
        ret = evaluate.forward_returns(p["close"], 1)
        ts.append(evaluate.summarize_ic(evaluate.cs_ic(zoo.compute(zoo.REGISTRY[fid], p), ret, 5))["t"])
    assert abs(float(np.mean(ts))) < 1.5 and sum(abs(t) > 3 for t in ts) <= 1, ts


def test_cross_section_is_default_with_five_coins_and_respects_late_listing():
    p = panel(400, assets=("BTC", "ETH", "SOL", "BNB", "HYPE"), seed=4)
    for k in p:
        p[k].loc[p[k].index[:150], "HYPE"] = np.nan                    # 模拟 HYPE 晚上市
    res = evaluate.evaluate(p, [zoo.REGISTRY["q158_roc5"]], 1)
    assert res["mode"] == "cross_section"
    cov = res["coverage"]
    assert cov["first_valid"]["HYPE"] == str(p["close"].index[150])[:10]
    assert cov["dates_usable"] <= 400 - 150 and cov["usable_from"] >= cov["first_valid"]["HYPE"]
    assert res["table"][0]["ic_count"] <= cov["dates_usable"]
    ret = evaluate.forward_returns(p["close"], 1)
    rng = np.random.default_rng(3)
    good = ret + rng.normal(0, ret.abs().mean().mean(), ret.shape)    # 植入的截面信号
    assert evaluate.summarize_ic(evaluate.cs_ic(good, ret, 5))["category"] == "alive"


def test_tiny_samples_are_insufficient_not_significant():
    ic = pd.Series([0.4, 0.5, 0.45, 0.42, 0.48, 0.47])                 # 6 个很“好看”的观测
    s = evaluate.summarize_ic(ic, 60)
    assert s["category"] == "insufficient" and s["p_value"] == 1.0


def test_funding_factors_skipped_without_funding():
    p = panel(100)
    p.pop("funding")
    assert not [f for f in zoo.available(p) if f.id.startswith("crypto_funding")]


# ------------------------------------------------------------------ 想法登记簿
def test_idea_lifecycle(tmp_path):
    book = IdeaBook(tmp_path / "ideas.jsonl")
    it = book.add("资金费极端后的反转", "资金费 z 值 > 2 后 3 天内收益为负", "多头拥挤被迫平仓",
                  "样本外 3 日收益不为负或 IC 不显著", "vibe-trading", "goal-123", ["crypto_funding_z12"])
    assert it.status == "proposed" and it.id.startswith("I-")
    with pytest.raises(ValueError):
        book.move(it.id, "validated", "跳步")
    book.move(it.id, "screening", "因子检验")
    book.move(it.id, "backtesting", "写成插件回测")
    with pytest.raises(ValueError):
        book.move(it.id, "validated", "没有统计证据")
    rep = tmp_path / "stats.json"
    rep.write_text(json.dumps({"verdict": "通过"}), "utf-8")
    book.attach(it.id, "stats", str(rep))
    book.move(it.id, "validated", "统计检验通过")
    with pytest.raises(ValueError):
        book.move(it.id, "approved", "agent 自批")
    assert book.move(it.id, "approved", "人工审阅", by="human", approver="Pocky").status == "approved"
    assert book.n_trials() == 1
    with pytest.raises(ValueError):
        book.add("x", "", "m", "f", "agent")


# ------------------------------------------------------------------ MCP 白名单代理
FAKE_MCP = textwrap.dedent("""
    import json, os, sys
    for line in sys.stdin:
        m = json.loads(line)
        if m.get("method") == "tools/list":
            tools = [{"name": n} for n in ("factor_analysis", "trading_place_order", "backtest", "write_file", "paper_trade_step")]
            print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": {"tools": tools}}), flush=True)
        elif m.get("method") == "tools/call":
            env = sorted(k for k in os.environ if "KEY" in k or "SECRET" in k)
            print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": {"called": m["params"]["name"], "env": env}}), flush=True)
""")


def test_mcp_guard_filters_and_blocks(tmp_path):
    fake = tmp_path / "fake_mcp.py"
    fake.write_text(FAKE_MCP, "utf-8")
    env = {**os.environ, "FREQTRADE__EXCHANGE__KEY": "x", "FREQTRADE__EXCHANGE__SECRET": "y", "OPENAI_API_KEY": "z",
           "PYTHONPATH": str(ROOT)}
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "trading_place_order", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "factor_analysis", "arguments": {}}}]
    p = subprocess.run([sys.executable, "-m", "fwresearch", "mcp-guard", "--audit", str(tmp_path / "audit.log"), "--",
                        sys.executable, str(fake)], input="\n".join(json.dumps(m) for m in msgs) + "\n",
                       capture_output=True, text=True, env=env, cwd=str(ROOT), timeout=60)
    out = {m["id"]: m for m in (json.loads(x) for x in p.stdout.splitlines() if x.strip())}
    assert [t["name"] for t in out[1]["result"]["tools"]] == ["factor_analysis", "backtest"]
    assert "error" in out[2] and "trading_place_order" in out[2]["error"]["message"]
    assert out[3]["result"]["called"] == "factor_analysis"
    assert out[3]["result"]["env"] == ["OPENAI_API_KEY"]                  # 交易所密钥没有传给子进程
    assert "blocked" in (tmp_path / "audit.log").read_text("utf-8")


def test_guard_never_allows_trading_even_if_listed(tmp_path):
    from fwresearch.mcp_guard import load_allow

    f = tmp_path / "allow.txt"
    f.write_text("factor_analysis\ntrading_place_order\nwrite_file\n", "utf-8")
    assert load_allow(str(f)) == frozenset({"factor_analysis"})


# ------------------------------------------------------------------ 数据与命令行
def _write_hl_csv(d: Path, p: dict) -> None:
    d.mkdir(parents=True, exist_ok=True)
    for a in p["close"].columns:
        df = pd.DataFrame({k: p[k][a] for k in ("open", "high", "low", "close", "volume")})
        df.index.name = "time"
        df.to_csv(d / f"{a}_1Dutc.csv")


def test_freqtrade_loader_aligns_funding(tmp_path):
    from fwresearch.data import load_panel

    idx = pd.date_range("2024-01-01", periods=10, freq="D", tz="UTC")
    pd.DataFrame({"date": idx, "open": 1.0, "high": 1.1, "low": 0.9, "close": 1.0, "volume": 5.0}).to_feather(
        tmp_path / "BTC_USDT_USDT-1d-futures.feather")
    fi = pd.date_range("2024-01-01", periods=30, freq="8h", tz="UTC")
    pd.DataFrame({"date": fi, "open": 0.0001, "high": 0, "low": 0, "close": 0, "volume": 0}).to_feather(
        tmp_path / "BTC_USDT_USDT-8h-funding_rate.feather")
    p = load_panel("freqtrade", tmp_path, ["BTC"], "1d")
    assert np.allclose(p["funding"]["BTC"].dropna(), 0.0003)              # 每天 3 次结算之和


def test_missing_coin_data_gives_clear_error(tmp_path):
    from fwresearch.data import load_panel

    _write_hl_csv(tmp_path, panel(50))
    with pytest.raises(ValueError, match="HYPE"):
        load_panel("hl", tmp_path, ["BTC", "HYPE"])


def test_cli_end_to_end(tmp_path):
    p = panel(300, assets=("BTC", "ETH", "SOL", "BNB", "HYPE"))
    _write_hl_csv(tmp_path / "data", p)
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    run = lambda *a: subprocess.run([sys.executable, "-m", "fwresearch", *a], capture_output=True, text=True,  # noqa: E731
                                    cwd=str(tmp_path), env=env, timeout=300)
    r = run("factors", "--source", "hl", "--data-dir", "data", "--ids", "q158_roc5,q158_ma20,a101_101")
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert Path(tmp_path, out["markdown"]).exists() and out["mode"] == "cross_section"   # 默认 5 个币 → 跨币种比较

    res = tmp_path / "research" / "results" / "trades"
    res.mkdir(parents=True)
    for v, mu in (("base", 0.3), ("stop_floor", 0.2), ("cost2x", 0.1)):
        for coin in ("BTC", "SOL"):
            df = trades(mu, 120, seed=hash((v, coin)) % 1000)
            df["job"], df["variant"], df["coin"] = f"{coin}-2023-{v}", v, coin
            df.to_csv(res / f"{coin}-2023-{v}.csv", index=False)
    r = run("matrix")
    assert r.returncode == 0, r.stderr
    m = json.loads(r.stdout)
    assert m["n_trials"] == 2 and m["verdict"] in ("通过", "不通过")
    r = run("stats", "--trades", str(res / "BTC-2023-base.csv"))
    assert r.returncode == 0 and json.loads(r.stdout)["n_trades"] == 120
    r = run("idea", "add", "--title", "测试想法", "--hypothesis", "某因子预测收益", "--mechanism", "资金拥挤",
            "--falsification", "样本外不显著")
    assert r.returncode == 0 and json.loads(r.stdout)["status"] == "proposed"


# ------------------------------------------------------------------ Vibe-Trading 桥接
def test_npz_frame_roundtrip_without_pickle(tmp_path):
    from fwresearch.factors.vt_bridge import read_frames, with_derived, write_frames

    p = panel(30, assets=("BTC", "HYPE"))
    write_frames(tmp_path / "x.npz", {"close": p["close"]})
    back = read_frames(tmp_path / "x.npz")["close"]
    assert back.equals(p["close"].astype("float64")) or np.allclose(back.to_numpy(), p["close"].to_numpy())
    assert list(back.columns) == ["BTC", "HYPE"] and str(back.index.tz) == "UTC"
    d = with_derived(p)
    assert {"vwap", "amount"} <= set(d) and "funding" not in d


@pytest.mark.skipif(not os.environ.get("VT_PYTHON"), reason="需要已安装的 Vibe-Trading（设置 VT_PYTHON=<vt-venv>/bin/python）")
def test_vibe_trading_integration_vt_check():
    """真实安装上的端到端自检：版本、因子库桥接、MCP 握手、白名单、拒绝交易工具、密钥过滤。"""
    from fwresearch.vtcheck import run

    res = run(str(Path(os.environ["VT_PYTHON"]).parent))
    assert res["ok"], json.dumps(res["checks"], ensure_ascii=False)
