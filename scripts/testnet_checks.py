"""测试网必测项（手册 G7）。只允许在测试网运行，使用极小仓位（约 12 USD 名义价值）。

  检查 1：买入方向触发单（tpsl=sl，价格向上穿越）能否开新仓  → 决定实现 A 是否可用
  检查 2：只减仓的市价止损触发单能否挂出，并在挂单列表中可见
  检查 3：scheduleCancel 触发后，止损触发单是否也被撤掉       → 决定定时撤单能否在有持仓时使用
  检查 4：按第 7 节公式设逐仓杠杆后，平台给出的强平价是否低于止损价
  检查 5：入场触发单 + 止损以 normalTpsl 成组提交时，入场成交后止损子单是否自动生效

运行前：
  export HL_ACCOUNT_ADDRESS=0x...      # 测试网主账户地址
  export HL_API_WALLET_KEY=0x...       # 测试网 API 钱包私钥（只在你本机设置）
  python scripts/testnet_checks.py --coin BTC
注意：检查 3 会消耗当天 10 次 scheduleCancel 触发额度中的 1 次。
"""
from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import eth_account  # noqa: E402
from hyperliquid.exchange import Exchange  # noqa: E402
from hyperliquid.info import Info  # noqa: E402
from hyperliquid.utils import constants  # noqa: E402

from s03.config import RiskConfig  # noqa: E402
from s03.execution.cloid import make_cloid  # noqa: E402
from s03.execution.precision import round_px, round_sz  # noqa: E402
from s03.risk.sizing import isolated_leverage  # noqa: E402

URL = constants.TESTNET_API_URL


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--coin", default="BTC")
    ap.add_argument("--notional", type=float, default=12.0)
    ap.add_argument("--wait", type=int, default=300, help="检查 1 等待触发的最长秒数")
    a = ap.parse_args()

    addr, key = os.environ.get("HL_ACCOUNT_ADDRESS"), os.environ.get("HL_API_WALLET_KEY")
    if not (addr and key):
        sys.exit("需要 HL_ACCOUNT_ADDRESS 与 HL_API_WALLET_KEY（测试网）")
    info = Info(URL, skip_ws=True)
    ex = Exchange(eth_account.Account.from_key(key), URL, account_address=addr)
    meta = {u["name"]: u for u in info.meta()["universe"]}
    szd, maxlev = meta[a.coin]["szDecimals"], meta[a.coin]["maxLeverage"]
    mid = float(info.all_mids()[a.coin])
    sz = round_sz(a.notional / mid * 1.05, szd) or 10 ** -szd
    run_id = str(int(time.time()))
    results = {}

    def position_size() -> float:
        for ap_ in info.user_state(addr).get("assetPositions", []):
            if ap_["position"]["coin"] == a.coin:
                return float(ap_["position"]["szi"])
        return 0.0

    # ---------- 检查 4 的前置：按公式设逐仓杠杆（假设 2.8% 止损）
    lev = isolated_leverage(0.028, maxlev, RiskConfig())
    print("updateLeverage", lev, ex.update_leverage(lev, a.coin, is_cross=False))

    # ---------- 检查 1 + 5：买入触发单（附带止损子单，normalTpsl）能否开仓、止损是否随之生效
    trig = round_px(mid * 1.0003, szd, "up")
    limit = round_px(trig * 1.005, szd, "up")
    stop_px = round_px(mid * 0.972, szd, "down")
    c1 = make_cloid(a.coin, "test-entry", run_id)
    c2 = make_cloid(a.coin, "test-stop", run_id)
    reqs = [
        {"coin": a.coin, "is_buy": True, "sz": sz, "limit_px": limit,
         "order_type": {"trigger": {"triggerPx": trig, "isMarket": False, "tpsl": "sl"}}, "reduce_only": False, "cloid": c1},
        {"coin": a.coin, "is_buy": False, "sz": sz, "limit_px": round_px(stop_px * 0.97, szd, "down"),
         "order_type": {"trigger": {"triggerPx": stop_px, "isMarket": True, "tpsl": "sl"}}, "reduce_only": True, "cloid": c2},
    ]
    r = ex.bulk_orders(reqs, grouping="normalTpsl")
    print("检查 1/5 成组下单返回：", r)
    results["5_grouped_order_accepted"] = isinstance(r, dict) and r.get("status") == "ok" and not any(
        isinstance(s, dict) and "error" in s for s in r.get("response", {}).get("data", {}).get("statuses", []))
    t_end = time.time() + a.wait
    while time.time() < t_end and position_size() <= 0:
        time.sleep(5)
    opened = position_size() > 0
    st = info.query_order_by_cloid(addr, c1)
    results["1_trigger_opens_position"] = opened
    print("检查 1：", "已开仓" if opened else "未开仓", "订单状态：", st)
    if not opened:
        ex.cancel_by_cloid(a.coin, c1)
        print("价格未上穿触发价或平台不支持；可增大 --wait 重试。后续检查改用市价开一个极小仓位。")
        print(ex.market_open(a.coin, True, sz, None, 0.01))
        time.sleep(3)

    # ---------- 检查 5 / 2：附带止损是否已生效；没有则单独挂只减仓市价止损
    time.sleep(3)
    oo = info.frontend_open_orders(addr)
    attached = opened and any(str(o.get("cloid", "")).lower() == str(c2).lower() for o in oo)
    results["5_attached_stop_live_after_fill"] = attached
    print("检查 5：附带止损", "已生效" if attached else "未见（将单独挂止损）")
    if not attached:
        try:
            ex.cancel_by_cloid(a.coin, c2)
        except Exception:
            pass
        c2 = make_cloid(a.coin, "test-stop2", run_id)
        r = ex.order(a.coin, False, round_sz(position_size(), szd), round_px(stop_px * 0.97, szd, "down"),
                     {"trigger": {"triggerPx": stop_px, "isMarket": True, "tpsl": "sl"}}, True, c2)
        print("检查 2 下单返回：", r)
        time.sleep(3)
        oo = info.frontend_open_orders(addr)
    visible = any(str(o.get("cloid", "")).lower() == str(c2).lower() for o in oo)
    results["2_stop_visible"] = visible
    print("检查 2：止损单", "可见" if visible else "不可见")

    # ---------- 检查 4：强平价 vs 止损价
    liq = None
    for ap_ in info.user_state(addr).get("assetPositions", []):
        if ap_["position"]["coin"] == a.coin:
            liq = ap_["position"].get("liquidationPx")
    results["4_liq_below_stop"] = liq is not None and float(liq) < stop_px
    print("检查 4：强平价", liq, "止损价", stop_px, "杠杆", lev)

    # ---------- 检查 3：scheduleCancel 是否撤掉止损触发单
    print("检查 3：设定 10 秒后定时撤单", ex.schedule_cancel(int(time.time() * 1000) + 10_000))
    time.sleep(25)
    oo = info.frontend_open_orders(addr)
    survived = any(str(o.get("cloid", "")).lower() == str(c2).lower() for o in oo)
    results["3_stop_survives_schedule_cancel"] = survived
    print("检查 3：止损单", "仍在" if survived else "被撤掉了 → 有持仓时不能用 scheduleCancel")

    # ---------- 清理
    print("清理：", ex.market_close(a.coin), ex.cancel_by_cloid(a.coin, c2) if survived else "")
    print("\n结果：", results)


if __name__ == "__main__":
    main()
