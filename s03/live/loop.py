"""实盘 / 影子循环：每根 15 分钟 K 线收盘后 scan_delay_sec 秒运行一次（手册第 8 节）。

一次循环的顺序：
  1 心跳与熔断检查  2 拉数据并核对  3 账户快照与对账  4 管理持仓（止损确认、分批、日线规则）
  5 对每个标的调用策略插件 step()（与回测同一函数）  6 风控校验后下单 / 改单 / 撤单  7 写日志与状态

mode="shadow"：用主网实时行情跑完整逻辑，成交用 research.sim 模拟，不发送任何订单。
mode="live"：通过 Executor 发送到 settings.base_url（默认测试网）。
"""
from __future__ import annotations

import threading
import time
import traceback
from pathlib import Path

import pandas as pd

from s03.config import Settings
from s03.data import okx_ref
from s03.data.hl_data import HLData
from s03.execution.executor import Executor
from s03.execution.precision import next_px_above, round_px
from s03.journal import Journal
from s03.live.state import LiveState, PosState
from s03.research import sim
from s03.risk.guard import AccountSnapshot, MarketSnapshot, RiskGuard
from s03.risk.sizing import isolated_leverage, size_position
from s03.strategy.api import ArmState, ExitIntent, Position, check_stop, load_strategy


class LiveLoop:
    def __init__(self, settings: Settings, data_base_url: str | None = None):
        self.s = settings
        rt = settings.runtime
        # 影子模式默认读主网公开行情（只读，不需要密钥）；live 模式读写同一网络
        from hyperliquid.utils import constants

        if rt.mode == "shadow":
            data_url = data_base_url or constants.MAINNET_API_URL
            exec_url = constants.TESTNET_API_URL
        else:
            data_url = exec_url = settings.base_url
        self.hl = HLData(data_url)
        self.journal = Journal(rt.journal_path)
        self.exec = Executor(exec_url, rt.mode, self.journal, settings.account_address,
                             settings.api_wallet_key, rt.action_expires_sec)
        self.guard = RiskGuard(settings.risk, settings.strategy, rt.kill_flag_path, rt.pause_flag_path)
        self.state = LiveState.load(rt.state_path)
        self._daily: dict[str, tuple[pd.Timestamp, pd.DataFrame]] = {}
        self.lock = threading.RLock()          # 15 分钟循环与成交监听线程共用
        from s03.ml.gate import MLGate

        self.mlgate = MLGate(settings.ml)      # 默认关闭；开启需你批准的模型哈希
        self.strategy = load_strategy(settings.strategy)
        if self.strategy.is_template:
            self.journal.write("alert", msg="strategy_not_configured",
                               detail="正在使用空白策略模板：不会产生任何入场。见 docs/STRATEGY_TEMPLATE.md")

    # ================================================================ 调度
    def run_forever(self) -> None:
        step = self.s.runtime.scan_interval_min * 60
        from s03.live.sdnotify import notify

        if self.s.runtime.mode == "live":
            self.start_fill_listener()
        notify("READY=1")
        while True:
            now = time.time()
            nxt = (int(now) // step + 1) * step + self.s.runtime.scan_delay_sec
            while time.time() < nxt:                       # 等待期间每 30 秒向 systemd 报一次“还活着”
                notify("WATCHDOG=1")
                time.sleep(max(0.5, min(30.0, nxt - time.time())))
            t0 = time.time()
            try:
                self.cycle()                               # 一轮卡住超过 WatchdogSec（20 分钟）会被 systemd 重启
            except Exception as e:
                self.journal.write("error", where="cycle", error=repr(e), tb=traceback.format_exc())
            notify("WATCHDOG=1")
            if time.time() - t0 > 60:
                self.journal.write("alert", msg="cycle_slow", seconds=round(time.time() - t0, 1))

    def heartbeat(self) -> None:
        p = Path(self.s.runtime.heartbeat_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(pd.Timestamp.now(tz="UTC").isoformat())

    # ================================================================ 一次循环
    def cycle(self) -> None:
        with self.lock:
            self._cycle()

    # ================================================================ 成交监听（live）
    def start_fill_listener(self) -> None:
        """websocket userFills：入场成交后几秒内对账，确认止损已生效并挂分批止盈单，
        不必等下一次 15 分钟扫描。止损本身已随入场单成组提交，这里是第二道保险。"""
        from hyperliquid.info import Info

        ws_info = Info(self.s.base_url, skip_ws=False)

        def on_msg(msg):
            data = msg.get("data", {}) if isinstance(msg, dict) else {}
            if data.get("isSnapshot"):
                return
            coins = {f.get("coin") for f in data.get("fills", [])}
            if any(self.state.arms.get(c, {}).get("phase") == "confirmed" for c in coins) or \
                    any(c in self.state.positions for c in coins):
                threading.Thread(target=self._on_fill_event, args=(sorted(coins),), daemon=True).start()

        ws_info.subscribe({"type": "userFills", "user": self.s.account_address}, on_msg)
        self.journal.write("fill_listener_started")

    def _on_fill_event(self, coins) -> None:
        time.sleep(1.0)
        try:
            with self.lock:
                self.reconcile(self.hl.asset_ctxs())
                self.state.save(self.s.runtime.state_path)
            self.journal.write("fill_event_reconciled", coins=coins)
        except Exception as e:
            self.journal.write("error", where="fill_listener", error=repr(e))

    def _cycle(self) -> None:
        now = pd.Timestamp.now(tz="UTC")
        self.heartbeat()
        if self.guard.killed():
            self.shutdown("kill_flag")
            return

        ctxs = self.hl.asset_ctxs()
        acct = self.account_snapshot(ctxs)
        self.roll_periods(acct, now)
        block_new = False
        if self.s.runtime.mode == "live" and not self.reconcile(ctxs):
            block_new = True

        dctx = self.daily_contexts(ctxs)
        new_day = self.state.last_daily_asof != str(dctx[self.s.strategy.coins[0]].asof)

        self.manage_positions(ctxs, dctx, now, new_day)
        acct = self.account_snapshot(ctxs)
        gb = self.guard.global_block(acct, now)

        news_status = "disabled"
        for coin in self.s.strategy.coins:
            if coin in self.state.positions:
                continue
            nb, news_status = self.guard.news_block(coin, now)       # 按币种：有重大事件时撤掉已挂的入场单
            self.step_coin(coin, ctxs[coin], dctx[coin], acct, blocked=block_new or not gb.ok or not nb.ok,
                           block_reasons=gb.reasons + nb.reasons)
        if news_status in ("stale", "error", "degraded"):      # degraded = 两个新闻来源有一个失败（封禁照常）
            self.journal.write("news_guard_degraded", status=news_status, file=self.s.strategy.news_file)

        if self.s.runtime.mode == "live":
            self.manage_schedule_cancel()
        self.state.last_daily_asof = str(dctx[self.s.strategy.coins[0]].asof)
        self.state.save(self.s.runtime.state_path)
        self.journal.write("cycle", equity=acct.equity, open_positions=list(self.state.positions),
                           arms={k: v.get("phase") for k, v in self.state.arms.items()},
                           global_block=gb.reasons, news_guard=news_status)

    # ================================================================ 数据
    def daily_contexts(self, ctxs) -> dict:
        """日线层交给策略插件；框架只负责取数据（只含已收盘日线）。"""
        today = pd.Timestamp.now(tz="UTC").normalize()
        dailies = {}
        for coin in self.s.strategy.coins:
            d = self._daily_df(coin)
            dailies[coin] = d[d.index < today]
        return self.strategy.contexts(dailies)

    def _daily_df(self, coin: str) -> pd.DataFrame:
        today = pd.Timestamp.now(tz="UTC").normalize()
        cached = self._daily.get(coin)
        if cached and cached[0] == today:
            return cached[1]
        df = self.hl.recent(coin, "1d", 260)
        self._daily[coin] = (today, df)
        return df

    def market_snapshot(self, coin: str, c: dict, planned_notional: float | None) -> MarketSnapshot:
        try:
            ref = okx_ref.ref_price(coin)
            dev = (c["markPx"] / ref - 1) * 100
        except Exception:
            dev = None
        try:
            depth = self.hl.depth_usd(coin, 0.5)
        except Exception:
            depth = None
        age = (pd.Timestamp.now(tz="UTC") - c["fetched_at"]).total_seconds()
        return MarketSnapshot(coin, c["funding"], dev, age, depth)

    # ================================================================ 账户
    def account_snapshot(self, ctxs) -> AccountSnapshot:
        st = self.state
        if self.s.runtime.mode == "live":
            us = self.hl.account(self.s.account_address)
            equity = float(us["marginSummary"]["accountValue"])
            budget = self.hl.rate_budget_left(self.s.account_address)
        else:
            equity = st.shadow_equity + sum(
                (ctxs[p.coin]["markPx"] - p.entry_px) * p.remaining for p in st.positions.values())
            budget = None
        open_pos = {}
        open_risk = 0.0
        for p in st.positions.values():
            risk_usd = max(0.0, (p.entry_px - p.stop_px) * p.remaining)
            open_pos[p.coin] = {"notional": p.remaining * ctxs[p.coin]["markPx"], "risk_usd": risk_usd}
            open_risk += risk_usd
        return AccountSnapshot(
            equity=equity,
            day_start_equity=st.day_start_equity or equity,
            week_start_equity=st.week_start_equity or equity,
            open_positions=open_pos,
            open_risk_usd=open_risk,
            consecutive_losses=st.consecutive_losses,
            last_loss_time=pd.Timestamp(st.last_loss_time) if st.last_loss_time else None,
            rate_budget_left=budget,
        )

    def roll_periods(self, acct: AccountSnapshot, now: pd.Timestamp) -> None:
        dk, wk = str(now.date()), f"{now.isocalendar().year}-W{now.isocalendar().week}"
        if self.state.day_key != dk:
            self.state.day_key, self.state.day_start_equity = dk, acct.equity
        if self.state.week_key != wk:
            self.state.week_key, self.state.week_start_equity = wk, acct.equity

    # ================================================================ 对账（live）
    def reconcile(self, ctxs) -> bool:
        us = self.hl.account(self.s.account_address)
        ex_pos = {ap["position"]["coin"]: ap["position"] for ap in us.get("assetPositions", [])
                  if float(ap["position"]["szi"]) != 0}
        ok = True
        for coin, p in ex_pos.items():
            if coin not in self.s.strategy.coins or float(p["szi"]) < 0:
                self.journal.write("alert", msg="unexpected_position", coin=coin, szi=p["szi"])
                self.guard.trip_kill(f"unexpected_position:{coin}")
                return False
            if coin not in self.state.positions:
                arm = self.state.arms.get(coin, {})
                if arm.get("phase") == "confirmed":
                    self.on_entry_filled(coin, float(p["entryPx"]), float(p["szi"]), arm, ctxs)
                else:
                    self.journal.write("alert", msg="position_without_arm", coin=coin)
                    self.guard.trip_kill(f"position_without_arm:{coin}")
                    return False
            else:
                ps = self.state.positions[coin]
                szi = float(p["szi"])
                if szi < ps.remaining - 1e-9:                     # 分批止盈成交：止损单改为剩余数量
                    ps.remaining = szi
                    self.exec.resize_stop(coin, ps.pos_key, ps.stop_px, szi, ctxs[coin]["szDecimals"])
        for coin in list(self.state.positions):
            if coin not in ex_pos:                                  # 止损或全部止盈已成交
                self.on_position_closed(coin)
        return ok

    # ================================================================ 入场成交后
    def on_entry_filled(self, coin: str, entry_px: float, size: float, arm: dict, ctxs) -> None:
        szd = ctxs[coin]["szDecimals"]
        pos_key = arm.get("arm_key") or str(pd.Timestamp.now(tz="UTC"))
        stop = float(arm["stop_px"])
        ps = PosState(coin, entry_px, stop, size, size, str(pd.Timestamp.now(tz="UTC")), pos_key)
        self.state.positions[coin] = ps
        self.state.set_arm(ArmState(coin, "in_position"))
        # 实现 A 的止损已随入场单成组提交；先确认它在挂单列表里，没有才补挂
        ps.stop_confirmed = self._stop_visible(coin, pos_key, 3)
        if not ps.stop_confirmed:
            res = self.exec.place_stop(coin, pos_key, stop, size, szd)
            ps.stop_confirmed = res.ok and self._stop_visible(coin, pos_key, self.s.risk.stop_confirm_seconds)
        if not ps.stop_confirmed:
            self.journal.write("alert", msg="stop_not_confirmed_closing", coin=coin)
            self.exec.close_all_market(coin)
            return
        legs = self.strategy.take_profit_legs(Position(coin, entry_px, stop, size, pd.Timestamp.now(tz="UTC")))
        ps.tp_legs = [[float(px), float(frac), False] for px, frac in legs if px > entry_px and 0 < frac <= 1]
        for i, (px, frac, _) in enumerate(ps.tp_legs):
            self.exec.place_take_profit(coin, pos_key, f"tp{i + 1}", px, size * frac, szd)
        self.journal.write("position_opened", coin=coin, entry=entry_px, stop=stop, size=size, tp_legs=ps.tp_legs)

    def _stop_visible(self, coin: str, pos_key: str, timeout: int) -> bool:
        if self.s.runtime.mode != "live":
            return True
        from s03.execution.cloid import make_cloid_hex

        want = make_cloid_hex(coin, "stop", pos_key)
        t_end = time.time() + timeout
        while time.time() < t_end:
            for o in self.hl.open_orders(self.s.account_address):
                if str(o.get("cloid", "")).lower() == want:
                    return True
            time.sleep(1)
        return False

    def on_position_closed(self, coin: str, pnl: float | None = None) -> None:
        ps = self.state.positions.pop(coin)
        if pnl is None and self.s.runtime.mode == "live":
            fills = self.hl.info.user_fills(self.s.account_address)
            t0 = pd.Timestamp(ps.opened_at).value // 10**6
            pnl = sum(float(f.get("closedPnl", 0)) - float(f.get("fee", 0))
                      for f in fills if f.get("coin") == coin and int(f.get("time", 0)) >= t0)
        pnl = pnl or 0.0
        if pnl <= 0:
            self.state.consecutive_losses += 1
            self.state.last_loss_time = str(pd.Timestamp.now(tz="UTC"))
        else:
            self.state.consecutive_losses = 0
        self.state.set_arm(ArmState(coin))
        risk_usd = ps.size * (ps.entry_px - ps.stop_px)
        self.journal.write("position_closed", coin=coin, pnl=pnl, entry=ps.entry_px, stop=ps.stop_px, size=ps.size,
                           opened_at=ps.opened_at, r=round(pnl / risk_usd, 3) if risk_usd > 0 else None)

    # ================================================================ 持仓管理
    def manage_positions(self, ctxs, dctx, now: pd.Timestamp, new_day: bool) -> None:
        c = self.s.cost
        for coin, ps in list(self.state.positions.items()):
            szd = ctxs[coin]["szDecimals"]
            if self.s.runtime.mode == "shadow":                     # 影子模式：用最新 15m K 线模拟止损/止盈
                bar = self.hl.recent(coin, "15m", 2).iloc[-1]
                tps = [(f"tp{i + 1}", leg[0], leg[1]) for i, leg in enumerate(ps.tp_legs) if not leg[2]]
                for ev in sim.bar_exits(bar, ps.stop_px, tps, c.slippage):
                    qty = ps.remaining if ev.frac_of_initial < 0 else min(ps.remaining, ps.size * ev.frac_of_initial)
                    fee = c.taker_fee if ev.kind == "stop" else c.maker_fee
                    pnl = qty * (ev.price - ps.entry_px) - qty * ev.price * fee
                    self.state.shadow_equity += pnl
                    ps.realized += pnl
                    ps.remaining -= qty
                    if ev.kind.startswith("tp"):
                        ps.tp_legs[int(ev.kind[2:]) - 1][2] = True
                    self.journal.write("shadow_exit", coin=coin, kind=ev.kind, px=ev.price, qty=qty, pnl=pnl)
                if ps.remaining <= 1e-12:
                    self.on_position_closed(coin, pnl=ps.realized)
                    continue
            if new_day:
                pos = Position(coin, ps.entry_px, ps.stop_px, ps.remaining, pd.Timestamp(ps.opened_at), dict(ps.flags))
                if now - pos.opened_at >= pd.Timedelta(days=self.s.strategy.max_hold_days):   # 框架强制
                    it = ExitIntent("close", coin, ps.remaining, reason="max_hold")
                else:
                    it = self.strategy.on_daily_close(pos, dctx[coin], now)
                if it is None:
                    continue
                self.journal.write("exit_intent", intent=it)
                if self.s.runtime.mode == "live":
                    self.exec.reduce_market(coin, it.size, szd)
                else:
                    px = ctxs[coin]["markPx"] * (1 - c.slippage)
                    pnl = it.size * (px - ps.entry_px) - it.size * px * c.taker_fee
                    self.state.shadow_equity += pnl
                    ps.realized += pnl
                    ps.remaining -= it.size
                    if ps.remaining <= 1e-12:
                        self.on_position_closed(coin, pnl=ps.realized)
                        continue
                ps.flags.update(it.set_flags or {})

    # ================================================================ 入场状态机
    def step_coin(self, coin: str, c: dict, ctx, acct: AccountSnapshot, blocked: bool, block_reasons) -> None:
        s = self.s
        bars15 = self.hl.recent(coin, "15m", 200)
        st = self.state.arm(coin)
        arm = self.state.arms.get(coin, {})
        szd = c["szDecimals"]

        # 影子模式：先用刚收盘的 K 线检查已挂触发单是否成交
        if s.runtime.mode == "shadow" and st.phase == "confirmed" and st.trigger_px:
            trig = next_px_above(st.trigger_px, szd)
            limit = round_px(trig * (1 + s.strategy.entry_limit_offset_pct / 100), szd, "up")
            fill = sim.entry_fill_a(bars15.iloc[-1], trig, limit)
            if fill is not None and arm.get("size"):
                self.state.set_arm(st, arm.get("arm_key"), arm.get("size"))
                ps = PosState(coin, fill * (1 + s.cost.slippage), st.stop_px, arm["size"], arm["size"],
                              str(bars15.index[-1]), arm.get("arm_key") or str(bars15.index[-1]))
                self.state.positions[coin] = ps
                entry_fee = ps.size * ps.entry_px * s.cost.taker_fee
                self.state.shadow_equity -= entry_fee
                ps.realized -= entry_fee
                self.state.set_arm(ArmState(coin, "in_position"))
                self.journal.write("shadow_fill", coin=coin, px=ps.entry_px, size=ps.size, stop=ps.stop_px)
                return

        if s.strategy.entry_impl == "B" and st.phase == "confirmed" and st.trigger_px and arm.get("size"):
            if self._entry_b(coin, st, arm, bars15, c):
                return

        mkt = self.market_snapshot(coin, c, None)
        mb = self.guard.market_block(mkt)
        new_st, intent = self.strategy.step(st, ctx, bars15, blocked=blocked or not mb.ok)
        if intent is None:
            self.state.set_arm(new_st)
            return

        if intent.kind == "cancel":
            if arm.get("arm_key"):
                self.exec.cancel_entry(coin, arm["arm_key"])
            self.journal.write("entry_cancel", coin=coin, reason=intent.reason,
                               blocks=block_reasons + mb.reasons)
            self.state.set_arm(new_st, arm_key=None)
            return

        entry = next_px_above(intent.trigger_px, szd)
        stop = check_stop(entry, intent.stop_px, s.strategy)       # 框架止损护栏：插件不能绕过
        if stop is None:
            self.journal.write("plan_rejected", card={"coin": coin, "trigger": entry, "stop": intent.stop_px},
                               reasons=["stop_out_of_range"])
            if intent.kind == "modify" and arm.get("arm_key"):
                self.exec.cancel_entry(coin, arm["arm_key"])
            self.state.set_arm(ArmState(coin, "armed"), arm_key=None)
            return
        if stop != intent.stop_px:
            from dataclasses import replace as _dc_replace

            intent = _dc_replace(intent, stop_px=stop)
            new_st = _dc_replace(new_st, stop_px=stop)
        acct = self._with_pending(acct, exclude=coin)        # 已挂入场单按“若全部成交”计入风险与相关性
        rf = self.guard.corr_scaled_risk(coin, acct, self._closes_frame())
        ml_note = self.mlgate.status
        if s.ml.enabled:
            from s03.ml.features import build_features

            feats = build_features(ctx, bars15, intent.stop_px, entry,
                                   self._daily_df("BTC") if "BTC" in s.strategy.coins else None, c["funding"],
                                   extra=self.strategy.ml_features(ctx, bars15, intent.stop_px, entry))
            scale, ml_note = self.mlgate.decide(feats)
            rf *= min(1.0, scale)                     # 只能降低风险，不能放大
        plan = size_position(acct.equity, entry, intent.stop_px, c["maxLeverage"], s.risk, s.cost, risk_frac=rf)
        v = self.guard.check_entry(coin, plan.notional, plan.risk_usd, acct, now=pd.Timestamp.now(tz="UTC"))
        mb2 = self.guard.market_block(mkt, plan.notional)
        card = {"coin": coin, "intent": intent.kind, "trigger": entry, "stop": intent.stop_px,
                "size": plan.size, "notional": plan.notional, "risk_usd": plan.risk_usd,
                "stop_dist_pct": plan.stop_dist_pct, "leverage": plan.leverage,
                "liq_dist_pct": plan.liq_dist_pct, "funding": c["funding"], "ref_dev_pct": mkt.ref_deviation_pct,
                "strategy": self.strategy.name, "daily_checks": ctx.checks, "context": ctx.data,
                "risk_frac": rf, "ml": ml_note}
        too_small = rf < 0.5 * s.risk.risk_per_trade          # 相关性额度只剩零头时不交易
        if too_small or not v.ok or not mb2.ok:
            self.journal.write("plan_rejected", card=card, reasons=v.reasons + mb2.reasons + (["corr_cap"] if too_small else []))
            if intent.kind == "modify" and arm.get("arm_key"):
                self.exec.cancel_entry(coin, arm["arm_key"])
            self.state.set_arm(ArmState(coin, "armed"), arm_key=None)
            return

        self.journal.write("plan_card", card=card)
        if intent.kind == "place":
            arm_key = f"{coin}-{intent.signal_bar.isoformat()}"
            if s.runtime.mode == "live":
                lev = self.exec.set_isolated_leverage(coin, plan.leverage)
                if not lev.ok:
                    self.journal.write("alert", msg="leverage_update_failed", coin=coin)
                    return
            if s.strategy.entry_impl == "B":           # B：只记录布防，等收盘突破再 IOC 买入
                self.state.set_arm(new_st, arm_key=arm_key, size=plan.size)
                self.state.arms[coin]["risk_usd"] = plan.risk_usd
                return
            res = self.exec.place_entry(coin, arm_key, intent.trigger_px, s.strategy.entry_limit_offset_pct, plan.size,
                                        szd, intent.stop_px)
            if res.ok:
                self.state.set_arm(new_st, arm_key=arm_key, size=plan.size)
                self.state.arms[coin]["risk_usd"] = plan.risk_usd
        else:
            if s.strategy.entry_impl == "B":
                self.state.set_arm(new_st, size=plan.size)
                self.state.arms[coin]["risk_usd"] = plan.risk_usd
                return
            res = self.exec.modify_entry(coin, arm["arm_key"], intent.trigger_px, s.strategy.entry_limit_offset_pct,
                                         plan.size, szd, intent.stop_px)
            if res.ok:
                self.state.set_arm(new_st, size=plan.size)
                self.state.arms[coin]["risk_usd"] = plan.risk_usd

    def _entry_b(self, coin: str, st: ArmState, arm: dict, bars15: pd.DataFrame, c: dict) -> bool:
        """实现 B（备用）：刚收盘的 15m K 线收盘价高于前一根高点 → IOC 限价买入，成交后立即挂止损。"""
        s = self.s
        bar = bars15.iloc[-1]
        if not sim.b_signal(bar, st.trigger_px):
            return False
        limit = float(bar["close"]) * (1 + s.strategy.entry_limit_offset_pct / 100)
        if s.runtime.mode == "shadow":
            px = float(bar["close"]) * (1 + s.cost.slippage)       # 影子近似：以收盘价成交
            ps = PosState(coin, px, st.stop_px, arm["size"], arm["size"], str(bars15.index[-1]), arm["arm_key"])
            fee = ps.size * px * s.cost.taker_fee
            self.state.shadow_equity -= fee
            ps.realized -= fee
            self.state.positions[coin] = ps
            self.state.set_arm(ArmState(coin, "in_position"))
            self.journal.write("shadow_fill", coin=coin, px=px, size=ps.size, stop=ps.stop_px, impl="B")
            return True
        lev = isolated_leverage((limit - st.stop_px) / limit, c["maxLeverage"], s.risk)
        if not self.exec.set_isolated_leverage(coin, lev).ok:
            self.journal.write("alert", msg="leverage_update_failed", coin=coin)
            return False
        res = self.exec.place_entry_ioc(coin, arm["arm_key"], limit, arm["size"], c["szDecimals"])
        if res.ok and res.filled:
            self.on_entry_filled(coin, res.avg_px, res.filled_sz, arm, self.hl.asset_ctxs())
            return True
        return False

    def _with_pending(self, acct: AccountSnapshot, exclude: str) -> AccountSnapshot:
        from dataclasses import replace as dc_replace

        pos = dict(acct.open_positions)
        risk = acct.open_risk_usd
        for coin, a in self.state.arms.items():
            if coin == exclude or coin in pos or a.get("phase") != "confirmed" or not a.get("size"):
                continue
            if a.get("trigger_px") is None or a.get("stop_px") is None:
                continue
            r_usd = a.get("risk_usd") or (a["trigger_px"] - a["stop_px"]) * a["size"]
            pos[coin] = {"notional": a["trigger_px"] * a["size"], "risk_usd": r_usd, "pending": True}
            risk += r_usd
        return dc_replace(acct, open_positions=pos, open_risk_usd=risk)

    def _closes_frame(self) -> pd.DataFrame:
        return pd.DataFrame({c: self._daily_df(c)["close"] for c in self.s.strategy.coins})

    # ================================================================ 定时撤单与关机
    def manage_schedule_cancel(self) -> None:
        """无持仓且有入场挂单时，设 20 分钟后的定时撤单；有持仓时取消（避免撤掉止损单）。"""
        has_pos = bool(self.state.positions)
        has_entries = any(a.get("phase") == "confirmed" for a in self.state.arms.values())
        if has_pos:
            self.exec.schedule_cancel_in(None)
        elif has_entries:
            self.exec.schedule_cancel_in(20 * 60)

    def shutdown(self, reason: str) -> None:
        """熔断：撤全部入场单 → 市价平全部仓位 → 停止。恢复只能由人删除 KILL 文件。"""
        if getattr(self, "_shut", False):
            return
        self._shut = True
        self.journal.write("kill_switch", reason=reason)
        for coin, a in self.state.arms.items():
            if a.get("arm_key"):
                self.exec.cancel_entry(coin, a["arm_key"])
        for coin in list(self.state.positions):
            self.exec.close_all_market(coin)
        self.state.save(self.s.runtime.state_path)
