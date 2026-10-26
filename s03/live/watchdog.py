"""看门狗：部署在另一台机器（或另一个进程），独立于主循环（手册第 12 节）。

心跳超过 watchdog_stale_sec 未更新时：只按 cloid 撤销入场触发单，保留止损单，并写告警。
建议给看门狗单独授权一个具名 API 钱包（approveAgent 支持多个具名钱包），与主程序分开。
它不开仓、不平仓、不改止损。
"""
from __future__ import annotations

import time
from pathlib import Path

import pandas as pd

from s03.config import Settings
from s03.execution.executor import Executor
from s03.journal import Journal
from s03.live.state import LiveState


def check_once(settings: Settings, executor: Executor, journal: Journal) -> bool:
    rt = settings.runtime
    hb = Path(rt.heartbeat_path)
    if not hb.exists():
        return True
    age = (pd.Timestamp.now(tz="UTC") - pd.Timestamp(hb.read_text().strip())).total_seconds()
    if age <= rt.watchdog_stale_sec:
        return True
    state = LiveState.load(rt.state_path)
    cancelled = []
    for coin, a in state.arms.items():
        if a.get("phase") == "confirmed" and a.get("arm_key"):
            executor.cancel_entry(coin, a["arm_key"])
            cancelled.append(coin)
    journal.write("watchdog_alert", heartbeat_age_sec=round(age), cancelled_entries=cancelled,
                  open_positions=list(state.positions))
    return False


def run(settings: Settings, poll_sec: int = 30) -> None:
    journal = Journal(settings.runtime.journal_path)
    executor = Executor(settings.base_url, settings.runtime.mode, journal, settings.account_address,
                        settings.api_wallet_key, settings.runtime.action_expires_sec)
    while True:
        try:
            check_once(settings, executor, journal)
        except Exception as e:
            journal.write("error", where="watchdog", error=repr(e))
        time.sleep(poll_sec)
