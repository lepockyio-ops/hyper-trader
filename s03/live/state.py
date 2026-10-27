"""实盘状态持久化（JSON）。重启后先读状态、再对账，不补发错过的信号。"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

import pandas as pd

from s03.strategy.api import ArmState


@dataclass
class PosState:
    coin: str
    entry_px: float
    stop_px: float
    size: float
    remaining: float
    opened_at: str
    pos_key: str
    tp_legs: list = field(default_factory=list)   # [[价格, 占初始仓位比例, 是否已成交]]，来自策略的 take_profit_legs
    flags: dict = field(default_factory=dict)     # 策略写入的持仓标记
    stop_confirmed: bool = False
    realized: float = 0.0          # 影子模式累计已实现盈亏（含手续费）


@dataclass
class LiveState:
    arms: dict[str, dict] = field(default_factory=dict)         # coin -> ArmState 字段 + arm_key + size
    positions: dict[str, PosState] = field(default_factory=dict)
    day_start_equity: float | None = None
    week_start_equity: float | None = None
    day_key: str | None = None
    week_key: str | None = None
    consecutive_losses: int = 0
    last_loss_time: str | None = None
    last_daily_asof: str | None = None
    shadow_equity: float = 10_000.0

    # ---------------------------------------------------------- ArmState 转换
    def arm(self, coin: str) -> ArmState:
        d = self.arms.get(coin)
        if not d:
            return ArmState(coin)
        sb = d.get("signal_bar")
        return ArmState(coin, d["phase"], d.get("trigger_px"), d.get("limit_px"), d.get("stop_px"),
                        pd.Timestamp(sb) if sb else None, dict(d.get("extra") or {}))

    def set_arm(self, st: ArmState, arm_key: str | None = None, size: float | None = None) -> None:
        d = asdict(st)
        d["signal_bar"] = str(st.signal_bar) if st.signal_bar is not None else None
        old = self.arms.get(st.coin, {})
        d["arm_key"] = arm_key or old.get("arm_key")
        d["size"] = size if size is not None else old.get("size")
        d["risk_usd"] = old.get("risk_usd") if st.phase == "confirmed" else None
        self.arms[st.coin] = d

    # ---------------------------------------------------------- 读写
    def save(self, path: str) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        d = asdict(self)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2, default=str), "utf-8")
        tmp.replace(p)

    @classmethod
    def load(cls, path: str) -> "LiveState":
        p = Path(path)
        if not p.exists():
            return cls()
        d = json.loads(p.read_text("utf-8"))
        keep = set(PosState.__dataclass_fields__)
        d["positions"] = {k: PosState(**{f: x for f, x in v.items() if f in keep}) for k, v in d.get("positions", {}).items()}
        return cls(**d)
