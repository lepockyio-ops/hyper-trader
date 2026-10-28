"""研究矩阵：把回测拆成“标的 × 年份 × 实现 × 方案”的独立任务，便于 s03-research 派生子 agent 并行；
另有复核命令，供“复核子 agent”用全新上下文重跑并比对。所有数字都来自程序输出，AI 只负责分派和解读。

  python scripts/research_matrix.py fetch --coins BTC,ETH,SOL,BNB,HYPE --since 2021-01-01     # 一次性缓存 OKX 历史
  python scripts/research_matrix.py plan  --years 2022-2025 --impl A --variants base,stop_floor,cost2x
  python scripts/research_matrix.py run   --job SOL-2023-A-base                         # 子 agent 各跑一格
  python scripts/research_matrix.py aggregate                                           # 汇总 → research/matrix_report.md
  python scripts/research_matrix.py verify --job SOL-2023-A-base                        # 复核：重跑并比对哈希
  python scripts/run.py lab matrix                                                      # 统计检验：DSR、PBO、Bootstrap（docs/RESEARCH_LAB.md）

每个任务的逐笔交易另存 research/results/trades/<任务>.csv（列 job, coin, year, impl, variant, entry_time, time, R, pnl），
供 fwresearch 做统计检验。同一个结果目录里只放一种实现（impl），否则不同实现会被当成同一个方案合并。

方案（variants）：
  base        当前参数
  stop_floor  最小止损改为“设在入场价下方 0.8%”而不是放弃（v1.0 的两种处理之一）
  cost2x      手续费 × 2、滑点 × 3（G5 成本压力）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd  # noqa: E402

from s03.config import load_settings  # noqa: E402

ROOT = Path("research")
DATA, RES = ROOT / "data", ROOT / "results"
PLATFORM_MAX = {"BTC": 40, "ETH": 25, "SOL": 20, "BNB": 10, "HYPE": 10}   # 2026-10-28 快照（Hyperliquid meta）
VARIANTS = ("base", "stop_floor", "cost2x")


def job_id(coin: str, year: int, impl: str, variant: str) -> str:
    return f"{coin}-{year}-{impl}-{variant}"


def parse_job(jid: str) -> tuple[str, int, str, str]:
    coin, year, impl, variant = jid.split("-", 3)
    if variant not in VARIANTS or impl not in ("A", "B"):
        raise ValueError(f"无效任务：{jid}")
    return coin, int(year), impl, variant


# ------------------------------------------------------------------ 数据缓存
def fetch_history(coin: str, bar: str, since: pd.Timestamp, max_pages: int = 5000) -> pd.DataFrame:
    import requests

    from s03.data.okx_ref import BASE, HEADERS

    rows, after = [], None
    for _ in range(max_pages):
        params = {"instId": f"{coin}-USDT-SWAP", "bar": bar, "limit": 100}
        if after:
            params["after"] = after
        r = requests.get(f"{BASE}/history-candles", params=params, headers=HEADERS, timeout=15)
        r.raise_for_status()
        data = r.json()["data"]
        if not data:
            break
        rows += data
        after = data[-1][0]
        if pd.Timestamp(int(after), unit="ms", tz="UTC") < since:
            break
        time.sleep(0.12)                                   # OKX 限速：约每 2 秒 20 次
    df = pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "vol", "volCcy", "volQuote", "confirm"])
    df["time"] = pd.to_datetime(df["time"].astype("int64"), unit="ms", utc=True)
    for c in ("open", "high", "low", "close"):
        df[c] = df[c].astype(float)
    df["volume"] = df["volCcy"].astype(float)
    df = df[df["confirm"] == "1"].set_index("time")[["open", "high", "low", "close", "volume"]].sort_index()
    return df[~df.index.duplicated()]


def load(coin: str, bar: str) -> pd.DataFrame:
    df = pd.read_csv(DATA / f"{coin}_{bar}.csv", index_col=0, parse_dates=True)
    df.index = pd.to_datetime(df.index, utc=True)
    return df


def file_hash(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()[:16]


# ------------------------------------------------------------------ 单格任务
def run_job(jid: str) -> dict:
    from s03.research.replay import run_replay

    coin, year, impl, variant = parse_job(jid)
    s = load_settings()
    if variant == "stop_floor":
        s = replace(s, strategy=replace(s.strategy, stop_min_mode="floor"))
    if variant == "cost2x":
        s = replace(s, cost=replace(s.cost, taker_fee=s.cost.taker_fee * 2, maker_fee=s.cost.maker_fee * 2,
                                    slippage=s.cost.slippage * 3))
    s = replace(s, strategy=replace(s.strategy, event_file=""))   # 回测不读实时事件文件
    daily, bars15 = load(coin, "1Dutc"), load(coin, "15m")
    lo, hi = pd.Timestamp(f"{year}-01-01", tz="UTC"), pd.Timestamp(f"{year + 1}-01-01", tz="UTC")
    daily_y = daily[daily.index < hi]                              # 日线保留之前的全部历史作为预热
    bars_y = bars15[(bars15.index >= lo) & (bars15.index < hi)]
    if len(bars_y) < 1000:
        return {"job": jid, "status": "no_data", "bars": int(len(bars_y))}
    warm = int((daily_y.index < lo).sum())
    trades, summary = run_replay(coin, daily_y, bars_y, s, PLATFORM_MAX[coin], impl=impl,
                                 warmup_days=max(30, min(warm, 400)))
    csv = trades.to_csv(index=False).encode()
    return {"_trades": trades, "job": jid, "status": "ok", "coin": coin, "year": year, "impl": impl, "variant": variant,
            "bars": int(len(bars_y)), "data_hash": {b: file_hash(DATA / f"{coin}_{b}.csv") for b in ("1Dutc", "15m")},
            "trades_hash": hashlib.sha256(csv).hexdigest()[:16], "n_trades": int(len(trades)),
            "summary": {k: (float(v) if isinstance(v, (int, float)) else v) for k, v in summary.items()}}


def save_trades(jid: str, trades: pd.DataFrame) -> Path:
    """逐笔交易的标准格式（fwresearch 读取）：time = 平仓时间，R = 盈亏 ÷ 初始风险。"""
    coin, year, impl, variant = parse_job(jid)
    d = RES / "trades"
    d.mkdir(parents=True, exist_ok=True)
    cols = ["job", "coin", "year", "impl", "variant", "entry_time", "time", "R", "pnl"]
    if trades is None or trades.empty:
        out = pd.DataFrame(columns=cols)
    else:
        out = trades.rename(columns={"exit_time": "time"})[["entry_time", "time", "R", "pnl"]].copy()
        out.insert(0, "variant", variant)
        out.insert(0, "impl", impl)
        out.insert(0, "year", year)
        out.insert(0, "coin", coin)
        out.insert(0, "job", jid)
    p = d / f"{jid}.csv"
    out[cols].to_csv(p, index=False)
    return p


def cmd_run(a) -> int:
    RES.mkdir(parents=True, exist_ok=True)
    out = run_job(a.job)
    save_trades(a.job, out.pop("_trades", None))
    (RES / f"{a.job}.json").write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str), "utf-8")
    print(json.dumps({k: out.get(k) for k in ("job", "status", "n_trades", "trades_hash")}, ensure_ascii=False))
    return 0


def cmd_verify(a) -> int:
    stored = json.loads((RES / f"{a.job}.json").read_text("utf-8"))
    fresh = run_job(a.job)
    fresh.pop("_trades", None)
    same = (stored.get("trades_hash") == fresh.get("trades_hash") and stored.get("data_hash") == fresh.get("data_hash"))
    print(json.dumps({"job": a.job, "match": same, "stored": stored.get("trades_hash"),
                      "fresh": fresh.get("trades_hash")}, ensure_ascii=False))
    return 0 if same else 1


def cmd_plan(a) -> int:
    y0, y1 = (int(x) for x in a.years.split("-"))
    jobs = [job_id(c, y, i, v) for c in a.coins.split(",") for y in range(y0, y1 + 1)
            for i in a.impl.split(",") for v in a.variants.split(",")]
    for j in jobs:
        parse_job(j)
    ROOT.mkdir(exist_ok=True)
    (ROOT / "jobs.json").write_text(json.dumps(jobs, indent=1), "utf-8")
    print(f"{len(jobs)} 个任务 → research/jobs.json")
    return 0


def cmd_fetch(a) -> int:
    DATA.mkdir(parents=True, exist_ok=True)
    since = pd.Timestamp(a.since, tz="UTC")
    for c in a.coins.split(","):
        for bar, start in (("1Dutc", since - pd.Timedelta(days=450)), ("15m", since)):
            df = fetch_history(c, bar, start)
            df.to_csv(DATA / f"{c}_{bar}.csv")
            print(c, bar, len(df), df.index.min(), df.index.max())
    return 0


def cmd_aggregate(a) -> int:
    rows = [json.loads(p.read_text("utf-8")) for p in sorted(RES.glob("*.json"))]
    ok = [r for r in rows if r.get("status") == "ok"]
    if not ok:
        print("没有结果")
        return 1
    df = pd.DataFrame([{**{k: r[k] for k in ("job", "coin", "year", "impl", "variant", "n_trades", "trades_hash")},
                        **{f"s_{k}": v for k, v in r["summary"].items() if isinstance(v, (int, float))}} for r in ok])
    per_year = df[df["variant"] == "base"].pivot_table(index="year", columns="coin", values="n_trades", aggfunc="sum")
    lines = ["# 研究矩阵汇总（程序生成）", "", f"生成时间：{pd.Timestamp.now(tz='UTC'):%Y-%m-%d %H:%M} UTC；任务 {len(rows)} 个，"
             f"成功 {len(ok)} 个。", "", "## 每年交易笔数（base 方案）", "", per_year.fillna(0).astype(int).to_markdown(), "",
             f"合计 {int(per_year.fillna(0).values.sum())} 笔；G1 要求 ≥ 100 笔，< 30 笔为“证据不足”。", "",
             "## 全部任务", "", df.round(4).to_markdown(index=False), ""]
    (ROOT / "matrix_report.md").write_text("\n".join(lines), "utf-8")
    print("\n".join(lines[:12]))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch")
    f.add_argument("--coins", default="BTC,ETH,SOL,BNB,HYPE")
    f.add_argument("--since", default="2021-01-01")
    p = sub.add_parser("plan")
    p.add_argument("--coins", default="BTC,ETH,SOL,BNB,HYPE")
    p.add_argument("--years", default="2022-2025")
    p.add_argument("--impl", default="A")
    p.add_argument("--variants", default="base,stop_floor,cost2x")
    for n in ("run", "verify"):
        x = sub.add_parser(n)
        x.add_argument("--job", required=True)
    sub.add_parser("aggregate")
    a = ap.parse_args()
    return {"fetch": cmd_fetch, "plan": cmd_plan, "run": cmd_run, "verify": cmd_verify,
            "aggregate": cmd_aggregate}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
