"""只读：用主网公开行情跑一遍当前策略插件的日线层，输出每个标的的检查结果。不需要密钥。

  python scripts/screen_now.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd  # noqa: E402
from hyperliquid.utils import constants  # noqa: E402

from s03.config import load_settings  # noqa: E402
from s03.data import okx_ref  # noqa: E402
from s03.data.hl_data import HLData  # noqa: E402
from s03.data.recorder import cross_check_closes  # noqa: E402
from s03.strategy.api import load_strategy  # noqa: E402


def main() -> None:
    s = load_settings()
    strat = load_strategy(s.strategy)
    if strat.is_template:
        print("当前是空白策略模板：没有任何入场条件。请先按 docs/STRATEGY_TEMPLATE.md 写好你的策略插件。")
    hl = HLData(constants.MAINNET_API_URL)
    today = pd.Timestamp.now(tz="UTC").normalize()
    dailies = {}
    for coin in s.strategy.coins:
        d = hl.recent(coin, "1d", 260)
        dailies[coin] = d[d.index < today]
    for coin, ctx in strat.contexts(dailies).items():
        try:
            xc = cross_check_closes(dailies[coin], okx_ref.history(coin, "1Dutc", 1))
        except Exception as e:
            xc = {"error": repr(e)}
        print(f"== {coin}  截至 {ctx.asof.date()}  策略 {strat.name}")
        print("   检查:", ctx.checks, " → 可以入场" if ctx.eligible else " → 不符合")
        print("   数据:", {k: (round(v, 6) if isinstance(v, float) else v) for k, v in ctx.data.items()})
        print("   HL vs OKX 日收盘:", xc)


if __name__ == "__main__":
    main()
