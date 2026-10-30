"""风控层（手册第 7、8 节）。

设计为可独立运行的检查器：实盘循环每次下单前调用 check_entry()，任何一条不过就拒单。
它只能“更保守”：拒单、要求撤单、触发熔断；不能放宽参数，也不接受 AI 助手的输入。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from s03.config import RiskConfig, StrategyConfig


@dataclass
class AccountSnapshot:
    equity: float
    day_start_equity: float
    week_start_equity: float
    open_positions: dict[str, dict]          # coin -> {"notional", "risk_usd"}
    open_risk_usd: float
    consecutive_losses: int
    last_loss_time: pd.Timestamp | None
    rate_budget_left: int | None             # info.user_rate_limit 估算的剩余动作数


@dataclass
class MarketSnapshot:
    coin: str
    funding_per_hour: float
    ref_deviation_pct: float | None          # HL 标记价 vs OKX
    data_age_sec: float
    depth_usd_05pct: float | None            # ±0.5% 深度（买卖两侧较小者）
    usdc_peg_dev_pct: float | None = None


@dataclass
class Verdict:
    ok: bool
    reasons: list[str] = field(default_factory=list)

    def fail(self, msg: str) -> None:
        self.ok = False
        self.reasons.append(msg)


class RiskGuard:
    def __init__(self, risk: RiskConfig, strat: StrategyConfig, kill_flag_path: str,
                 pause_flag_path: str | None = None):
        self.risk = risk
        self.strat = strat
        self.kill_flag = Path(kill_flag_path)
        self.pause_flag = Path(pause_flag_path) if pause_flag_path else self.kill_flag.with_name("PAUSE_ENTRY")

    def paused(self) -> bool:
        return self.pause_flag.exists()

    def set_pause(self, reason: str, by: str) -> None:
        """暂停新开仓：已有持仓、交易所端止损和分批单都不动。恢复只能由人删除标记文件（run.py resume）。"""
        self.pause_flag.parent.mkdir(parents=True, exist_ok=True)
        self.pause_flag.write_text(json.dumps({"reason": reason, "by": by,
                                               "at": pd.Timestamp.now(tz="UTC").isoformat()}, ensure_ascii=False))

    # ---------------------------------------------------------- 熔断
    def killed(self) -> bool:
        return self.kill_flag.exists()

    def trip_kill(self, reason: str) -> None:
        self.kill_flag.parent.mkdir(parents=True, exist_ok=True)
        self.kill_flag.write_text(json.dumps({"reason": reason, "at": pd.Timestamp.now(tz="UTC").isoformat()}))

    # ---------------------------------------------------------- 全局禁入
    def global_block(self, acct: AccountSnapshot, now: pd.Timestamp) -> Verdict:
        v = Verdict(True)
        r = self.risk
        if self.killed():
            v.fail("kill_switch")
        if self.paused():
            v.fail("paused_by_operator")
        if acct.equity <= acct.day_start_equity * (1 - r.daily_loss_halt):
            v.fail("daily_loss_halt")
        if acct.equity <= acct.week_start_equity * (1 - r.weekly_loss_halt):
            v.fail("weekly_loss_halt")
        if acct.consecutive_losses >= r.consecutive_loss_pause and acct.last_loss_time is not None:
            if now - acct.last_loss_time < pd.Timedelta(hours=r.pause_hours):
                v.fail("consecutive_loss_pause")
        if acct.rate_budget_left is not None and acct.rate_budget_left < r.min_rate_budget:
            v.fail("rate_budget_low")
        try:
            cal_ok, cal_reason, times = self.calendar(now)
        except Exception as e:                                  # 日历检查本身出错：按不可用处理
            cal_ok, cal_reason, times = False, f"事件日历检查出错：{type(e).__name__}", []
        if not cal_ok:
            v.fail("event_calendar_stale")
        elif self.in_event_window(now, times):
            v.fail("event_window")
        return v

    def calendar(self, now: pd.Timestamp | None = None) -> tuple[bool, str, list]:
        """(可用, 原因, 事件时间)。event_file 为空 = 明确不用事件日历（本地调试、回放），只用 event_times_utc。
        设置了 event_file 且 event_file_required：文件缺失、损坏、过期、来源失败、覆盖不足都判为不可用（拒绝新开仓）。"""
        from s03.events_calendar import calendar_check

        st = self.strat
        times = [(t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC"))
                 for t in (pd.Timestamp(x) for x in st.event_times_utc)]
        if not st.event_file:
            return True, "", times
        ok, reason, ftimes = calendar_check(st.event_file, now, st.event_file_max_age_days)
        if not ok and st.event_file_required:
            return False, reason, times
        return True, reason, times + ftimes

    def event_calendar_stale(self, now: pd.Timestamp | None = None) -> bool:
        """兼容旧名：True = 日历不可用（会拒绝新开仓）。"""
        try:
            return not self.calendar(now)[0]
        except Exception:
            return True

    def in_event_window(self, now: pd.Timestamp, times: list | None = None) -> bool:
        if times is None:
            try:
                times = self.calendar(now)[2]
            except Exception:
                return True                                     # 读不出事件时间：按处于窗口内处理（不开仓）
        for t in times:
            if t - pd.Timedelta(minutes=self.strat.event_before_min) <= now <= t + pd.Timedelta(
                minutes=self.strat.event_after_min
            ):
                return True
        return False

    # ---------------------------------------------------------- 标的禁入
    def market_block(self, m: MarketSnapshot, planned_notional: float | None = None) -> Verdict:
        v = Verdict(True)
        r = self.risk
        if abs(m.funding_per_hour) > r.max_funding_abs_per_hour and m.funding_per_hour > 0:
            v.fail("funding_crowded_long")       # 只做多：多头拥挤时不进
        if m.ref_deviation_pct is None or abs(m.ref_deviation_pct) > r.max_ref_deviation_pct:
            v.fail("ref_price_mismatch_or_missing")
        if m.data_age_sec > 30:
            v.fail("stale_data")
        if m.usdc_peg_dev_pct is not None and abs(m.usdc_peg_dev_pct) > 0.5:
            v.fail("usdc_depeg")
        if planned_notional and m.depth_usd_05pct is not None and m.depth_usd_05pct < 20 * planned_notional:
            v.fail("thin_book")
        return v

    # ---------------------------------------------------------- 单笔检查
    def corr_scaled_risk(self, coin: str, acct: AccountSnapshot, daily_closes: pd.DataFrame) -> float:
        """与已有持仓 30 日相关系数 > 阈值时，同组风险合计不超过 corr_group_risk_cap。"""
        rf = self.risk.risk_per_trade
        if not acct.open_positions or coin not in daily_closes:
            return rf
        rets = daily_closes.pct_change().iloc[-30:]
        group_risk = 0.0
        for other, p in acct.open_positions.items():
            if other in rets and coin in rets:
                c = rets[coin].corr(rets[other])
                if np.isfinite(c) and c > self.risk.corr_threshold:
                    group_risk += p["risk_usd"] / acct.equity
        return max(0.0, min(rf, self.risk.corr_group_risk_cap - group_risk))

    # ---------------------------------------------------------- 新闻事件守卫（按币种，只收紧）
    def news_block(self, coin: str, now: pd.Timestamp | None = None) -> tuple[Verdict, str]:
        """新闻事件守卫（Odaily + BlockBeats）：该币或本交易所有重大事件时禁止新开仓。返回 (结论, 守卫状态)。
        状态 disabled / ok / degraded / stale / error；ok 与 degraded（一个来源失败）时封禁，过期或损坏时忽略（由日志告警）。"""
        from fwresearch.blockbeats import blocked, load_blocks

        v = Verdict(True)
        st = self.strat
        ts = (now or pd.Timestamp.now(tz="UTC")).to_pydatetime()
        blocks, status = load_blocks(st.news_file or None, ts, st.news_max_age_min)
        b = blocked(coin, st.news_venue, blocks)
        if b:
            v.fail(f"news_block:{b['category']}:{b['target']}")
        return v, status

    def check_entry(self, coin: str, notional: float, risk_usd: float, acct: AccountSnapshot,
                    now: pd.Timestamp | None = None) -> Verdict:
        v = Verdict(True)
        r = self.risk
        nb, _ = self.news_block(coin, now)
        for reason in nb.reasons:
            v.fail(reason)
        if coin in acct.open_positions:
            v.fail("already_in_position")
        if len(acct.open_positions) >= r.max_positions:
            v.fail("max_positions")
        if (acct.open_risk_usd + risk_usd) > acct.equity * r.max_open_risk + 1e-9:
            v.fail("max_open_risk")
        if risk_usd > acct.equity * r.risk_per_trade * 1.0001:
            v.fail("risk_per_trade")
        total_notional = sum(p["notional"] for p in acct.open_positions.values()) + notional
        if total_notional > acct.equity * r.max_account_leverage:
            v.fail("account_leverage")
        if notional > acct.equity * r.max_notional_frac * 1.0001:
            v.fail("notional_cap")
        return v
