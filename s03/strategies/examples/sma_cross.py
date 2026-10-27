"""示例策略（仅用于验证框架能跑通，不代表有正期望，不要直接用于交易）。

日线：收盘价在上升的 SMA(trend_len) 之上才允许入场。
信号：15 分钟收盘价上穿 EMA(fast_len) → 在这根 K 线高点挂突破单；止损 = 最近 stop_lookback 根最低点下方 buffer。
止盈：2R 卖出一半（可调）。离场：日线收盘跌破 SMA(trend_len)。
启用：settings.toml 中 [strategy] plugin = "s03.strategies.examples.sma_cross:SmaCrossExample"
"""
from __future__ import annotations

from dataclasses import replace

import pandas as pd

from s03.signals import indicators as ind
from s03.strategy.api import ArmState, Context, EntryIntent, ExitIntent, Position, StrategyPlugin


class SmaCrossExample(StrategyPlugin):
    name = "sma_cross_example"
    warmup_days = 60

    def context(self, coin: str, daily: pd.DataFrame) -> Context:
        n = int(self.p("trend_len", 50))
        c = daily["close"]
        sma = c.rolling(n).mean()
        last, prev = float(sma.iloc[-1]), float(sma.iloc[-6]) if len(sma) > 6 else float("nan")
        checks = {"above_sma": bool(c.iloc[-1] > last), "sma_rising": bool(last >= prev)}
        return Context(coin, daily.index[-1], all(checks.values()), checks, {"sma": last}, float(c.iloc[-1]))

    def step(self, state: ArmState, ctx: Context, bars: pd.DataFrame,
             blocked: bool = False) -> tuple[ArmState, EntryIntent | None]:
        if state.phase == "in_position":
            return state, None
        has_order = state.phase == "confirmed" and state.trigger_px is not None
        if blocked or not ctx.eligible:
            return ArmState(ctx.coin), (EntryIntent("cancel", ctx.coin, reason="blocked_or_ineligible") if has_order else None)
        fast = ind.ema(bars["close"], int(self.p("fast_len", 20)))
        b, prev_close = bars.iloc[-1], float(bars["close"].iloc[-2])
        crossed_up = b["close"] > fast.iloc[-1] and prev_close <= fast.iloc[-2]
        if has_order and b["close"] < fast.iloc[-1]:
            return ArmState(ctx.coin), EntryIntent("cancel", ctx.coin, reason="lost_fast_ema")
        if not (crossed_up or has_order):
            return state, None
        trigger = float(b["high"])
        stop = float(bars["low"].iloc[-int(self.p("stop_lookback", 4)):].min()) * (1 - self.p("stop_buffer_pct", 0.1) / 100)
        limit = trigger * (1 + self.cfg.entry_limit_offset_pct / 100)
        new = replace(state, phase="confirmed", trigger_px=trigger, limit_px=limit, stop_px=stop, signal_bar=bars.index[-1])
        return new, EntryIntent("modify" if has_order else "place", ctx.coin, trigger, limit, stop, bars.index[-1])

    def take_profit_legs(self, pos: Position) -> list[tuple[float, float]]:
        return [(pos.entry_px + float(self.p("tp_r", 2.0)) * pos.r, float(self.p("tp_frac", 0.5)))]

    def on_daily_close(self, pos: Position, ctx: Context, now: pd.Timestamp) -> ExitIntent | None:
        if ctx.close is not None and ctx.close < ctx.data.get("sma", float("-inf")):
            return ExitIntent("close", pos.coin, pos.size, reason="daily_close_below_sma")
        return None
