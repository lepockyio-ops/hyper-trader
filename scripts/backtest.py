"""15 分钟逐笔回放（G1–G5）。数据优先用本地录制，其次 OKX 永续历史补充。

  python scripts/backtest.py --coin SOL --impl A --okx-pages 300
  python scripts/backtest.py --coin SOL --impl B --okx-pages 300 --fee-mult 2 --slip-mult 3   # G5 成本压力
注意：OKX 为 USDT 计价，与 HL（USDC）存在价差，需先用 recorder.cross_check_closes 测量并计入滑点。
"""
from __future__ import annotations

import argparse
import os
import sys
from dataclasses import replace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from s03.config import load_settings  # noqa: E402
from s03.data import okx_ref  # noqa: E402
from s03.research.replay import run_replay  # noqa: E402

PLATFORM_MAX = {"BTC": 40, "ETH": 25, "SOL": 20, "BNB": 10}   # 2026-10-25 快照，实盘以 meta 为准


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--coin", default="SOL")
    ap.add_argument("--impl", default="A", choices=["A", "B"])
    ap.add_argument("--okx-pages", type=int, default=300, help="15m 每页 100 根；300 页约 312 天")
    ap.add_argument("--fee-mult", type=float, default=1.0)
    ap.add_argument("--slip-mult", type=float, default=1.0)
    ap.add_argument("--out", default="var/backtest")
    a = ap.parse_args()

    s = load_settings()
    s = replace(s, cost=replace(s.cost, taker_fee=s.cost.taker_fee * a.fee_mult,
                                maker_fee=s.cost.maker_fee * a.fee_mult, slippage=s.cost.slippage * a.slip_mult))
    daily = okx_ref.history(a.coin, "1Dutc", 10)
    bars15 = okx_ref.history(a.coin, "15m", a.okx_pages)
    trades, summary = run_replay(a.coin, daily, bars15, s, PLATFORM_MAX[a.coin], impl=a.impl,
                                 warmup_days=min(200, len(daily) - 30))
    os.makedirs(a.out, exist_ok=True)
    trades.to_csv(f"{a.out}/{a.coin}_{a.impl}_trades.csv", index=False)
    print(f"{a.coin} 实现 {a.impl}  数据 {bars15.index[0]} → {bars15.index[-1]}")
    for k, v in summary.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
