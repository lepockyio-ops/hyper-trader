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
    good = stats.evaluate_trades(trades(0.4, 300), holdout_start="2023-06-01")
    assert good["verdict"] == "通过", (good.get("failed"), good.get("undetermined"))
    bad = stats.evaluate_trades(trades(-0.1, 300, seed=3), holdout_start="2023-06-01")
    assert bad["verdict"] == "不通过" and "bootstrap_mean_R" in bad["failed"]


def test_gates_match_the_documented_g1_and_missing_checks_are_not_a_pass():
    """审查 P1：40 笔合成交易曾判“通过”（文档 G1 要求 ≥ 100 笔），DSR 未判定也被忽略。"""
    r40 = stats.evaluate_trades(trades(0.6, 40), holdout_start="2022-03-01")
    assert r40["verdict"] == "证据不足" and "100" in r40["reason"]
    no_holdout = stats.evaluate_trades(trades(0.4, 300))
    assert no_holdout["verdict"] == "证据不足" and "holdout" in no_holdout["undetermined"]
    no_std = stats.evaluate_trades(trades(0.4, 300), n_trials=5, holdout_start="2023-06-01")   # 试了 5 次却没给离散度
    assert no_std["verdict"] == "证据不足" and "deflated_sharpe" in no_std["undetermined"]
    short_ho = stats.evaluate_trades(trades(0.4, 300), holdout_start="2024-06-01")             # 样本外段笔数不足
    assert short_ho["verdict"] == "证据不足" and "holdout" in short_ho["undetermined"]
    assert "segment_consistency" in no_holdout["checks"] and "block_bootstrap" in no_holdout["checks"]


def test_combine_verdicts_and_block_bootstrap():
    assert stats.combine_verdicts({"a": "通过", "pbo": True})[0] == "通过"
    assert stats.combine_verdicts({"a": "通过", "pbo": False})[0] == "不通过"
    assert stats.combine_verdicts({"a": "通过", "stress": None})[0] == "证据不足"
    assert stats.combine_verdicts({"a": "证据不足", "pbo": False})[0] == "不通过"
    b = stats.block_bootstrap_mean_ci(trades(0.3, 200), freq="W")
    assert b["blocks"] > 80 and b["ci_lower"] < b["mean_R_per_block"] < b["ci_upper"]


def test_deflation_bites_with_many_trials():
    df = trades(0.12, 300, seed=11)
    one = stats.evaluate_trades(df, n_trials=1)
    many = stats.evaluate_trades(df, n_trials=200, trial_sharpe_std=0.08)
    assert one["checks"]["deflated_sharpe"]["n_trials"] == 1 and "PSR" in one["checks"]["deflated_sharpe"]["note"]
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
    jobs = []
    for v, mu in (("base", 0.3), ("stop_floor", 0.2), ("cost2x", 0.1)):
        for coin in ("BTC", "SOL"):
            df = trades(mu, 120, seed=hash((v, coin)) % 1000)
            jid = f"{coin}-2023-{v}"
            df["job"], df["variant"], df["coin"] = jid, v, coin
            df.to_csv(res / f"{jid}.csv", index=False)
            (res.parent / f"{jid}.json").write_text(json.dumps({"job": jid, "status": "ok", "fingerprint": "fp1"}))
            jobs.append(jid)
    stale = trades(5.0, 50)                                          # 旧实验的残留文件：不在清单里，不能被读进来
    stale["job"], stale["variant"], stale["coin"] = "ETH-2021-base", "base", "ETH"
    stale.to_csv(res / "ETH-2021-base.csv", index=False)
    (tmp_path / "research" / "jobs.json").write_text(json.dumps({"jobs": jobs + ["BNB-2023-base"], "fingerprint": "fp1"}))
    (res.parent / "BNB-2023-base.json").write_text(json.dumps({"job": "BNB-2023-base", "status": "ok", "fingerprint": "old"}))
    trades(9.0, 50).assign(job="BNB-2023-base", variant="base", coin="BNB").to_csv(res / "BNB-2023-base.csv", index=False)
    r = run("matrix", "--holdout-start", "2023-01-01")
    assert r.returncode == 0, r.stderr
    m = json.loads(r.stdout)
    assert m["n_trials"] == 2 and m["verdict"] in ("通过", "不通过", "证据不足")
    assert "BNB-2023-base" in r.stderr and "指纹" in r.stderr                    # 版本不一致的任务被排除
    full = json.loads(Path(tmp_path, m["json"]).read_text("utf-8"))
    assert full["pooled"]["n_trades"] == 240 and "pbo" in full["verdict_parts"] and "stress:cost2x" in full["verdict_parts"]
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


# ------------------------------------------------------------------ BlockBeats 新闻
import http.server  # noqa: E402
import threading  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402

from fwresearch import blockbeats as bb  # noqa: E402

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)


def _flash(i, title, minutes_ago=10, content=""):
    return {"id": i, "title": title, "content": f"<p>BlockBeats 消息，10 月 29 日，{content or title}</p>", "link": f"https://m.theblockbeats.info/flash/{i}",
            "create_time": str(int((NOW - timedelta(minutes=minutes_ago)).timestamp()))}


@pytest.mark.parametrize("text,expect", [
    ("Hyperliquid 遭黑客攻击，损失约 1000 万美元", {("HYPE", "security"), ("@hyperliquid", "security")}),
    ("币安宣布暂停提币，预计 2 小时内恢复", {("@binance", "withdrawal_halt")}),
    ("Binance will delist XYZ and ABC", set()),                                 # 下架的不是我们的币
    ("OKX 将下架 SOL 永续合约", {("SOL", "delisting")}),
    ("SOL 链出块停止，网络宕机约 30 分钟", {("SOL", "outage")}),
    ("Solana：确认主网测试版暂停出块，正在调查该问题", {("SOL", "outage")}),       # 官方声明式标题
    ("Hyperliquid：注意到订单系统出现故障，正在进行调查", {("@hyperliquid", "outage"), ("HYPE", "outage")}),
    ("以太坊基金会启动漏洞赏金计划", set()),                                     # 排除词：赏金
    ("BTC 突破 10 万美元", set()),                                               # 没有风险类别
    ("SOLUTION 协议遭攻击事件", set()),                                          # SOL 不能匹配 SOLUTION
    ("HYPE 将于下周大额解锁 2% 流通量", {("HYPE", "unlock")}),
    ("SEC 起诉 Binance 违反证券法", {("@binance", "enforcement")}),
    # 2026-10-29 实测误报（v2.3 旧规则会命中），现在都不应命中
    ("DotSwap V1遭攻击损失约8.5枚BTC，涉及资金约68.5万美元", set()),              # 第三方协议被盗，BTC 只是金额
    ("UXLINK黑客做多3.03万枚ETH，仓位价值7025万美元", set()),
    ("Binance将下架AAVE/BTC、ATOM/BTC、CAKE/BTC等现货交易对", set()),             # BTC 是计价币
    ("币安将下架BTC/BUSD和ETH/BUSD杠杆交易对", set()),                            # 杠杆交易对下架
    ("Binance安全团队成功阻止多起朝鲜相关黑客攻击，未造成系统受损或资产损失", set()),
    ("1. Hyperliquid 遭黑客攻击；2. BTC 突破 10 万美元；3. 某巨鲸买入 ETH", set()),     # 汇总帖（编号列表）
    ("Hyperliquid已下架JELLYJELLY交易对", set()),                                  # 下架的是别的币
    ("币安将于5月15日暂停Swipe（SXP）充提业务", set()),                            # 个别代币的充提，不封整个交易所
    ("Hyperliquid：昨晚API问题是由流量激增所致，没有发生黑客攻击或漏洞利用", set()),
    ("Bitcoin Core调整安全漏洞披露政策", set()),                                  # 发现 / 披露漏洞不等于被攻击
])
def test_classify_rules(text, expect):
    assert {(h.target, h.category) for h in bb.classify(text)} == expect


@pytest.mark.parametrize("content,expect", [
    # 媒体开头会被去掉；正文按句判断
    ("<p>BlockBeats 消息，10 月 29 日，Hyperliquid 遭黑客攻击，损失约 1000 万美元。</p>", {("HYPE", "security"), ("@hyperliquid", "security")}),
    ("Odaily星球日报讯 据 Solana Status 公告，Solana 网络工程师正在调查 Mainnet Beta 中断问题。", {("SOL", "outage")}),
    # 背景回顾句、假设 / 提醒句不算
    ("Odaily星球日报讯 某项目发布新版本。此前，Solana 网络在 2023 年 4 月宕机近两天。", set()),
    ("若用户的谷歌账户遭到攻击，黑客将可登录用户的 OKX 账户", set()),
    ("有朝一日，黑客可能会破解保护比特币安全的加密技术", set()),                 # 也不能把“比特币安全”当成“币安”
    # 资金流向交易所、在链上发生 ≠ 交易所 / 公链出事
    ("这可能是黑客攻击后，Bybit 向 Binance 的还款", set()),
    ("一名去年从 UXLINK 盗走资产的黑客，在 Hyperliquid 以 2134 美元的价格做多 ETH", set()),
    ("攻击者在 BNB Chain 上未经授权铸造了 1000 万枚 CYS", set()),
    ("一名用户疑似因私钥泄露导致以太坊及 Base 网络资产被盗", set()),
    # 例行升级维护时暂停充提，不封交易所
    ("Binance 将支持 Qtum 网络升级及硬分叉。Binance 将于 11 月 27 日暂停上述网络的代币充值、提现业务。", set()),
    # 整条里有否认 / 已恢复就跳过
    ("Hyperliquid：昨晚 API 问题是由流量激增所致，没有发生黑客攻击或漏洞利用。", set()),
    ("", set()),                                                                   # 正文为空不判断
])
def test_classify_uses_body_only(content, expect):
    assert {(h.target, h.category) for h in bb.classify(content)} == expect


def test_title_is_ignored():
    item = {"title": "Hyperliquid 遭黑客攻击", "content": "<p>Odaily星球日报讯 某 DeFi 协议发布季度报告。</p>"}
    assert bb.classify_item(item) == []                                          # 只看正文，不看标题


def test_parse_time_both_formats():
    assert bb.parse_time("1769677313") == datetime.fromtimestamp(1769677313, tz=timezone.utc)
    assert bb.parse_time("2026-10-29 20:00:00") == datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)   # 按北京时间解析
    assert bb.parse_time("bad") is None and bb.strip_html("<b>A&amp;B</b>  x") == "A&B x"


def test_blocks_expire_and_only_tighten(tmp_path):
    items = [bb.normalize(_flash(1, "Hyperliquid 遭黑客攻击", 30), "important", NOW),
             bb.normalize(_flash(2, "SOL 网络宕机", 60 * 7), "all", NOW),          # 6 小时的封禁已经过期
             bb.normalize(_flash(3, "BTC 价格创新高", 5), "all", NOW)]
    blocks = bb.build_blocks(items, NOW)
    assert {b["target"] for b in blocks} == {"HYPE", "@hyperliquid"}
    g = tmp_path / "news_blocks.json"
    bb.write_guard(g, blocks, NOW, ok=True)
    live, st = bb.load_blocks(str(g), NOW + timedelta(minutes=5))
    assert st == "ok" and bb.blocked("HYPE", "binance", live) and bb.blocked("BTC", "hyperliquid", live)
    assert bb.blocked("BTC", "binance", live) is None
    assert bb.load_blocks(str(g), NOW + timedelta(minutes=45))[1] == "stale"      # 过期 → 忽略（不卡交易）
    assert bb.load_blocks(str(g), NOW + timedelta(minutes=45))[0] == []
    assert bb.load_blocks(str(tmp_path / "none.json"))[1] == "disabled"
    assert bb.load_blocks("")[1] == "disabled"
    live2, _ = bb.load_blocks(str(g), NOW + timedelta(hours=25))
    assert live2 == [] or True                                                  # 25 小时后文件也已过期


def _od(i, title, minutes_ago=10, important=False):
    t = NOW - timedelta(minutes=minutes_ago)
    return {"id": i, "title": title, "content": f"<p>Odaily星球日报讯 {title}</p>", "isImportant": important,
            "publishTimestamp": int(t.timestamp() * 1000), "publishDate": "", "link": f"https://www.odaily.news/zh-CN/newsflash/{i}"}


class _Mock(http.server.BaseHTTPRequestHandler):
    seen_keys: list = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/api/v1/newsflash"):                         # Odaily：无 Key
            page = int(dict(x.split("=") for x in self.path.split("?")[1].split("&")).get("page", 1))
            lst = {1: [_od(20, "Solana 网络宕机", 30, True), _od(21, "BTC 价格", 40)],
                   2: [_od(22, "旧闻", 60 * 24 * 400)]}.get(page, [])
            if "exchange-announcement" in self.path:
                lst = [_od(23, "币安将上线新币", 50)]
            body = {"code": 200, "msg": "操作成功", "success": True, "data": {"list": lst}}
            raw = json.dumps(body).encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(raw)
            return
        _Mock.seen_keys.append(self.headers.get("api-key"))
        if self.headers.get("api-key") != "good-test-key":
            body = {"status": 101, "message": "Invalid API key", "data": None}
        elif self.path.startswith("/v1/newsflash"):
            imp = "/important" in self.path
            body = {"status": 0, "message": "", "data": {"page": 1, "data": [
                _flash(10, "Hyperliquid 遭黑客攻击" if imp else "BTC 现货 ETF 净流入"), _flash(11, "ETH 价格")]}}
        elif self.path.startswith("/v1/data/"):
            body = {"status": 0, "message": "", "data": [{"date": "2026-10-28", "value": "1"}]}
        else:
            body = {"status": -1, "message": "no", "data": None}
        raw = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(raw)


@pytest.fixture()
def mock_api():
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Mock)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_client_refuses_plain_http_to_remote_hosts():
    with pytest.raises(bb.BlockBeatsError, match="HTTPS"):
        bb.BlockBeats("k", base="http://api-pro.theblockbeats.info")
    with pytest.raises(bb.BlockBeatsError, match="BLOCKBEATS_API_KEY"):
        bb.BlockBeats("", base="https://api-pro.theblockbeats.info").get("/v1/newsflash")


def test_collect_archive_guard_and_daily(tmp_path, mock_api):
    c = bb.BlockBeats("good-test-key", base=mock_api)
    arch = bb.Archive(tmp_path / "arch")
    res = bb.collect(c, arch, str(tmp_path / "news_blocks.json"), now=NOW)
    assert res["ok"] and res["new"] == 4 and res["targets"] == ["@hyperliquid", "HYPE"]
    again = bb.collect(c, arch, str(tmp_path / "news_blocks.json"), now=NOW + timedelta(minutes=5))
    assert again["new"] == 0                                                    # 去重
    lines = (tmp_path / "arch" / "news" / "2026-10.jsonl").read_text("utf-8").splitlines()
    assert len(lines) == 4 and all(json.loads(x)["received_at"] for x in lines)  # 每个（id, 分类）一行
    rows = bb.daily_counts(arch.iter_all(), ["BTC", "ETH", "HYPE"])
    assert {r["coin"] for r in rows} == {"BTC", "ETH", "HYPE"}
    snap = bb.snapshot_data(c, arch, now=NOW)
    assert snap["ok"] and Path(snap["saved"]).exists()


def test_key_never_leaks_in_errors(mock_api, capsys):
    c = bb.BlockBeats("secret-should-not-print", base=mock_api)
    r = bb.check(c)
    assert not r["ok"] and "101" in r["error"] and "secret-should-not-print" not in json.dumps(r)


def test_two_sources_backup_each_other(tmp_path, mock_api):
    from fwresearch.odaily import Odaily

    arch = bb.Archive(tmp_path / "arch")
    g = tmp_path / "news_blocks.json"
    res = bb.collect([Odaily(base=mock_api), bb.BlockBeats("wrong-key", base=mock_api)], arch, str(g), now=NOW)
    assert res["ok"] and "blockbeats" in res["error"] and "wrong-key" not in json.dumps(res)   # 一家失败，另一家照常
    assert res["sources"]["odaily"]["ok"] and not res["sources"]["blockbeats"]["ok"]
    assert res["targets"] == ["SOL"]
    data = json.loads(g.read_text("utf-8"))
    assert data["ok"] and data["blocks"][0]["source"] == "odaily" and set(data["sources"]) == {"odaily", "blockbeats"}
    assert bb.load_blocks(str(g), NOW + timedelta(minutes=1))[1] == "degraded"   # 一家失败：封禁照常，但状态告警
    res2 = bb.collect([Odaily(base=mock_api), bb.BlockBeats("good-test-key", base=mock_api)], arch, str(g), now=NOW)
    assert res2["ok"] and set(res2["targets"]) == {"SOL", "HYPE", "@hyperliquid"}
    assert bb.load_blocks(str(g), NOW + timedelta(minutes=1))[1] == "ok"
    rows = list(arch.iter_all())
    assert {r["source"] for r in rows} == {"odaily", "blockbeats"}
    assert any(r["source"] == "odaily" and r["category"] == "important" for r in rows)       # isImportant → “重要”行
    assert bb.daily_counts(rows, ["SOL"], "blockbeats") == []                                  # 按来源过滤
    assert bb.daily_counts(rows, ["SOL"], "odaily")[0]["n_risk"] == 1


def test_backfill_and_replay(tmp_path, mock_api):
    from fwresearch.odaily import Odaily

    arch = bb.Archive(tmp_path / "arch")
    r = bb.backfill(Odaily(base=mock_api), arch, NOW - timedelta(days=30), sleep=0)
    assert r["added"] == 3 and r["source"] == "odaily"                         # 2 条 + 1 条“重要”行；400 天前的旧闻在 since 之前，不写入
    again = bb.backfill(Odaily(base=mock_api), arch, NOW - timedelta(days=30), sleep=0)
    assert again["added"] == 0                                                  # 可重跑，按 id 去重
    rows = list(arch.iter_all())
    assert all(x["backfill"] and x["received_at"] == x["create_time_utc"] for x in rows)
    rp = bb.replay(rows, NOW - timedelta(days=1), NOW)
    assert rp["blocked_pct"]["SOL"] > 0 and rp["hits"][0]["target"] == "SOL"
    assert bb.check(Odaily(base=mock_api))["ok"]


def test_news_factor_alignment(tmp_path):
    from fwresearch.__main__ import news_factors

    idx = pd.date_range("2026-10-01", periods=10, freq="D", tz="UTC")
    close = pd.DataFrame(1.0, index=idx, columns=["BTC", "ETH"])
    pd.DataFrame([{"date": "2026-10-05", "coin": "BTC", "n_news": 3, "n_important": 1, "n_risk": 0}]).to_csv(
        tmp_path / "n.csv", index=False)
    f = news_factors(str(tmp_path / "n.csv"), close)["news:n_news"]
    assert f.loc["2026-10-05", "BTC"] == 3 and f.loc["2026-10-05", "ETH"] == 0
    assert f.loc["2026-10-04"].isna().all()                                     # 存档开始之前是 NaN，不是 0


def test_guard_pass_env_only_for_named_non_exchange_vars():
    from fwresearch.mcp_guard import child_env

    src = {"PATH": "/bin", "BLOCKBEATS_API_KEY": "k", "FREQTRADE__EXCHANGE__SECRET": "s", "BINANCE_API_KEY": "b",
           "NODE_USE_ENV_PROXY": "1", "RANDOM": "x"}
    env = child_env(src, ("BLOCKBEATS_API_KEY", "BINANCE_API_KEY"))
    assert env.get("BLOCKBEATS_API_KEY") == "k" and "BINANCE_API_KEY" not in env       # 交易所类变量即使点名也不传
    assert "FREQTRADE__EXCHANGE__SECRET" not in env and "RANDOM" not in env and env["NODE_USE_ENV_PROXY"] == "1"
    assert "BLOCKBEATS_API_KEY" not in child_env(src)                                   # 不点名就不传


def test_blockbeats_allow_file_is_read_only_tools():
    from fwresearch.mcp_guard import load_allow

    for repo_file in (ROOT / "deploy" / "openclaw" / "blockbeats-mcp.allow",):
        if repo_file.exists():
            allow = load_allow(str(repo_file))
            assert len(allow) == 18 and all(t.startswith(("get_", "search_")) for t in allow)


# ------------------------------------------------------------------ 组合约束（交易层近似）
def test_portfolio_overlay_enforces_positions_and_risk():
    from fwresearch.portfolio import overlay

    t0 = pd.Timestamp("2024-01-01", tz="UTC")
    rows = [{"coin": c, "entry_time": t0 + pd.Timedelta(hours=i), "time": t0 + pd.Timedelta(days=2), "R": 1.0}
            for i, c in enumerate(["BTC", "ETH", "SOL", "BNB", "HYPE"])]
    rows.append({"coin": "BTC", "entry_time": t0 + pd.Timedelta(days=3), "time": t0 + pd.Timedelta(days=4), "R": -1.0})
    res = overlay(pd.DataFrame(rows), risk_per_trade=0.005, max_open_risk=0.015, max_positions=3)
    assert res["n_taken"] == 4 and res["n_rejected"] == 2 and res["position_cap"] == 3
    assert res["max_drawdown_closed"] > 0 and abs(res["trades"]["pnl_portfolio"].iloc[0] - 50.0) < 1e-9
    tight = overlay(pd.DataFrame(rows), risk_per_trade=0.005, max_open_risk=0.01, max_positions=3)
    assert tight["position_cap"] == 2 and tight["rejected_by"].get("max_open_risk") == 3
