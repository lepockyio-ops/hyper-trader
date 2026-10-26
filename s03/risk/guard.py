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
        if self.in_event_window(now):
            v.fail("event_window")
        if self.event_calendar_stale():
            v.fail("event_calendar_stale")
        return v

    def event_calendar_stale(self) -> bool:
        from s03.events_calendar import file_is_stale

        st = self.strat
        return bool(st.event_file) and st.event_file_required and file_is_stale(st.event_file, st.event_file_max_age_days)

    def event_times(self) -> list[str]:
        from s03.events_calendar import load_event_times

        return list(self.strat.event_times_utc) + load_event_times(self.strat.event_file or None)

    def in_event_window(self, now: pd.Timestamp) -> bool:
        for s in self.event_times():
            t = pd.Timestamp(s)
            t = t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")
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

    def check_entry(self, coin: str, notional: float, risk_usd: float, acct: AccountSnapshot) -> Verdict:
        v = Verdict(True)
        r = self.risk
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
