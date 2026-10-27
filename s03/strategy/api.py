"""策略插件接口（框架与策略的唯一边界）。

框架负责：数据、风控（仓位、杠杆、强平距离、总风险、相关性、事件窗口、熔断）、下单与对账、
止损单、分批止盈单的挂出与改量、影子模式、回放回测、告警、复盘。
策略只负责回答 4 个问题（见 StrategyPlugin）：
  1. 这个标的今天能不能做？                         context()
  2. 刚收盘的这根信号 K 线之后，入场单要挂、改还是撤？  step()
  3. 入场成交后，分批止盈挂在哪里？                    take_profit_legs()
  4. 每天日线收盘后，持仓要不要减仓或离场？            on_daily_close()

约定：
- 只做多。入场方式由框架的 entry_impl 决定：A = 突破触发单（价格到 trigger_px 才成交），B = 收盘确认后限价。
- 策略必须给出绝对止损价 stop_px；框架用 check_stop() 检查止损距离（stop_min_pct–stop_max_pct），
  不合格的入场意图会被框架拒绝或按 stop_min_mode 处理，策略不能绕过。
- 策略函数必须是纯函数：不读网络、时钟、文件；回测与实盘调用同一段代码。
- 策略参数放在 settings.toml 的 [strategy.params]，通过 self.p(...) 读取，并写明默认值。
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from typing import Any, Literal

import pandas as pd

Phase = Literal["idle", "armed", "confirmed", "in_position"]


@dataclass(frozen=True)
class Context:
    """日线层结论。asof = 最后一根已收盘日线的时间；eligible = 今天是否允许这个标的入场。"""
    coin: str
    asof: pd.Timestamp
    eligible: bool
    checks: dict = field(default_factory=dict)    # 各条件是否通过，写进计划卡与日志
    data: dict = field(default_factory=dict)      # 策略自己需要的数值（如均线、区间高低点）
    close: float | None = None


@dataclass(frozen=True)
class ArmState:
    """入场状态。框架只依赖：phase == "confirmed" 且 trigger_px 不为空 = 入场单已挂在 trigger_px。"""
    coin: str
    phase: Phase = "idle"
    trigger_px: float | None = None
    limit_px: float | None = None
    stop_px: float | None = None
    signal_bar: pd.Timestamp | None = None
    extra: dict = field(default_factory=dict)     # 策略自己的状态（需可 JSON 序列化）


@dataclass(frozen=True)
class EntryIntent:
    kind: Literal["place", "modify", "cancel"]
    coin: str
    trigger_px: float | None = None
    limit_px: float | None = None
    stop_px: float | None = None
    signal_bar: pd.Timestamp | None = None
    reason: str = ""


@dataclass(frozen=True)
class Position:
    coin: str
    entry_px: float
    stop_px: float
    size: float                                   # 剩余数量
    opened_at: pd.Timestamp
    flags: dict = field(default_factory=dict)     # 策略写入的持仓标记（如“已减仓一次”）

    @property
    def r(self) -> float:
        return self.entry_px - self.stop_px


@dataclass(frozen=True)
class ExitIntent:
    kind: Literal["reduce", "close"]
    coin: str
    size: float
    reason: str = ""
    set_flags: dict = field(default_factory=dict)  # 执行后写入持仓标记


def check_stop(entry: float, stop: float | None, cfg) -> float | None:
    """框架级止损检查：距离必须在 [stop_min_pct, stop_max_pct]。
    太近时按 cfg.stop_min_mode：skip = 拒绝；floor = 改为入场价下方 stop_min_pct。太远或无效一律拒绝。"""
    if stop is None or entry is None or entry <= 0 or stop >= entry:
        return None
    dist = (entry - stop) / entry * 100
    if dist < cfg.stop_min_pct:
        if cfg.stop_min_mode != "floor":
            return None
        stop, dist = entry * (1 - cfg.stop_min_pct / 100), cfg.stop_min_pct
    if dist > cfg.stop_max_pct:
        return None
    return stop


class StrategyPlugin:
    """所有策略的基类。子类至少实现 context() 和 step()。"""

    name = "base"
    is_template = False          # 空白模板设为 True：框架会提示“未配置策略”，并且不会产生任何入场
    warmup_days = 60             # context() 需要的最少日线根数
    signal_timeframe = "15m"     # 目前框架按 15 分钟扫描；改动需同步 runtime.scan_interval_min

    def __init__(self, cfg):
        self.cfg = cfg

    # ---------------------------------------------------------------- 参数
    def p(self, key: str, default: Any) -> Any:
        """读取 [strategy.params] 里的参数，没有就用默认值。"""
        return (self.cfg.params or {}).get(key, default)

    # ---------------------------------------------------------------- 必须实现
    def context(self, coin: str, daily: pd.DataFrame) -> Context:
        """daily：截至上一根已收盘日线的历史（UTC 索引，列 open/high/low/close/volume）。"""
        raise NotImplementedError

    def step(self, state: ArmState, ctx: Context, bars: pd.DataFrame,
             blocked: bool = False) -> tuple[ArmState, EntryIntent | None]:
        """每根信号 K 线收盘后调用。bars 只含已收盘 K 线，最后一根是刚收盘的。
        blocked=True 表示风控 / 事件窗口 / 数据异常禁止新开仓：已挂的入场单应撤销（返回 cancel）。"""
        raise NotImplementedError

    # ---------------------------------------------------------------- 可选
    def contexts(self, dailies: dict[str, pd.DataFrame]) -> dict[str, Context]:
        """一次算全部标的（需要跨标的条件时覆盖它，例如“大盘强势”）。"""
        return {c: self.context(c, d) for c, d in dailies.items()}

    def take_profit_legs(self, pos: Position) -> list[tuple[float, float]]:
        """入场成交后调用：返回 [(价格, 占初始仓位比例)]，框架挂只减仓限价单。默认不分批。"""
        return []

    def on_daily_close(self, pos: Position, ctx: Context, now: pd.Timestamp) -> ExitIntent | None:
        """每天日线收盘后调用一次。最长持有天数由框架检查，这里只写策略自己的离场规则。"""
        return None

    def ml_features(self, ctx: Context, bars: pd.DataFrame, stop_px: float, trigger_px: float) -> dict:
        """机器学习门控的额外特征（只用当时已知的数据）。默认不加。"""
        return {}


def load_strategy(cfg) -> StrategyPlugin:
    """按 settings.toml 的 strategy.plugin（"模块:类名"）加载策略。"""
    spec = cfg.plugin or "s03.strategies.template:BlankStrategy"
    mod, _, cls = spec.partition(":")
    klass = getattr(importlib.import_module(mod), cls)
    if not issubclass(klass, StrategyPlugin):
        raise TypeError(f"{spec} 必须继承 StrategyPlugin")
    return klass(cfg)
