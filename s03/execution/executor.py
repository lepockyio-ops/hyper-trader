"""执行器：唯一会调用 Hyperliquid exchange 端点的模块（手册第 12 节对应表）。

- mode="shadow"：只记录将要发送的动作，不签名、不发送（影子模式）。
- mode="live"：用 API 钱包签名发送。需要环境变量 HL_API_WALLET_KEY 与 HL_ACCOUNT_ADDRESS。
- 每个动作发送前设置 expiresAfter = 当前 + action_expires_sec，避免旧指令延后执行。
- 风控层在调用这里之前已经检查过；执行器本身只做精度处理和防重复。

待测试网确认（scripts/testnet_checks.py）：
  1. 买入方向的触发单（tpsl="sl"，价格向上穿越触发）能否开新仓
  2. 市价触发止损时 limit_px 的作用
  3. scheduleCancel 是否会撤掉止损触发单
  5. normalTpsl 成组下单时，触发单能否作为父单、止损子单是否在入场成交后生效
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from s03.execution.cloid import make_cloid
from s03.execution.precision import next_px_above, round_px, round_sz
from s03.journal import Journal


@dataclass
class OrderResult:
    ok: bool
    raw: object
    oid: int | None = None
    filled: bool = False
    avg_px: float | None = None
    filled_sz: float | None = None


def _parse(resp) -> OrderResult:
    """解析第一个订单状态；成组下单（normalTpsl）时任一子单报错都视为失败。"""
    try:
        statuses = resp["response"]["data"]["statuses"]
        if any(isinstance(s, dict) and "error" in s for s in statuses):
            return OrderResult(False, resp)
        st = statuses[0]
        if isinstance(st, dict) and "filled" in st:
            f = st["filled"]
            return OrderResult(True, resp, f.get("oid"), True, float(f.get("avgPx", 0)), float(f.get("totalSz", 0)))
        if isinstance(st, dict) and "resting" in st:
            return OrderResult(True, resp, st["resting"].get("oid"))
        return OrderResult(resp.get("status") == "ok", resp)
    except Exception:
        return OrderResult(isinstance(resp, dict) and resp.get("status") == "ok", resp)


class Executor:
    def __init__(self, base_url: str, mode: str, journal: Journal, account_address: str | None,
                 api_wallet_key: str | None, expires_sec: int = 10):
        self.mode = mode
        self.journal = journal
        self.expires_sec = expires_sec
        self.exchange = None
        if mode == "live":
            if not (account_address and api_wallet_key):
                raise RuntimeError("live 模式需要 HL_ACCOUNT_ADDRESS 与 HL_API_WALLET_KEY 环境变量")
            import eth_account
            from hyperliquid.exchange import Exchange

            wallet = eth_account.Account.from_key(api_wallet_key)
            self.exchange = Exchange(wallet, base_url, account_address=account_address)

    # ---------------------------------------------------------- 内部
    def _send(self, action: str, fn, **log) -> OrderResult:
        self.journal.write("action", action=action, mode=self.mode, **log)
        if self.mode != "live":
            return OrderResult(True, {"shadow": True})
        self.exchange.set_expires_after(int(time.time() * 1000) + self.expires_sec * 1000)
        try:
            res = _parse(fn())
        except Exception as e:                          # 网络或签名错误
            res = OrderResult(False, {"exception": repr(e)})
        self.journal.write("action_result", action=action, ok=res.ok, oid=res.oid, raw=res.raw)
        return res

    # ---------------------------------------------------------- 杠杆
    def set_isolated_leverage(self, coin: str, leverage: int) -> OrderResult:
        return self._send("updateLeverage", lambda: self.exchange.update_leverage(leverage, coin, is_cross=False),
                          coin=coin, leverage=leverage, is_cross=False)

    # ---------------------------------------------------------- 入场（实现 A：触发单 + 附带止损）
    def entry_order_spec(self, coin: str, trigger_raw: float, limit_offset_pct: float, size: float, sz_dec: int):
        trig = next_px_above(trigger_raw, sz_dec)
        limit = round_px(trig * (1 + limit_offset_pct / 100), sz_dec, "up")
        sz = round_sz(size, sz_dec)
        order_type = {"trigger": {"triggerPx": trig, "isMarket": False, "tpsl": "sl"}}
        return trig, limit, sz, order_type

    def stop_order_spec(self, stop_px: float, sz_dec: int, worst_slippage_pct: float = 3.0):
        trig = round_px(stop_px, sz_dec, "down")
        limit = round_px(trig * (1 - worst_slippage_pct / 100), sz_dec, "down")
        return trig, limit, {"trigger": {"triggerPx": trig, "isMarket": True, "tpsl": "sl"}}

    def place_entry(self, coin: str, arm_key: str, trigger_raw: float, limit_offset_pct: float,
                    size: float, sz_dec: int, stop_px: float) -> OrderResult:
        """入场触发单与只减仓止损一起以 normalTpsl 成组提交：入场一成交，止损就在交易所生效，
        即使程序在两次扫描之间失联也有保护（是否支持“触发单作父单”需测试网检查 5 确认）。"""
        trig, limit, sz, ot = self.entry_order_spec(coin, trigger_raw, limit_offset_pct, size, sz_dec)
        if sz <= 0:
            return OrderResult(False, {"error": "size_rounds_to_zero"})
        s_trig, s_limit, s_ot = self.stop_order_spec(stop_px, sz_dec)
        c_entry, c_stop = make_cloid(coin, "entry", arm_key), make_cloid(coin, "stop", arm_key)
        reqs = [
            {"coin": coin, "is_buy": True, "sz": sz, "limit_px": limit, "order_type": ot, "reduce_only": False, "cloid": c_entry},
            {"coin": coin, "is_buy": False, "sz": sz, "limit_px": s_limit, "order_type": s_ot, "reduce_only": True, "cloid": c_stop},
        ]
        return self._send("entry_place", lambda: self.exchange.bulk_orders(reqs, grouping="normalTpsl"),
                          coin=coin, trigger=trig, limit=limit, size=sz, stop=s_trig,
                          cloid=str(c_entry), stop_cloid=str(c_stop))

    def modify_entry(self, coin: str, arm_key: str, trigger_raw: float, limit_offset_pct: float,
                     size: float, sz_dec: int, stop_px: float) -> OrderResult:
        """每 15 分钟同时改入场触发价与附带止损（2 次动作，计入地址额度）。"""
        trig, limit, sz, ot = self.entry_order_spec(coin, trigger_raw, limit_offset_pct, size, sz_dec)
        s_trig, s_limit, s_ot = self.stop_order_spec(stop_px, sz_dec)
        c_entry, c_stop = make_cloid(coin, "entry", arm_key), make_cloid(coin, "stop", arm_key)
        mods = [
            {"oid": c_entry, "order": {"coin": coin, "is_buy": True, "sz": sz, "limit_px": limit, "order_type": ot,
                                       "reduce_only": False, "cloid": c_entry}},
            {"oid": c_stop, "order": {"coin": coin, "is_buy": False, "sz": sz, "limit_px": s_limit, "order_type": s_ot,
                                      "reduce_only": True, "cloid": c_stop}},
        ]
        return self._send("entry_modify", lambda: self.exchange.bulk_modify_orders_new(mods),
                          coin=coin, trigger=trig, limit=limit, size=sz, stop=s_trig, cloid=str(c_entry))

    def cancel_entry(self, coin: str, arm_key: str) -> OrderResult:
        """撤入场单及其附带止损（未成交时止损子单也一并撤掉）。"""
        c_entry, c_stop = make_cloid(coin, "entry", arm_key), make_cloid(coin, "stop", arm_key)

        def go():
            r = self.exchange.cancel_by_cloid(coin, c_entry)
            try:
                self.exchange.cancel_by_cloid(coin, c_stop)
            except Exception:
                pass
            return r

        return self._send("entry_cancel", go, coin=coin, cloid=str(c_entry))

    # ---------------------------------------------------------- 入场（实现 B：收盘突破后 IOC 限价，备用）
    def place_entry_ioc(self, coin: str, arm_key: str, limit_raw: float, size: float, sz_dec: int) -> OrderResult:
        limit = round_px(limit_raw, sz_dec, "up")
        sz = round_sz(size, sz_dec)
        cloid = make_cloid(coin, "entry", arm_key)
        return self._send("entry_ioc", lambda: self.exchange.order(coin, True, sz, limit, {"limit": {"tif": "Ioc"}}, False, cloid),
                          coin=coin, limit=limit, size=sz, cloid=str(cloid))

    # ---------------------------------------------------------- 止损与分批
    def place_stop(self, coin: str, pos_key: str, stop_px: float, size: float, sz_dec: int,
                   worst_slippage_pct: float = 3.0) -> OrderResult:
        trig, limit, ot = self.stop_order_spec(stop_px, sz_dec, worst_slippage_pct)
        sz = round_sz(size, sz_dec)
        cloid = make_cloid(coin, "stop", pos_key)
        return self._send("stop_place", lambda: self.exchange.order(coin, False, sz, limit, ot, True, cloid),
                          coin=coin, trigger=trig, size=sz, cloid=str(cloid))

    def resize_stop(self, coin: str, pos_key: str, stop_px: float, size: float, sz_dec: int,
                    worst_slippage_pct: float = 3.0) -> OrderResult:
        trig, limit, ot = self.stop_order_spec(stop_px, sz_dec, worst_slippage_pct)
        sz = round_sz(size, sz_dec)
        cloid = make_cloid(coin, "stop", pos_key)
        return self._send("stop_modify", lambda: self.exchange.modify_order(cloid, coin, False, sz, limit, ot, True, cloid),
                          coin=coin, trigger=trig, size=sz, cloid=str(cloid))

    def place_take_profit(self, coin: str, pos_key: str, leg: str, px: float, size: float, sz_dec: int) -> OrderResult:
        p = round_px(px, sz_dec, "up")
        sz = round_sz(size, sz_dec)
        cloid = make_cloid(coin, leg, pos_key)
        ot = {"limit": {"tif": "Gtc"}}
        return self._send("tp_place", lambda: self.exchange.order(coin, False, sz, p, ot, True, cloid),
                          coin=coin, leg=leg, px=p, size=sz, cloid=str(cloid))

    def reduce_market(self, coin: str, size: float, sz_dec: int, slippage: float = 0.01) -> OrderResult:
        sz = round_sz(size, sz_dec)
        return self._send("reduce_market", lambda: self.exchange.market_close(coin, sz, None, slippage),
                          coin=coin, size=sz)

    def close_all_market(self, coin: str, slippage: float = 0.01) -> OrderResult:
        return self._send("close_market", lambda: self.exchange.market_close(coin, None, None, slippage), coin=coin)

    def cancel_by_oid(self, coin: str, oid: int) -> OrderResult:
        return self._send("cancel_oid", lambda: self.exchange.cancel(coin, oid), coin=coin, oid=oid)

    # ---------------------------------------------------------- 定时撤单（仅无持仓时使用）
    def schedule_cancel_in(self, seconds: int | None) -> OrderResult:
        t = None if seconds is None else int(time.time() * 1000) + max(5, seconds) * 1000
        return self._send("scheduleCancel", lambda: self.exchange.schedule_cancel(t), at=t)
