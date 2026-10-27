"""空白策略模板：复制本文件（例如 s03/strategies/my_strategy.py），按 TODO 填入你的规则，
再在 settings.toml 设置  [strategy] plugin = "s03.strategies.my_strategy:MyStrategy"。

在你填好之前，框架用这个模板运行：不会产生任何入场，自检与日志会提示“未配置策略”。
填写前请先写好 docs/STRATEGY_TEMPLATE.md 里的策略规格（适用市场、信号、止损、仓位、离场、验证门槛）。
"""
from __future__ import annotations

import pandas as pd

from s03.strategy.api import ArmState, Context, EntryIntent, ExitIntent, Position, StrategyPlugin


class BlankStrategy(StrategyPlugin):
    name = "blank"
    is_template = True           # 你的策略里删掉这一行（或设为 False）
    warmup_days = 60             # TODO：context() 需要的最少日线根数

    def context(self, coin: str, daily: pd.DataFrame) -> Context:
        # TODO 1：这个标的今天能不能做？
        #   用 daily（截至上一根已收盘日线）计算你的条件，放进 checks；全部满足时 eligible=True。
        #   需要在后面用到的数值（均线、区间等）放进 data。
        return Context(coin, daily.index[-1], eligible=False, checks={"strategy_configured": False},
                       close=float(daily["close"].iloc[-1]))

    def step(self, state: ArmState, ctx: Context, bars: pd.DataFrame,
             blocked: bool = False) -> tuple[ArmState, EntryIntent | None]:
        # TODO 2：刚收盘的信号 K 线之后，入场单要挂、改还是撤？
        #   - blocked 或 not ctx.eligible：已挂单（state.phase == "confirmed"）返回 cancel，并回到 idle。
        #   - 满足入场条件：返回 ArmState(phase="confirmed", trigger_px=…, limit_px=…, stop_px=…)
        #     与 EntryIntent("place" 或 "modify", …)。stop_px 是绝对价格，框架会检查距离是否合规。
        return state, None

    def take_profit_legs(self, pos: Position) -> list[tuple[float, float]]:
        # TODO 3（可选）：分批止盈 [(价格, 占初始仓位比例)]，例如 [(pos.entry_px + 2 * pos.r, 0.5)]。
        return []

    def on_daily_close(self, pos: Position, ctx: Context, now: pd.Timestamp) -> ExitIntent | None:
        # TODO 4（可选）：日线收盘后的离场或减仓，例如跌破某条均线就 ExitIntent("close", …)。
        return None
