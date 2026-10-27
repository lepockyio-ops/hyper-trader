"""候选事件数据集：每次策略插件发出“挂入场单”就记为一个事件。

为什么用事件而不是成交：4 个标的的实际成交太少（回放中 SOL 7 个月仅 2–4 笔），
而每次布防确认都会形成一个候选事件。每个事件独立模拟一笔假设交易（不受持仓上限影响），
标签 = 扣除成本后的 R 倍数。标签向前看最多 max_hold_days，训练时按 purge_days 清除。
"""
from __future__ import annotations

import pandas as pd

from s03.config import Settings
from s03.ml.features import build_features
from s03.research import sim
from s03.strategy.api import ArmState, Position, StrategyPlugin, check_stop, load_strategy


def simulate_event(bars_after: pd.DataFrame, trigger: float, limit: float, stop: float, settings: Settings,
                   fill_window: int = 4, strategy: StrategyPlugin | None = None) -> dict | None:
    """固定触发价与止损，前 fill_window 根内成交才算；之后按止损 / 策略的分批止盈 / 时间上限退出。"""
    s, c = settings.strategy, settings.cost
    entry_idx, fill = None, None
    for k in range(min(fill_window, len(bars_after))):
        fill = sim.entry_fill_a(bars_after.iloc[k], trigger, limit)
        if fill is not None:
            entry_idx = k
            break
    if fill is None:
        return None
    entry = fill * (1 + c.slippage)
    r_px = entry - stop
    if r_px <= 0:
        return None
    per_unit = r_px + entry * (2 * (c.taker_fee + c.slippage))
    remaining, pnl = 1.0, -entry * c.taker_fee
    t0 = bars_after.index[entry_idx]
    strat = strategy or load_strategy(s)
    legs = strat.take_profit_legs(Position("event", entry, stop, 1.0, t0))
    tps = [(f"tp{k + 1}", float(px), float(fr)) for k, (px, fr) in enumerate(legs) if px > entry and 0 < fr <= 1]
    exit_time = None
    for t, bar in bars_after.iloc[entry_idx:].iterrows():
        pnl -= remaining * entry * c.funding_per_hour_est * 0.25
        for ev in sim.bar_exits(bar, stop, tps, c.slippage):
            q = remaining if ev.frac_of_initial < 0 else min(remaining, ev.frac_of_initial)
            fee = c.taker_fee if ev.kind == "stop" else c.maker_fee
            pnl += q * (ev.price - entry) - q * ev.price * fee
            remaining -= q
            tps = [x for x in tps if x[0] != ev.kind]
        if remaining <= 1e-9:
            exit_time = t
            break
        if t - t0 >= pd.Timedelta(days=s.max_hold_days):
            px = float(bar["close"]) * (1 - c.slippage)
            pnl += remaining * (px - entry) - remaining * px * c.taker_fee
            remaining, exit_time = 0.0, t
            break
    if remaining > 1e-9:
        return None                                    # 数据结束前未平仓：标签未知，不纳入
    return {"entry_time": t0, "exit_time": exit_time, "entry": entry, "R": pnl / per_unit}


def build_events(coin: str, daily: pd.DataFrame, bars15: pd.DataFrame, settings: Settings,
                 btc_daily: pd.DataFrame | None = None, warmup_days: int = 200) -> pd.DataFrame:
    s = settings.strategy
    strat = load_strategy(s)
    state = ArmState(coin)
    ctx_cache: dict = {}
    rows = []
    start = daily.index[min(warmup_days, len(daily) - 1)]
    b15 = bars15[bars15.index >= start]
    for i in range(1, len(b15) - 1):
        t = b15.index[i]
        day = t.normalize()
        if day not in ctx_cache:
            hist = daily[daily.index < day]
            ctx_cache[day] = strat.context(coin, hist) if len(hist) >= strat.warmup_days else None
        ctx = ctx_cache[day]
        if ctx is None:
            continue
        hist15 = b15.iloc[max(0, i - 200): i + 1]
        prev_phase = state.phase
        state, intent = strat.step(state, ctx, hist15)
        if intent is None or intent.kind != "place" or prev_phase == "confirmed":
            continue
        stop = check_stop(intent.trigger_px, intent.stop_px, s)
        if stop is None:
            continue
        out = simulate_event(b15.iloc[i + 1:], intent.trigger_px, intent.limit_px, stop, settings, strategy=strat)
        if out is None:
            continue
        btc_hist = btc_daily[btc_daily.index < day] if btc_daily is not None else None
        feats = build_features(ctx, hist15, stop, intent.trigger_px, btc_hist,
                               extra=strat.ml_features(ctx, hist15, stop, intent.trigger_px))
        rows.append({"coin": coin, "signal_time": t, **feats, **out})
    return pd.DataFrame(rows)
