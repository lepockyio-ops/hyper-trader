"""交易层组合模拟（两个模板共用；hyper-trader 的组合回测用它）。

输入：各币**连续时间**单独回放得到的逐笔交易（entry_time、time、R、coin）。
做法：按入场时间依次处理，同时持仓数 ≤ max_positions、在险风险（笔数 × risk_per_trade）≤ max_open_risk，
超出的交易记为“被组合约束挡掉”；保留的交易按入场时的权益复利计算盈亏：pnl = R × risk_per_trade × 入场时权益。

这是近似（模型推断，结论里要写明）：
- 单币回放的信号不变，只按组合约束取舍；被挡掉的交易之后，那个币本来可能出现的其他信号不会重算；
- 不检查相关性（需要逐日收益，这里没有）；权益按已平仓交易计算，不含持仓浮亏；
- freq-trader 不需要它：freqtrade 自带多交易对、共享余额的组合回测（research/matrix.py portfolio）。
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd


def overlay(trades: pd.DataFrame, risk_per_trade: float = 0.005, max_open_risk: float = 0.015,
            max_positions: int = 3, equity0: float = 10_000.0) -> dict[str, Any]:
    df = trades.dropna(subset=["R"]).copy()
    if df.empty:
        return {"n_signals": 0, "n_taken": 0, "n_rejected": 0, "rejected_by": {}, "final_equity": equity0,
                "return_pct": 0.0, "max_drawdown_closed": 0.0, "position_cap": max_positions, "trades": df}
    df["entry_time"] = pd.to_datetime(df["entry_time"], utc=True)
    df["time"] = pd.to_datetime(df["time"], utc=True)
    df = df.sort_values(["entry_time", "coin"]).reset_index(drop=True)
    max_by_risk = int(np.floor(max_open_risk / risk_per_trade + 1e-9))
    cap = max(0, min(max_positions, max_by_risk))
    open_: list[tuple[pd.Timestamp, str]] = []           # (平仓时间, 币)
    equity, taken, reasons = equity0, [], []
    pending_pnl: list[tuple[pd.Timestamp, float]] = []
    for _, row in df.iterrows():
        t0 = row["entry_time"]
        for tc, p in sorted([x for x in pending_pnl if x[0] <= t0]):      # 入场前已平仓的盈亏计入权益
            equity += p
        pending_pnl = [x for x in pending_pnl if x[0] > t0]
        open_ = [x for x in open_ if x[0] > t0]
        if any(c == row["coin"] for _, c in open_):
            reasons.append("same_coin_open")
            continue
        if len(open_) >= cap:
            reasons.append("max_positions" if len(open_) >= max_positions else "max_open_risk")
            continue
        pnl = float(row["R"]) * risk_per_trade * equity
        open_.append((row["time"], row["coin"]))
        pending_pnl.append((row["time"], pnl))
        taken.append({**row.to_dict(), "pnl_portfolio": pnl, "equity_at_entry": equity})
    out = pd.DataFrame(taken)
    eq, peak, mdd = equity0, equity0, 0.0
    if len(out):
        for p in out.sort_values("time")["pnl_portfolio"]:
            eq += p
            peak = max(peak, eq)
            mdd = max(mdd, (peak - eq) / peak)
    return {"n_signals": int(len(df)), "n_taken": int(len(out)), "n_rejected": int(len(df) - len(out)),
            "rejected_by": pd.Series(reasons).value_counts().to_dict() if reasons else {},
            "final_equity": round(eq, 2), "return_pct": round((eq / equity0 - 1) * 100, 3),
            "max_drawdown_closed": round(mdd, 4), "position_cap": cap, "trades": out}
