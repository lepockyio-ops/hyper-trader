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

方案（variants，每个只改变一个因素）：
  base        当前参数（固定 stop_min_mode = skip，不继承 settings.toml 里的改动）
  stop_floor  最小止损改为“设在入场价下方 0.8%”而不是放弃（v1.0 的两种处理之一）
  cost2x      手续费 × 2、滑点 × 3（G5 成本压力；回放里止损单也按这个滑点成交）
  funding2x   资金费估计 × 2

两种回测的用途不同（v2.6）：
- 研究矩阵（plan / run）：单币 × 单年，只作诊断（哪个币、哪一年贡献了结果）。测不到组合约束，年末强制平仓，
  合并后的逐笔记录不是同一个账户能走出的路径。
- 组合回测（portfolio）：每个币连续时间回放，再按组合约束（同时持仓数、总风险）取舍、共享权益复利，
  见 fwresearch/portfolio.py。这是交易层近似：被挡掉的交易之后的信号不重算、不检查相关性。上线证据以它为准。
- 回放不应用事件窗口与新闻守卫（结论里要写明）。

可复核性（v2.6）：plan 写入任务清单与“版本指纹”（s03 / fwresearch / 本脚本 / settings / 数据文件的哈希）；
run 先删除这个任务的旧结果，再记录当时的指纹；aggregate 与 lab matrix 只用清单里、成功、指纹一致的任务。
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
VARIANTS = ("base", "stop_floor", "cost2x", "funding2x")


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
    s = settings_for(variant)
    daily, bars15 = load(coin, "1Dutc"), load(coin, "15m")
    lo, hi = pd.Timestamp(f"{year}-01-01", tz="UTC"), pd.Timestamp(f"{year + 1}-01-01", tz="UTC")
    daily_y = daily[daily.index < hi]                              # 日线保留之前的全部历史作为预热
    bars_y = bars15[(bars15.index >= lo) & (bars15.index < hi)]
    if len(bars_y) < 1000:
        return {"job": jid, "status": "no_data", "bars": int(len(bars_y)), "fingerprint": fingerprint()["id"]}
    warm = int((daily_y.index < lo).sum())
    trades, summary = run_replay(coin, daily_y, bars_y, s, PLATFORM_MAX[coin], impl=impl,
                                 warmup_days=max(30, min(warm, 400)))
    csv = trades.to_csv(index=False).encode()
    return {"_trades": trades, "job": jid, "status": "ok", "coin": coin, "year": year, "impl": impl, "variant": variant,
            "bars": int(len(bars_y)), "data_hash": {b: file_hash(DATA / f"{coin}_{b}.csv") for b in ("1Dutc", "15m")},
            "fingerprint": fingerprint()["id"],
            "trades_hash": hashlib.sha256(csv).hexdigest()[:16], "n_trades": int(len(trades)),
            "summary": {k: (float(v) if isinstance(v, (int, float)) else v) for k, v in summary.items()}}


def settings_for(variant: str):
    """每个方案只改变一个因素；base 固定 stop_min_mode = skip。回放不读实时事件文件。"""
    s = load_settings()
    s = replace(s, strategy=replace(s.strategy, stop_min_mode="skip", event_file=""))
    if variant == "stop_floor":
        s = replace(s, strategy=replace(s.strategy, stop_min_mode="floor"))
    if variant == "cost2x":
        s = replace(s, cost=replace(s.cost, taker_fee=s.cost.taker_fee * 2, maker_fee=s.cost.maker_fee * 2,
                                    slippage=s.cost.slippage * 3))
    if variant == "funding2x":
        s = replace(s, cost=replace(s.cost, funding_per_hour_est=s.cost.funding_per_hour_est * 2))
    return s


def fingerprint() -> dict:
    """结果属于哪个版本：代码（s03、fwresearch、本脚本）、settings、数据文件。任何一项变了，旧结果就不再被汇总。"""
    root = Path(__file__).resolve().parents[1]
    h = hashlib.sha256()
    files = sorted(list((root / "s03").rglob("*.py")) + list((root / "fwresearch").rglob("*.py")) + [Path(__file__).resolve()])
    for f in files:
        h.update(str(f.relative_to(root)).encode())
        h.update(f.read_bytes())
    for f in (root / "config" / "settings.toml", root / "config" / "settings.example.toml"):
        h.update(f.read_bytes() if f.exists() else b"<missing>")
    d = hashlib.sha256()
    for f in sorted(DATA.glob("*.csv")) if DATA.exists() else []:
        st = f.stat()
        d.update(f"{f.name}:{st.st_size}:{int(st.st_mtime)}".encode())
    parts = {"code": h.hexdigest()[:16], "data": d.hexdigest()[:16]}
    parts["id"] = hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()[:16]
    return parts


def load_manifest() -> dict:
    m = json.loads((ROOT / "jobs.json").read_text("utf-8"))
    return m if isinstance(m, dict) else {"jobs": m, "fingerprint": None}


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
    for old in (RES / f"{a.job}.json", RES / "trades" / f"{a.job}.csv"):   # 先删旧结果：失败时不会留下旧数据
        old.unlink(missing_ok=True)
    out = run_job(a.job)
    save_trades(a.job, out.pop("_trades", None))
    (RES / f"{a.job}.json").write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str), "utf-8")
    print(json.dumps({k: out.get(k) for k in ("job", "status", "n_trades", "trades_hash")}, ensure_ascii=False))
    return 0


def cmd_verify(a) -> int:
    stored = json.loads((RES / f"{a.job}.json").read_text("utf-8"))
    fresh = run_job(a.job)
    fresh.pop("_trades", None)
    same = (stored.get("trades_hash") == fresh.get("trades_hash") and stored.get("data_hash") == fresh.get("data_hash")
            and stored.get("fingerprint") == fresh.get("fingerprint"))
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
    fp = fingerprint()
    (ROOT / "jobs.json").write_text(json.dumps({"jobs": jobs, "fingerprint": fp["id"], "fingerprint_parts": fp,
                                                "created": pd.Timestamp.now(tz="UTC").isoformat()}, indent=1), "utf-8")
    print(f"{len(jobs)} 个任务 → research/jobs.json（版本指纹 {fp['id']}）")
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
    m = load_manifest()
    rows, skipped = [], []
    for j in m["jobs"]:
        try:
            r = json.loads((RES / f"{j}.json").read_text("utf-8"))
        except (OSError, ValueError):
            skipped.append((j, "没有结果"))
            continue
        if r.get("status") != "ok":
            skipped.append((j, f"状态 {r.get('status')}"))
        elif m.get("fingerprint") and r.get("fingerprint") != m["fingerprint"]:
            skipped.append((j, "版本指纹不一致"))
        else:
            rows.append(r)
    if not rows:
        print("没有可汇总的结果（清单里的任务都没有成功、或版本指纹不一致）")
        return 1
    df = pd.DataFrame([{**{k: r[k] for k in ("job", "coin", "year", "impl", "variant", "n_trades", "trades_hash")},
                        **{f"s_{k}": v for k, v in r["summary"].items() if isinstance(v, (int, float))}} for r in rows])
    per_year = df[df["variant"] == "base"].pivot_table(index="year", columns="coin", values="n_trades", aggfunc="sum")
    lines = ["# 研究矩阵汇总（程序生成）", "", f"生成时间：{pd.Timestamp.now(tz='UTC'):%Y-%m-%d %H:%M} UTC；清单 {len(m['jobs'])} 个任务，"
             f"纳入 {len(rows)} 个，排除 {len(skipped)} 个；版本指纹 {m.get('fingerprint')}。", "",
             "## 每年交易笔数（base 方案）", "", per_year.fillna(0).astype(int).to_markdown(), "",
             f"合计 {int(per_year.fillna(0).values.sum())} 笔；G1 要求 ≥ 100 笔（30–99 笔为“证据不足”）。"
             "矩阵是单币 × 单年的诊断，上线证据看 portfolio。", "",
             "## 全部任务", "", df.round(4).to_markdown(index=False), ""]
    if skipped:
        lines += ["## 排除的任务", ""] + [f"- {j}：{why}" for j, why in skipped] + [""]
    (ROOT / "matrix_report.md").write_text("\n".join(lines), "utf-8")
    print("\n".join(lines[:12]))
    return 0


def cmd_portfolio(a) -> int:
    """组合回测：每个币连续时间回放，再按组合约束取舍（fwresearch.portfolio.overlay）。"""
    from fwresearch.portfolio import overlay
    from s03.research.replay import run_replay

    s = settings_for(a.variant)
    since = pd.Timestamp(a.since, tz="UTC")
    frames, per_coin = [], {}
    for coin in a.coins.split(","):
        daily, bars15 = load(coin, "1Dutc"), load(coin, "15m")
        bars = bars15[bars15.index >= since]
        if len(bars) < 1000:
            per_coin[coin] = "no_data"
            continue
        warm = int((daily.index < since).sum())
        trades, _ = run_replay(coin, daily, bars, s, PLATFORM_MAX[coin], impl=a.impl, warmup_days=max(30, min(warm, 400)))
        per_coin[coin] = int(len(trades))
        if len(trades):
            frames.append(trades.rename(columns={"exit_time": "time"}).assign(coin=coin)[["coin", "entry_time", "time", "R", "pnl"]])
    allt = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=["coin", "entry_time", "time", "R", "pnl"])
    r = s.risk
    res = overlay(allt, r.risk_per_trade, r.max_open_risk, r.max_positions)
    out_dir = RES / "portfolio"
    out_dir.mkdir(parents=True, exist_ok=True)
    taken = res.pop("trades")
    if len(taken):
        taken.assign(job=f"portfolio-{a.variant}", variant=a.variant, year=0, impl=a.impl).rename(
            columns={"pnl_portfolio": "pnl_account"})[["job", "coin", "year", "impl", "variant", "entry_time", "time", "R",
                                                        "pnl_account"]].to_csv(out_dir / f"{a.variant}.csv", index=False)
    res.update(variant=a.variant, since=a.since, per_coin_signals=per_coin, fingerprint=fingerprint(),
               note="交易层近似：被挡掉的交易之后的信号不重算、不检查相关性；回放不应用事件窗口与新闻守卫")
    (out_dir / f"{a.variant}.json").write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), "utf-8")
    print(json.dumps({k: res[k] for k in ("variant", "n_signals", "n_taken", "n_rejected", "return_pct",
                                          "max_drawdown_closed")}, ensure_ascii=False))
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
    p.add_argument("--variants", default="base,stop_floor,cost2x,funding2x")
    for n in ("run", "verify"):
        x = sub.add_parser(n)
        x.add_argument("--job", required=True)
    sub.add_parser("aggregate")
    pf = sub.add_parser("portfolio")
    pf.add_argument("--coins", default="BTC,ETH,SOL,BNB,HYPE")
    pf.add_argument("--since", default="2022-01-01")
    pf.add_argument("--impl", default="A")
    pf.add_argument("--variant", default="base", choices=VARIANTS)
    a = ap.parse_args()
    return {"fetch": cmd_fetch, "plan": cmd_plan, "run": cmd_run, "verify": cmd_verify,
            "aggregate": cmd_aggregate, "portfolio": cmd_portfolio}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
