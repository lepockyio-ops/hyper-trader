"""告警核心（纯计算，不联网、不调用 LLM）。Hyperliquid 版与 freqtrade 版共用同一份文件（freqtrade 版：monitor/alerts_core.py）。

分工（手册“推荐分工”）：
  1. 策略信号     程序，每根 15 分钟 K 线收盘          —— 不在本模块
  2. 实时监控     程序，WebSocket 行情                  —— feed 把行情喂给 AlertEngine
  3. 告警判断     程序（本模块）：阈值、冷却、去重、限流
  4. 告警分析     OpenClaw，只在告警时（openclaw.py 发送）
  5. 人工决定     你

所有阈值都是示例值，需用回测与 dry-run / 影子模式数据校准。
本模块只产生 Alert，不下单、不改止损、不改参数。
"""
from __future__ import annotations

import hashlib
import math
import time
from collections import deque
from dataclasses import asdict, dataclass, field

SEV_INFO, SEV_WARN, SEV_CRIT = "info", "warning", "critical"


@dataclass(frozen=True)
class AlertConfig:
    # 波动类（示例值）
    shock_sigma: float = 4.0              # |1 分钟收益| > k × 近 24 小时 1 分钟收益标准差
    shock_window: int = 1440              # 1 分钟样本数（24 小时）
    shock_min_samples: int = 240          # 样本不足 4 小时不判断
    range15_atr_mult: float = 2.0         # 15 分钟振幅 > k × ATR(15m, 14)
    atr_period: int = 14
    # 持仓类
    near_stop_r: float = 0.3              # 价格距离止损 < 0.3R
    stop_missing_grace_s: int = 20        # 有持仓但交易所端无止损，持续超过 20 秒才告警
    # 资金费率与参考价
    funding_pctile: float = 0.95          # 超过近 30 天历史的 95% 分位
    funding_abs_floor: float = 0.0001     # 且绝对值 ≥ 0.01%/期（避免低波动期误报）
    ref_dev_pct: float = 0.3              # 与参考交易所价差 > 0.3%
    # 数据流
    feed_stale_s: int = 30                # 行情超过 30 秒无更新
    # 限流
    cooldown_s: int = 15 * 60             # 同一标的、同一规则 15 分钟内只告警一次
    crit_cooldown_s: int = 5 * 60
    max_llm_per_hour: int = 6             # 每小时最多转给 OpenClaw 6 次；critical 不占额度
    rules_enabled: tuple[str, ...] = ("shock_1m", "range_15m", "near_stop", "stop_missing",
                                      "funding_extreme", "ref_deviation", "feed_stale")


@dataclass
class Alert:
    coin: str
    rule: str
    severity: str
    value: float | None
    threshold: float | None
    ts: float
    detail: str
    context: dict = field(default_factory=dict)

    @property
    def id(self) -> str:
        """确定性 id：同一标的、同一规则、同一分钟只算一次。用作 OpenClaw 的 Idempotency-Key。"""
        raw = f"{self.coin}|{self.rule}|{int(self.ts // 60)}"
        return hashlib.sha256(raw.encode()).hexdigest()[:24]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["id"] = self.id
        return d


@dataclass
class PositionView:
    """持仓的只读视图（来自 HL 状态文件或 freqtrade REST API）。不含账户地址与余额。"""
    coin: str
    entry_px: float
    initial_stop_px: float
    current_stop_px: float
    has_exchange_stop: bool
    since_ts: float                        # 入场成交时间（秒）


class _CoinStats:
    def __init__(self, cfg: AlertConfig):
        self.rets: deque[float] = deque(maxlen=cfg.shock_window)
        self.last_close_1m: float | None = None
        self.last_1m_open_ts: float | None = None
        self.tr15: deque[float] = deque(maxlen=cfg.atr_period)
        self.prev_close_15m: float | None = None
        self.funding_hist: deque[float] = deque(maxlen=24 * 30 * 3)   # 足够容纳 30 天（每小时一期）
        self.last_price: float | None = None
        self.last_update: float | None = None
        self.stop_missing_since: float | None = None


def _std(xs) -> float:
    n = len(xs)
    if n < 2:
        return float("nan")
    m = sum(xs) / n
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))


def _quantile(xs, q: float) -> float:
    s = sorted(xs)
    if not s:
        return float("nan")
    pos = (len(s) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


class AlertEngine:
    """喂入行情事件，返回本次产生的告警（已经过冷却与去重）。线程不安全：由单个 feed 线程调用。"""

    def __init__(self, cfg: AlertConfig | None = None, clock=time.time):
        self.cfg = cfg or AlertConfig()
        self.clock = clock
        self.stats: dict[str, _CoinStats] = {}
        self._last_fire: dict[tuple[str, str], float] = {}
        self._llm_times: deque[float] = deque()

    def _st(self, coin: str) -> _CoinStats:
        if coin not in self.stats:
            self.stats[coin] = _CoinStats(self.cfg)
        return self.stats[coin]

    def _on(self, rule: str) -> bool:
        return rule in self.cfg.rules_enabled

    # ------------------------------------------------------------------ 行情输入
    def on_price(self, coin: str, price: float, ts: float | None = None) -> list[Alert]:
        st = self._st(coin)
        st.last_price, st.last_update = price, ts if ts is not None else self.clock()
        return []

    def on_bar_1m(self, coin: str, open_ts: float, close: float) -> list[Alert]:
        """已收盘的 1 分钟 K 线。open_ts 秒。重复推送同一根会被忽略。"""
        st = self._st(coin)
        if st.last_1m_open_ts is not None and open_ts <= st.last_1m_open_ts:
            return []
        out: list[Alert] = []
        if st.last_close_1m:
            r = close / st.last_close_1m - 1
            sigma = _std(st.rets)
            if (self._on("shock_1m") and len(st.rets) >= self.cfg.shock_min_samples and sigma > 0
                    and abs(r) > self.cfg.shock_sigma * sigma):
                out += self._emit(Alert(coin, "shock_1m", SEV_WARN, round(r * 100, 4),
                                        round(self.cfg.shock_sigma * sigma * 100, 4), open_ts + 60,
                                        f"1 分钟涨跌 {r:+.2%}，超过 {self.cfg.shock_sigma:g} 倍 24 小时标准差",
                                        {"sigma_pct": round(sigma * 100, 4)}))
            st.rets.append(r)
        st.last_close_1m, st.last_1m_open_ts = close, open_ts
        st.last_price, st.last_update = close, open_ts + 60
        return out

    def on_bar_15m(self, coin: str, open_ts: float, high: float, low: float, close: float) -> list[Alert]:
        """已收盘的 15 分钟 K 线。ATR 用此前 14 根计算，不含当前这根。"""
        st = self._st(coin)
        out: list[Alert] = []
        if self._on("range_15m") and len(st.tr15) == self.cfg.atr_period:
            atr = sum(st.tr15) / len(st.tr15)
            rng = high - low
            if atr > 0 and rng > self.cfg.range15_atr_mult * atr:
                out += self._emit(Alert(coin, "range_15m", SEV_WARN, round(rng / atr, 3),
                                        self.cfg.range15_atr_mult, open_ts + 900,
                                        f"15 分钟振幅为 ATR 的 {rng / atr:.1f} 倍", {"atr": atr, "range": rng}))
        pc = st.prev_close_15m
        tr = max(high - low, abs(high - pc), abs(low - pc)) if pc is not None else high - low
        st.tr15.append(tr)
        st.prev_close_15m = close
        return out

    def on_funding(self, coin: str, rate: float, ts: float | None = None) -> list[Alert]:
        """每期资金费率（HL 每小时一期，Binance 每 8 小时一期；同一交易所内口径一致即可）。"""
        st = self._st(coin)
        out: list[Alert] = []
        hist = st.funding_hist
        if self._on("funding_extreme") and len(hist) >= 48:
            q = _quantile([abs(x) for x in hist], self.cfg.funding_pctile)
            if abs(rate) > max(q, self.cfg.funding_abs_floor):
                out += self._emit(Alert(coin, "funding_extreme", SEV_INFO, rate, max(q, self.cfg.funding_abs_floor),
                                        ts if ts is not None else self.clock(), f"资金费率 {rate:+.4%} 高于近 30 天 {self.cfg.funding_pctile:.0%} 分位"))
        hist.append(rate)
        return out

    def seed_funding(self, coin: str, rates) -> None:
        """启动时用历史资金费率填充，避免前 48 期无法判断。"""
        for r in rates:
            self._st(coin).funding_hist.append(float(r))

    def on_ref_price(self, coin: str, ref_px: float, ts: float | None = None) -> list[Alert]:
        st = self._st(coin)
        if not self._on("ref_deviation") or not st.last_price or not ref_px:
            return []
        dev = (st.last_price / ref_px - 1) * 100
        if abs(dev) > self.cfg.ref_dev_pct:
            return self._emit(Alert(coin, "ref_deviation", SEV_WARN, round(dev, 4), self.cfg.ref_dev_pct,
                                    ts if ts is not None else self.clock(), f"与参考交易所价差 {dev:+.2f}%", {"ref_px": ref_px,
                                                                                    "px": st.last_price}))
        return []

    # ------------------------------------------------------------------ 持仓与数据流（定时调用，建议每 5–10 秒）
    def check_positions(self, positions: list[PositionView], now: float | None = None) -> list[Alert]:
        now = now if now is not None else self.clock()
        out: list[Alert] = []
        held = {p.coin for p in positions}
        for coin, st in self.stats.items():
            if coin not in held:
                st.stop_missing_since = None
        for p in positions:
            st = self._st(p.coin)
            if self._on("stop_missing"):
                if not p.has_exchange_stop:
                    if st.stop_missing_since is None:
                        st.stop_missing_since = now
                    if now - st.stop_missing_since >= self.cfg.stop_missing_grace_s:
                        out += self._emit(Alert(p.coin, "stop_missing", SEV_CRIT, now - st.stop_missing_since,
                                                self.cfg.stop_missing_grace_s, now,
                                                "有持仓但交易所端没有止损单（程序的风控层应已在处理）"))
                else:
                    st.stop_missing_since = None
            r_unit = p.entry_px - p.initial_stop_px
            if self._on("near_stop") and st.last_price and r_unit > 0:
                dist_r = (st.last_price - p.current_stop_px) / r_unit
                if 0 <= dist_r < self.cfg.near_stop_r:
                    out += self._emit(Alert(p.coin, "near_stop", SEV_INFO, round(dist_r, 3), self.cfg.near_stop_r, now,
                                            f"价格距离止损 {dist_r:.2f}R", {"stop_px": p.current_stop_px,
                                                                         "px": st.last_price}))
        return out

    def check_feed(self, coins, now: float | None = None) -> list[Alert]:
        now = now if now is not None else self.clock()
        out: list[Alert] = []
        if not self._on("feed_stale"):
            return out
        for c in coins:
            lu = self._st(c).last_update
            if lu is None or now - lu > self.cfg.feed_stale_s:
                age = None if lu is None else round(now - lu, 1)
                out += self._emit(Alert(c, "feed_stale", SEV_WARN, age, self.cfg.feed_stale_s, now,
                                        "行情数据中断" if lu else "尚未收到行情"))
        return out

    # ------------------------------------------------------------------ 冷却、去重、限流
    def _emit(self, a: Alert) -> list[Alert]:
        key = (a.coin, a.rule)
        cd = self.cfg.crit_cooldown_s if a.severity == SEV_CRIT else self.cfg.cooldown_s
        last = self._last_fire.get(key)
        if last is not None and a.ts - last < cd:
            return []
        self._last_fire[key] = a.ts
        return [a]

    def allow_llm(self, a: Alert, now: float | None = None) -> bool:
        """是否把这条告警转给 OpenClaw。critical 总是转发（不占额度）；其余每小时最多 max_llm_per_hour 次。
        超出时只写日志，不调用 LLM。"""
        if a.severity == SEV_CRIT:
            return True
        now = now if now is not None else self.clock()
        while self._llm_times and now - self._llm_times[0] > 3600:
            self._llm_times.popleft()
        if len(self._llm_times) >= self.cfg.max_llm_per_hour:
            return False
        self._llm_times.append(now)
        return True


class BarCloser:
    """把交易所推送的“进行中 K 线”转成“已收盘 K 线”：同一 key 的开盘时间前进时，返回上一根。
    适用于 Hyperliquid candle 订阅（每次推送当前 K 线快照）。Binance 推送自带 x=true 收盘标记，可不用它。"""

    def __init__(self):
        self.cur: dict[tuple[str, str], dict] = {}

    def push(self, coin: str, interval: str, bar: dict) -> dict | None:
        """bar 需含 t（开盘时间，毫秒）、o、h、l、c（数值）。返回已收盘的上一根或 None。"""
        key = (coin, interval)
        prev = self.cur.get(key)
        self.cur[key] = bar
        if prev is not None and bar["t"] > prev["t"]:
            return prev
        return None
