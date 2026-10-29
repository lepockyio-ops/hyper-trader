"""配置加载。

- 参数来自 config/settings.toml（复制 settings.example.toml 后修改）。
- 密钥只从环境变量读取，绝不写进文件或日志：
    HL_ACCOUNT_ADDRESS   主账户（或子账户）地址，用于查询和对账
    HL_API_WALLET_KEY    API 钱包私钥（只能交易，不能提现）
- 默认连接测试网。连接主网需要同时满足：
    settings.toml 中 runtime.network = "mainnet"
    环境变量 HL_ALLOW_MAINNET = "I_HAVE_PASSED_G1_TO_G8"
  这是对应手册 G9 的人工闸门，程序不会自行打开。
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from s03.alerts.core import AlertConfig
from s03.alerts.openclaw import OpenClawConfig

MAINNET_GATE_VALUE = "I_HAVE_PASSED_G1_TO_G8"


@dataclass(frozen=True)
class StrategyConfig:
    """框架层的策略设置。具体交易规则在插件里（s03/strategy/api.py），参数放 [strategy.params]。"""
    coins: tuple[str, ...] = ("BTC", "ETH", "SOL", "BNB", "HYPE")   # 示例标的，按你的策略修改
    # 策略插件（"模块:类名"）。默认是空白模板：不会产生任何入场
    plugin: str = "s03.strategies.template:BlankStrategy"
    params: dict = field(default_factory=dict)              # 传给插件的参数
    # 入场执行（框架）
    entry_impl: str = "A"                  # A：突破触发单 | B：收盘确认后 IOC 限价
    entry_limit_offset_pct: float = 0.1
    # 止损距离护栏（框架强制，插件不能绕过；示例值）
    stop_min_pct: float = 0.8
    stop_max_pct: float = 3.0
    stop_min_mode: str = "skip"            # 太近时：skip = 放弃入场；floor = 止损设在入场价下方 stop_min_pct
    max_hold_days: int = 30                # 框架强制的最长持有天数（示例值）
    # 事件窗口（UTC，ISO 字符串列表；可由 OpenBB 或手工维护）
    event_times_utc: tuple[str, ...] = ()
    event_before_min: int = 120
    event_after_min: int = 60
    # 官方日程生成的事件文件（s03-events 每天更新）；空字符串 = 只用上面的手工列表
    event_file: str = ""
    event_file_max_age_days: float = 3.0
    event_file_required: bool = True          # 设置了 event_file 但文件缺失或过旧时，拒绝新开仓
    # BlockBeats 新闻事件守卫（可选）：按币种暂停新开仓。文件由 `python -m fwresearch news collect` 生成；
    # 空字符串或文件不存在 = 未启用；文件过期（> news_max_age_min）= 忽略并记日志，不会卡住交易
    news_file: str = ""
    news_venue: str = "hyperliquid"           # 交易所级事件（如该交易所被盗、暂停提现）会封禁本框架全部币种
    news_max_age_min: float = 30.0


@dataclass(frozen=True)
class CostConfig:
    taker_fee: float = 0.00045
    maker_fee: float = 0.00015
    slippage: float = 0.0003
    funding_per_hour_est: float = 0.0000125
    expected_hold_hours: int = 240
    # v1.0 修正：资金费预留不超过止损距离的 20%（示例值），避免窄止损时仓位被过度压缩
    funding_cap_frac_of_stop: float = 0.2


@dataclass(frozen=True)
class RiskConfig:
    risk_per_trade: float = 0.005          # 来自用户描述
    max_open_risk: float = 0.015
    max_positions: int = 3
    max_account_leverage: float = 30.0     # 用户设定
    max_position_leverage: int = 30        # 用户设定（再受平台与公式约束）
    liq_to_stop_multiple: float = 3.0      # 强平距离 ≥ 3 × 止损距离（示例值）
    max_notional_frac: float = 1.0
    daily_loss_halt: float = 0.02          # 系统故障保护（示例值）
    weekly_loss_halt: float = 0.04
    consecutive_loss_pause: int = 6
    pause_hours: int = 24
    corr_threshold: float = 0.8
    corr_group_risk_cap: float = 0.005
    max_funding_abs_per_hour: float = 0.0001
    max_ref_deviation_pct: float = 0.3
    min_rate_budget: int = 2000
    stop_confirm_seconds: int = 10


@dataclass(frozen=True)
class RuntimeConfig:
    network: str = "testnet"               # testnet | mainnet
    mode: str = "shadow"                   # shadow（不下单）| live
    scan_interval_min: int = 15
    scan_delay_sec: int = 5
    action_expires_sec: int = 10
    data_dir: str = "var/data"
    journal_path: str = "var/journal.jsonl"
    state_path: str = "var/state.json"
    heartbeat_path: str = "var/heartbeat"
    kill_flag_path: str = "var/KILL"
    pause_flag_path: str = "var/PAUSE_ENTRY"   # 只停新开仓（不撤止损、不平仓）；OpenClaw 经白名单代理只能写这个
    watchdog_stale_sec: int = 120


@dataclass(frozen=True)
class MLConfig:
    """第 14 节：机器学习只做“过滤 / 降低风险”，默认关闭。参数借鉴 FreqAI 的同名设置。"""
    enabled: bool = False
    model_path: str = "var/ml/model.pkl"
    train_period_days: int = 365          # FreqAI: train_period_days（滑动训练窗口）
    test_period_days: int = 90            # FreqAI: backtest_period_days（每段预测多久后重训）
    purge_days: int = 30                  # 标签向前看的最长时间（= max_hold_days），训练与测试之间清除
    embargo_days: int = 5
    expiration_hours: int = 24 * 45       # FreqAI: expiration_hours（模型过期后回到无过滤）
    min_events: int = 300                 # 少于此数直接判定“证据不足”
    take_threshold: float = 0.5
    reduced_scale: float = 0.5            # 预测不佳时风险缩放；只能 ≤ 1
    outlier_method: str = "svm"           # FreqAI: use_SVM_to_remove_outliers；离群时回到无过滤
    mc_trials: int = 1000                 # Vibe-Trading 式 Monte Carlo：与同比例随机过滤比较
    min_expectancy_gain_r: float = 0.10
    min_take_rate: float = 0.5
    max_p_value: float = 0.05


@dataclass(frozen=True)
class Settings:
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    cost: CostConfig = field(default_factory=CostConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    ml: MLConfig = field(default_factory=MLConfig)
    alerts: AlertConfig = field(default_factory=AlertConfig)
    openclaw: OpenClawConfig = field(default_factory=OpenClawConfig)

    @property
    def data_url(self) -> str:
        """行情地址：影子模式读主网公开行情（只读）；live 模式与交易同一网络。"""
        from hyperliquid.utils import constants

        return constants.MAINNET_API_URL if self.runtime.mode == "shadow" else self.base_url

    @property
    def account_address(self) -> str | None:
        return os.environ.get("HL_ACCOUNT_ADDRESS")

    @property
    def api_wallet_key(self) -> str | None:
        return os.environ.get("HL_API_WALLET_KEY")

    @property
    def base_url(self) -> str:
        from hyperliquid.utils import constants

        if self.runtime.network == "mainnet":
            if os.environ.get("HL_ALLOW_MAINNET") != MAINNET_GATE_VALUE:
                raise PermissionError(
                    "主网未解锁：需设置 HL_ALLOW_MAINNET（见 config.py 说明，对应手册 G9）"
                )
            return constants.MAINNET_API_URL
        return constants.TESTNET_API_URL


def _build(cls, data: dict | None):
    if not data:
        return cls()
    kwargs = {}
    for k, v in data.items():
        kwargs[k] = tuple(v) if isinstance(v, list) else v
    return cls(**kwargs)


def load_settings(path: str | Path | None = None) -> Settings:
    """路径优先级：参数 > 环境变量 S03_SETTINGS（systemd 部署用 /etc/s03/settings.toml）> config/settings.toml。"""
    p = Path(path or os.environ.get("S03_SETTINGS") or "config/settings.toml")
    raw: dict = tomllib.loads(p.read_text("utf-8")) if p.exists() else {}
    return Settings(
        strategy=_build(StrategyConfig, raw.get("strategy")),
        cost=_build(CostConfig, raw.get("cost")),
        risk=_build(RiskConfig, raw.get("risk")),
        runtime=_build(RuntimeConfig, raw.get("runtime")),
        ml=_build(MLConfig, raw.get("ml")),
        alerts=_build(AlertConfig, raw.get("alerts")),
        openclaw=_build(OpenClawConfig, raw.get("openclaw")),
    )
