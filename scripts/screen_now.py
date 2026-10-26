"""只读：用主网公开行情跑一遍第 4 节的日线筛选，输出每个标的 R1–R6 与支撑区。不需要密钥。

  python scripts/screen_now.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hyperliquid.utils import constants  # noqa: E402

from s03.config import load_settings  # noqa: E402
from s03.data import okx_ref  # noqa: E402
from s03.data.hl_data import HLData  # noqa: E402
from s03.data.recorder import cross_check_closes  # noqa: E402
from s03.signals.s03 import daily_context  # noqa: E402


def main() -> None:
    s = load_settings()
    hl = HLData(constants.MAINNET_API_URL)
    for coin in s.strategy.coins:
        d = hl.recent(coin, "1d", 260)
        ntl = float((d["volume"] * d["close"]).iloc[-7:].mean())
        ctx = daily_context(coin, d, s.strategy, ntl)
        try:
            xc = cross_check_closes(d, okx_ref.history(coin, "1Dutc", 1))
        except Exception as e:
            xc = {"error": repr(e)}
        print(f"== {coin}  截至 {ctx.asof.date()} 收盘 {ctx.close}")
        print("   涨幅%:", {k: round(v, 1) for k, v in ctx.detail["returns_pct"].items()},
              " 7日均成交额(USD):", f"{ntl:,.0f}")
        print("   摆动低点:", ctx.detail["pivot_lows"], " 摆动高点:", ctx.detail["pivot_highs"])
        print(f"   EMA9 {ctx.ema_fast:.4g}  EMA21 {ctx.ema_mid:.4g}  EMA50 {ctx.ema_slow:.4g}  "
              f"振幅 近10日 {ctx.detail['range10']:.2f}% / 前30日 {ctx.detail['range30']:.2f}%")
        print(f"   AVWAP {ctx.detail['avwap']:.4g}（锚点 {ctx.detail['avwap_anchor']}）  支撑区 {ctx.zone}  回测次数 {ctx.retests}")
        print("   检查:", ctx.checks, " → 具备入场背景" if ctx.eligible else " → 不符合")
        print("   HL vs OKX 日收盘:", xc)


if __name__ == "__main__":
    main()
