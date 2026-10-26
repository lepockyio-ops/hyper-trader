"""S-03 强势整理回调买入：信号模块（回测与实盘共用，纯函数，无 I/O）。

对应手册：
  第 4 节  第一层 R1–R6（日线强势与整理）、第二层 支撑区
  第 5 节  布防 → 回升确认 → 触发 → 止损 → 分批 → 追踪
任何参数改动都必须同时影响回测与实盘，因此这里不允许读取网络、时钟或文件。
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Literal

import pandas as pd

from s03.config import StrategyConfig
from s03.signals import indicators as ind


# ---------------------------------------------------------------- 日线层
@dataclass(frozen=True)
class Zone:
    lo: float
    hi: float
    members: tuple[str, ...]


@dataclass(frozen=True)
class DailyContext:
    coin: str
    asof: pd.Timestamp                 # 最后一根已收盘日线的时间
    close: float
    ema_fast: float
    ema_mid: float
    ema_slow: float
    atr: float
    checks: dict[str, bool]
    zone: Zone | None
    retests: int
    detail: dict = field(default_factory=dict)

    @property
    def eligible(self) -> bool:
        return all(self.checks.values()) and self.zone is not None


def _cluster(cands: list[tuple[str, float]], width_pct: float) -> list[Zone]:
    cands = sorted([c for c in cands if c[1] == c[1] and c[1] > 0], key=lambda x: x[1])
    zones: list[Zone] = []
    for i in range(len(cands)):
        group = [cands[i]]
        for j in range(i + 1, len(cands)):
            if cands[j][1] <= cands[i][1] * (1 + width_pct / 100):
                group.append(cands[j])
            else:
                break
        if len(group) >= 2:
            zones.append(Zone(group[0][1], group[-1][1], tuple(n for n, _ in group)))
    return zones


def _pick_zone(zones: list[Zone], close: float, arm_buffer_pct: float, invalidate_pct: float = 0.5) -> Zone | None:
    # 支撑区必须在价格下方或包住价格；价格已跌破下沿（超过缓冲）的区域视为压力，不用
    below = [z for z in zones
             if z.lo <= close * (1 + arm_buffer_pct / 100) and close >= z.lo * (1 - invalidate_pct / 100)]
    if not below:
        return None
    key = {"avwap", "ema_mid", "prior_high"}

    def prio(z: Zone):
        tags = {m.split(":")[0] for m in z.members}
        return (len(tags & key) >= 2, z.hi)       # 优先“AVWAP/21EMA/前高”中任两者，其次离价格最近

    return max(below, key=prio)


def _count_retests(daily: pd.DataFrame, zone: Zone) -> int:
    """从最近一次收盘向上站上 zone.hi 起，之后日线最低价回到区内的天数。"""
    above = daily["close"] > zone.hi
    cross = above & ~above.shift(1, fill_value=False)
    if not cross.any():
        return 0
    start = cross[cross].index[-1]
    after = daily.loc[start:].iloc[1:]
    return int((after["low"] <= zone.hi).sum())


def daily_context(
    coin: str,
    daily: pd.DataFrame,
    cfg: StrategyConfig,
    avg_daily_ntl_usd: float | None = None,
    market_ok: bool | None = None,
) -> DailyContext:
    """daily: 只含已收盘日线，至少 max(lookbacks)+1 根。"""
    c = daily["close"]
    last = float(c.iloc[-1])
    e_f, e_m, e_s = (ind.ema(c, n) for n in (cfg.ema_fast, cfg.ema_mid, cfg.ema_slow))
    a = ind.atr(daily, 14)
    k = cfg.ema_slope_days

    rets = {n: (last / float(c.iloc[-1 - n]) - 1) * 100 for n in cfg.momentum_lookbacks if len(c) > n}
    lows = ind.pivot_lows(daily.iloc[-120:], cfg.pivot_span)
    highs = ind.pivot_highs(daily.iloc[-120:], cfg.pivot_span)
    rng = ind.range_pct(daily)
    r10, r30 = float(rng.iloc[-10:].mean()), float(rng.iloc[-40:-10].mean())

    higher_low = len(lows) >= 2 and lows.iloc[-1] > lows.iloc[-2]
    lower_high = len(highs) >= 2 and highs.iloc[-1] < highs.iloc[-2]
    lower_low = len(lows) >= 2 and lows.iloc[-1] < lows.iloc[-2]
    slope_f = e_f.iloc[-1] - e_f.iloc[-1 - k]
    slope_m = e_m.iloc[-1] - e_m.iloc[-1 - k]

    checks = {
        "R1_momentum": any(v >= cfg.momentum_min_pct for v in rets.values()),
        "R2_liquidity": True if avg_daily_ntl_usd is None else avg_daily_ntl_usd >= cfg.min_avg_daily_ntl_usd,
        "R3_market": True if market_ok is None else bool(market_ok),
        "R4_higher_low": bool(higher_low),
        "R5_contraction": bool(r10 < r30 * cfg.contraction_ratio),
        "R6_above_emas": bool(last > e_f.iloc[-1] and last > e_m.iloc[-1] and slope_f >= 0 and slope_m >= 0),
        "not_excluded": not bool(lower_high and lower_low and last < e_m.iloc[-1] and slope_m < 0),
    }

    seg = daily.iloc[-cfg.avwap_anchor_lookback:]
    anchor = seg["high"].idxmax()
    av = ind.avwap(daily, anchor)
    cands: list[tuple[str, float]] = [
        ("ema_fast", float(e_f.iloc[-1])),
        ("ema_mid", float(e_m.iloc[-1])),
        ("ema_slow", float(e_s.iloc[-1])),
        ("avwap", av),
    ]
    cands += [(f"prior_high:{t.date()}", float(v)) for t, v in highs.iloc[-4:].items()]
    cands += [(f"prior_low:{t.date()}", float(v)) for t, v in lows.iloc[-4:].items()]
    zone = _pick_zone(_cluster(cands, cfg.zone_cluster_pct), last, cfg.arm_buffer_pct, cfg.invalidate_buffer_pct)
    retests = _count_retests(daily, zone) if zone else 0
    if zone is not None and retests >= cfg.max_retests:
        zone_ok = False
    else:
        zone_ok = True
    checks["zone_fresh"] = zone_ok

    return DailyContext(
        coin=coin,
        asof=daily.index[-1],
        close=last,
        ema_fast=float(e_f.iloc[-1]),
        ema_mid=float(e_m.iloc[-1]),
        ema_slow=float(e_s.iloc[-1]),
        atr=float(a.iloc[-1]),
        checks=checks,
        zone=zone,
        retests=retests,
        detail={
            "returns_pct": rets,
            "pivot_lows": [(str(t.date()), float(v)) for t, v in lows.iloc[-2:].items()],
            "pivot_highs": [(str(t.date()), float(v)) for t, v in highs.iloc[-2:].items()],
            "range10": r10,
            "range30": r30,
            "avwap": av,
            "avwap_anchor": str(anchor.date()),
        },
    )


# ---------------------------------------------------------------- 15 分钟层
Phase = Literal["idle", "armed", "confirmed", "in_position"]


@dataclass(frozen=True)
class ArmState:
    coin: str
    phase: Phase = "idle"
    trigger_px: float | None = None     # 前一根 15m 高点（未取整，执行层再按最小价位上取）
    limit_px: float | None = None
    stop_px: float | None = None
    signal_bar: pd.Timestamp | None = None


@dataclass(frozen=True)
class EntryIntent:
    kind: Literal["place", "modify", "cancel"]
    coin: str
    trigger_px: float | None = None
    limit_px: float | None = None
    stop_px: float | None = None
    signal_bar: pd.Timestamp | None = None
    reason: str = ""


def _stop_for(bar: pd.Series, day_low: float, entry: float, cfg: StrategyConfig) -> float | None:
    stop = float(bar["low"]) * (1 - cfg.stop_buffer_pct / 100)
    dist = (entry - stop) / entry * 100
    if dist < cfg.stop_min_pct:
        stop = day_low * (1 - cfg.stop_buffer_pct / 100)
        dist = (entry - stop) / entry * 100
    if dist < cfg.stop_min_pct:                         # v1.0：当日最低点仍太近
        if cfg.stop_min_mode == "floor":
            stop = entry * (1 - cfg.stop_min_pct / 100)
            dist = cfg.stop_min_pct
        else:
            return None
    if dist > cfg.stop_max_pct or dist <= 0:
        return None
    return stop


def step_intraday(
    state: ArmState,
    ctx: DailyContext,
    bars15: pd.DataFrame,
    cfg: StrategyConfig,
    blocked: bool = False,
) -> tuple[ArmState, EntryIntent | None]:
    """在每根 15 分钟 K 线收盘后调用一次。bars15 只含已收盘 K 线，最后一根为刚收盘的。

    blocked=True 表示风控 / 事件窗口 / 数据异常禁止新开仓：已布防的撤销。
    返回新状态与需要执行的入场意图（下单、改价、撤单或无动作）。
    """
    if state.phase == "in_position":
        return state, None

    b = bars15.iloc[-1]
    day = bars15.index[-1].normalize()
    day_low = float(bars15.loc[bars15.index >= day, "low"].min())
    has_order = state.phase == "confirmed" and state.trigger_px is not None

    def reset(reason: str):
        intent = EntryIntent("cancel", ctx.coin, reason=reason) if has_order else None
        return ArmState(ctx.coin), intent

    if blocked or not ctx.eligible:
        return reset("blocked_or_ineligible") if state.phase != "idle" else (state, None)

    z = ctx.zone
    assert z is not None
    if state.phase != "idle" and b["close"] < z.lo * (1 - cfg.invalidate_buffer_pct / 100):
        return reset("broke_support")              # 一路跌穿支撑：继续等待

    in_zone = b["low"] <= z.hi * (1 + cfg.arm_buffer_pct / 100)

    if state.phase == "idle":
        if in_zone and b["close"] >= z.lo * (1 - cfg.invalidate_buffer_pct / 100):
            state = replace(state, phase="armed")
        else:
            return state, None

    if state.phase == "armed":
        if not (in_zone and b["close"] > b["open"]):   # 等“下探后回升”的阳线
            return state, None

    # 已确认：触发价跟随最新一根已收盘 K 线的高点
    trigger = float(b["high"])
    limit = trigger * (1 + cfg.entry_limit_offset_pct / 100)
    stop = _stop_for(b, day_low, trigger, cfg)
    if stop is None:                                   # 止损 > 3%：不挂单，下一根再算
        if has_order:
            return replace(state, phase="confirmed", trigger_px=None), EntryIntent(
                "cancel", ctx.coin, reason="stop_too_wide"
            )
        return replace(state, phase="confirmed", trigger_px=None), None

    new = replace(state, phase="confirmed", trigger_px=trigger, limit_px=limit, stop_px=stop,
                  signal_bar=bars15.index[-1])
    kind = "modify" if has_order else "place"
    return new, EntryIntent(kind, ctx.coin, trigger, limit, stop, bars15.index[-1])


# ---------------------------------------------------------------- 持仓管理
@dataclass(frozen=True)
class Position:
    coin: str
    entry_px: float
    stop_px: float
    size: float
    opened_at: pd.Timestamp
    tp1_done: bool = False
    tp2_done: bool = False
    extension_done: bool = False

    @property
    def r(self) -> float:
        return self.entry_px - self.stop_px


@dataclass(frozen=True)
class ExitIntent:
    kind: Literal["tp_orders", "reduce", "close"]
    coin: str
    size: float = 0.0
    prices: tuple[tuple[float, float], ...] = ()   # (价格, 数量) —— 只减仓限价单
    reason: str = ""


def take_profit_plan(pos: Position, cfg: StrategyConfig, strong_market: bool) -> ExitIntent:
    """入场成交后调用：3R / 5R 只减仓限价单。强市且有利润缓冲时 3R 不减仓。"""
    legs = []
    if not strong_market:
        legs.append((pos.entry_px + cfg.tp1_r * pos.r, pos.size * cfg.tp1_frac))
    legs.append((pos.entry_px + cfg.tp2_r * pos.r, pos.size * cfg.tp2_frac))
    return ExitIntent("tp_orders", pos.coin, prices=tuple(legs), reason="3R/5R")


def on_daily_close(pos: Position, ctx: DailyContext, now: pd.Timestamp, cfg: StrategyConfig) -> ExitIntent | None:
    """每天 UTC 00:00 后第一次扫描时调用（ctx 为刚收盘的日线）。"""
    if (now - pos.opened_at) >= pd.Timedelta(days=cfg.max_hold_days):
        return ExitIntent("close", pos.coin, pos.size, reason="max_hold")
    if ctx.close < ctx.ema_fast:
        return ExitIntent("close", pos.coin, pos.size, reason="daily_close_below_ema9")
    if not pos.extension_done and ctx.close > ctx.ema_fast + cfg.extension_atr_mult * ctx.atr:
        return ExitIntent("reduce", pos.coin, pos.size / 2, reason="extended_from_ema9")
    return None
