"""Hyperliquid 行情监控进程（独立于交易主循环，不需要私钥）。

  python scripts/run.py alerts

数据来源（全部只读，不消耗 LLM token）：
  - WebSocket candle 1m / 15m        → 波动告警
  - WebSocket activeAssetCtx         → 标记价、资金费率（每小时整点取一次）
  - OKX 公开 ticker（每 60 秒）       → 参考价偏离
  - var/state.json + info.frontendOpenOrders（每 10 秒，只需账户地址）→ 持仓、交易所端止损是否在场

告警写入 journal（kind="alert"），符合限流的再交给 OpenClaw 分析。
交易主循环不依赖本进程：本进程挂掉，交易与交易所端止损照常运行。
"""
from __future__ import annotations

import threading
import time

import pandas as pd

from s03.alerts.core import AlertEngine, BarCloser, PositionView
from s03.alerts.openclaw import OpenClawNotifier
from s03.config import Settings
from s03.journal import Journal
from s03.live.state import LiveState


def positions_from_state(state: LiveState, open_orders: list[dict] | None, shadow: bool) -> list[PositionView]:
    """把实盘状态文件转成只读持仓视图。影子模式没有真实挂单，视为止损在场。"""
    stops = set()
    for o in open_orders or []:
        if o.get("reduceOnly") and o.get("isTrigger") and o.get("side") == "A":     # 多头的止损是卖出触发单
            stops.add(o.get("coin"))
    out = []
    for coin, p in state.positions.items():
        if p.remaining <= 0:
            continue
        ts = pd.Timestamp(p.opened_at).timestamp() if p.opened_at else time.time()
        out.append(PositionView(coin, p.entry_px, p.stop_px, p.stop_px, shadow or coin in stops, ts))
    return out


class HLMonitor:
    def __init__(self, s: Settings, base_url: str | None = None):
        self.s = s
        self.coins = s.strategy.coins
        self.engine = AlertEngine(s.alerts)
        self.closer = BarCloser()
        self.journal = Journal(s.runtime.journal_path)
        self.notifier = OpenClawNotifier(s.openclaw, log=lambda d: self.journal.write("openclaw", **d))
        self.lock = threading.Lock()
        self.base_url = base_url or s.data_url
        self.last_ctx: dict[str, dict] = {}
        self.last_funding_hour: dict[str, int] = {}
        self.info = None

    # ------------------------------------------------------------------ 告警出口
    def handle(self, alerts) -> None:
        for a in alerts:
            to_llm = self.engine.allow_llm(a)
            self.journal.write("alert", **a.to_dict(), to_openclaw=to_llm and self.s.openclaw.enabled)
            if to_llm:
                self.notifier.send(a, self.market_extra(a.coin))

    def market_extra(self, coin: str) -> dict:
        st = self.engine.stats.get(coin)
        ctx = self.last_ctx.get(coin, {})
        return {"last_px": st.last_price if st else None, "mark_px": ctx.get("markPx"),
                "oracle_px": ctx.get("oraclePx"), "funding_1h": ctx.get("funding"),
                "open_interest": ctx.get("openInterest")}

    # ------------------------------------------------------------------ WebSocket 回调
    def on_candle(self, msg) -> None:
        d = msg.get("data", {}) if isinstance(msg, dict) else {}
        try:
            bar = {"t": int(d["t"]), "o": float(d["o"]), "h": float(d["h"]), "l": float(d["l"]), "c": float(d["c"])}
            coin, iv = d["s"], d["i"]
        except (KeyError, TypeError, ValueError):
            return
        with self.lock:
            self.engine.on_price(coin, bar["c"])
            closed = self.closer.push(coin, iv, bar)
            if closed is None:
                return
            if iv == "1m":
                self.handle(self.engine.on_bar_1m(coin, closed["t"] / 1000, closed["c"]))
            elif iv == "15m":
                self.handle(self.engine.on_bar_15m(coin, closed["t"] / 1000, closed["h"], closed["l"], closed["c"]))

    def on_ctx(self, msg) -> None:
        d = msg.get("data", {}) if isinstance(msg, dict) else {}
        coin, ctx = d.get("coin"), d.get("ctx") or {}
        if not coin or "funding" not in ctx:
            return
        with self.lock:
            self.last_ctx[coin] = ctx
            hour = int(time.time() // 3600)
            if self.last_funding_hour.get(coin) != hour:          # 每小时只记一期，口径与历史一致
                self.last_funding_hour[coin] = hour
                self.handle(self.engine.on_funding(coin, float(ctx["funding"])))

    # ------------------------------------------------------------------ 启动与循环
    def seed(self) -> None:
        from hyperliquid.info import Info

        info = Info(self.base_url, skip_ws=True)
        start = int((time.time() - 30 * 86400) * 1000)
        for c in self.coins:
            try:
                hist, t0 = [], start
                for _ in range(5):                                 # 每次最多返回 500 期，分页取满 30 天
                    page = info.funding_history(c, t0)
                    hist += page
                    if len(page) < 500:
                        break
                    t0 = int(page[-1]["time"]) + 1
                self.engine.seed_funding(c, [h["fundingRate"] for h in hist])
            except Exception as e:
                self.journal.write("error", where="alerts_seed", coin=c, error=repr(e))
            try:                                                   # 用最近 1 分钟 K 线填充波动统计
                now = int(time.time() * 1000)
                bars = info.candles_snapshot(c, "1m", now - 1500 * 60_000, now)
                for b in bars[:-1]:
                    self.engine.on_bar_1m(c, int(b["t"]) / 1000, float(b["c"]))
                bars15 = info.candles_snapshot(c, "15m", now - 20 * 900_000, now)
                for b in bars15[:-1]:
                    self.engine.on_bar_15m(c, int(b["t"]) / 1000, float(b["h"]), float(b["l"]), float(b["c"]))
            except Exception as e:
                self.journal.write("error", where="alerts_seed_bars", coin=c, error=repr(e))
        self.engine._last_fire.clear()                            # 预热期产生的告警不算数

    def connect(self) -> None:
        from hyperliquid.info import Info

        self.info = Info(self.base_url, skip_ws=False)
        for c in self.coins:
            for iv in ("1m", "15m"):
                self.info.subscribe({"type": "candle", "coin": c, "interval": iv}, self.on_candle)
            self.info.subscribe({"type": "activeAssetCtx", "coin": c}, self.on_ctx)
        self.journal.write("alerts_connected", coins=list(self.coins))

    def run_forever(self) -> None:
        from s03.data import okx_ref
        from s03.data.hl_data import HLData

        from s03.live.sdnotify import notify

        self.seed()
        self.notifier.start()
        self.connect()
        notify("READY=1")
        hl = HLData(self.base_url)
        shadow = self.s.runtime.mode != "live"
        addr = self.s.account_address
        last_ref = 0.0
        stale_since: float | None = None
        while True:
            time.sleep(10)
            notify("WATCHDOG=1")
            now = time.time()
            try:
                orders = hl.open_orders(addr) if (addr and not shadow) else []
                pos = positions_from_state(LiveState.load(self.s.runtime.state_path), orders, shadow)
                with self.lock:
                    self.handle(self.engine.check_positions(pos, now))
                    feed = self.engine.check_feed(self.coins, now)
                    self.handle(feed)
                if now - last_ref >= 60:
                    last_ref = now
                    for c in self.coins:
                        try:
                            ref = okx_ref.ref_price(c)
                        except Exception:
                            continue
                        with self.lock:
                            self.handle(self.engine.on_ref_price(c, ref, now))
                # 行情中断超过 60 秒：重建 WebSocket
                lus = [self.engine.stats[c].last_update for c in self.coins if c in self.engine.stats]
                if not lus or min(x or 0 for x in lus) < now - 60:
                    stale_since = stale_since or now
                    if now - stale_since > 60:
                        self.journal.write("alerts_reconnect")
                        try:
                            self.info.disconnect_websocket()
                        except Exception:
                            pass
                        self.connect()
                        stale_since = None
                else:
                    stale_since = None
            except Exception as e:
                self.journal.write("error", where="alerts_loop", error=repr(e))
