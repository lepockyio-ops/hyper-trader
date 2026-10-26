"""15 分钟逐笔回放回测（G1–G6 的精确口径；复用实盘同一个信号模块）。

用法见 scripts/backtest.py。输入：
  daily: 日线（已收盘），bars15: 15 分钟线（已收盘），同一标的、UTC 索引
  funding: 可选，逐小时资金费率 Series（索引 UTC 小时）；缺省用 cost.funding_per_hour_est
输出：逐笔交易 DataFrame 与汇总指标。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from s03.config import Settings
from s03.research import sim
from s03.risk.sizing import size_position
from s03.signals.s03 import ArmState, daily_context, step_intraday


@dataclass
class OpenTrade:
    entry_time: pd.Timestamp
    entry_px: float
    stop_px: float
    size: float
    remaining: float
    r_usd: float
    tps: list = field(default_factory=list)
    realized: float = 0.0
    fees: float = 0.0
    funding: float = 0.0
    extension_done: bool = False


def run_replay(coin: str, daily: pd.DataFrame, bars15: pd.DataFrame, settings: Settings,
               platform_max_leverage: int, impl: str = "A", equity0: float = 10_000.0,
               funding: pd.Series | None = None, warmup_days: int = 200) -> tuple[pd.DataFrame, dict]:
    s, c, r = settings.strategy, settings.cost, settings.risk
    equity = equity0
    state = ArmState(coin)
    pending: dict | None = None            # 已挂（模拟）入场单
    pos: OpenTrade | None = None
    trades = []
    ctx_cache: dict = {}
    days = daily.index
    start = days[min(warmup_days, len(days) - 1)]
    b15 = bars15[bars15.index >= start]

    def ctx_for(day: pd.Timestamp):
        if day not in ctx_cache:
            hist = daily[daily.index < day]
            ctx_cache[day] = daily_context(coin, hist, s) if len(hist) > max(s.momentum_lookbacks) + 5 else None
        return ctx_cache[day]

    prev_day = None
    for i in range(1, len(b15)):
        t = b15.index[i]
        bar = b15.iloc[i]
        day = t.normalize()
        ctx = ctx_for(day)

        # ---- 持仓管理：资金费、止损/止盈、日线收盘规则
        if pos is not None:
            pos.funding += sim.funding_cost(pos.remaining * float(bar["close"]), 0.25,
                                            float(funding.asof(t)) if funding is not None else c.funding_per_hour_est)
            for ev in sim.bar_exits(bar, pos.stop_px, pos.tps, c.slippage):
                qty = pos.remaining if ev.frac_of_initial < 0 else min(pos.remaining, pos.size * ev.frac_of_initial)
                fee = c.taker_fee if ev.kind == "stop" else c.maker_fee
                pos.realized += qty * (ev.price - pos.entry_px)
                pos.fees += qty * ev.price * fee
                pos.remaining -= qty
                pos.tps = [x for x in pos.tps if x[0] != ev.kind]
            if prev_day is not None and day != prev_day and ctx is not None and pos.remaining > 1e-12:
                held = t - pos.entry_time
                exit_all = held >= pd.Timedelta(days=s.max_hold_days) or ctx.close < ctx.ema_fast
                if exit_all or (not pos.extension_done and ctx.close > ctx.ema_fast + s.extension_atr_mult * ctx.atr):
                    qty = pos.remaining if exit_all else pos.remaining / 2
                    px = float(bar["open"]) * (1 - c.slippage)
                    pos.realized += qty * (px - pos.entry_px)
                    pos.fees += qty * px * c.taker_fee
                    pos.remaining -= qty
                    pos.extension_done = True
            if pos.remaining <= 1e-12:
                pnl = pos.realized - pos.fees - pos.funding
                equity += pnl
                trades.append({"coin": coin, "entry_time": pos.entry_time, "exit_time": t, "entry": pos.entry_px,
                               "stop": pos.stop_px, "size": pos.size, "pnl": pnl, "R": pnl / pos.r_usd,
                               "fees": pos.fees, "funding": pos.funding, "equity": equity})
                pos = None
                state = ArmState(coin)
        prev_day = day

        # ---- 挂单成交检查（用本根 K 线）
        if pos is None and pending is not None:
            if impl == "A":
                fill = sim.entry_fill_a(bar, pending["trigger"], pending["limit"])
            elif pending.get("go_limit"):
                fill = sim.entry_fill_b(bar, pending["go_limit"])
                if fill is None:
                    pending.pop("go_limit")
            else:
                fill = None
                if sim.b_signal(bar, pending["trigger"]):
                    pending["go_limit"] = float(bar["close"]) * (1 + s.entry_limit_offset_pct / 100)
                    continue                       # 下一根开盘下单；本根不再推进状态机
            if fill is not None:
                fill *= (1 + c.slippage)
                plan = size_position(equity, fill, pending["stop"], platform_max_leverage, r, c)
                pos = OpenTrade(t, fill, pending["stop"], plan.size, plan.size, plan.risk_usd)
                pos.fees += plan.size * fill * c.taker_fee
                pos.tps = [("tp1", fill + s.tp1_r * (fill - pending["stop"]), s.tp1_frac),
                           ("tp2", fill + s.tp2_r * (fill - pending["stop"]), s.tp2_frac)]
                state = ArmState(coin, phase="in_position")
                pending = None
                for ev in sim.bar_exits(bar, pos.stop_px, [], c.slippage):   # 同根打到止损（保守）
                    pos.realized += pos.remaining * (ev.price - pos.entry_px)
                    pos.fees += pos.remaining * ev.price * c.taker_fee
                    pos.remaining = 0
                continue

        # ---- 信号状态机（与实盘同一函数）
        if pos is None and ctx is not None:
            hist15 = b15.iloc[max(0, i - 200): i + 1]
            state, intent = step_intraday(state, ctx, hist15, s)
            if intent is not None:
                if intent.kind == "cancel":
                    pending = None
                else:
                    pending = {"trigger": intent.trigger_px, "limit": intent.limit_px, "stop": intent.stop_px}

    tdf = pd.DataFrame(trades)
    return tdf, summarize(tdf, equity0)


def summarize(tdf: pd.DataFrame, equity0: float) -> dict:
    if tdf.empty:
        return {"trades": 0, "note": "证据不足：无交易"}
    eq = pd.concat([pd.Series([equity0]), tdf["equity"]]).reset_index(drop=True)
    dd = (eq / eq.cummax() - 1).min()
    wins, losses = tdf[tdf.pnl > 0], tdf[tdf.pnl <= 0]
    pf = wins.pnl.sum() / abs(losses.pnl.sum()) if len(losses) and losses.pnl.sum() != 0 else np.inf
    streak = max_streak = 0
    for p in tdf.pnl:
        streak = streak + 1 if p <= 0 else 0
        max_streak = max(max_streak, streak)
    out = {
        "trades": len(tdf),
        "net_pnl": round(float(tdf.pnl.sum()), 2),
        "net_return_pct": round(float(eq.iloc[-1] / equity0 - 1) * 100, 2),
        "max_drawdown_pct": round(float(dd) * 100, 2),
        "win_rate_pct": round(len(wins) / len(tdf) * 100, 1),
        "avg_win_R": round(float(wins.R.mean()), 2) if len(wins) else None,
        "avg_loss_R": round(float(losses.R.mean()), 2) if len(losses) else None,
        "expectancy_R": round(float(tdf.R.mean()), 3),
        "profit_factor": round(float(pf), 2),
        "worst_5pct_R": round(float(tdf.R.quantile(0.05)), 2),
        "max_losing_streak": max_streak,
        "fees_total": round(float(tdf.fees.sum()), 2),
        "funding_total": round(float(tdf.funding.sum()), 2),
    }
    if len(tdf) < 30:
        out["note"] = "证据不足：交易少于 30 笔（手册 G1）"
    return out
