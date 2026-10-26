"""Hyperliquid 只读数据（info 端点）。不需要任何密钥。

注意：candleSnapshot 只保留最近 5,000 根 K 线；15 分钟线约 52 天。
长历史由 recorder 自行录制，或用 OKX 永续数据补充（见手册第 3 节）。
"""
from __future__ import annotations

import time

import pandas as pd
from hyperliquid.info import Info

INTERVAL_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}


class HLData:
    def __init__(self, base_url: str, timeout: float = 15.0):
        self.info = Info(base_url, skip_ws=True, timeout=timeout)
        self._meta_cache: dict | None = None

    # ---------------------------------------------------------- K 线
    def candles(self, coin: str, interval: str, start_ms: int, end_ms: int | None = None,
                closed_only: bool = True) -> pd.DataFrame:
        end_ms = end_ms or int(time.time() * 1000)
        raw = self.info.candles_snapshot(coin, interval, start_ms, end_ms)
        df = pd.DataFrame(raw)
        if df.empty:
            return df
        df = df.rename(columns={"t": "time", "o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
        for col in ("open", "high", "low", "close", "volume"):
            df[col] = df[col].astype(float)
        df["time"] = pd.to_datetime(df["time"], unit="ms", utc=True)
        df = df.set_index("time")[["open", "high", "low", "close", "volume"]].sort_index()
        df = df[~df.index.duplicated(keep="last")]
        if closed_only:
            now = pd.Timestamp.now(tz="UTC")
            df = df[df.index + pd.Timedelta(milliseconds=INTERVAL_MS[interval]) <= now]
        return df

    def recent(self, coin: str, interval: str, bars: int) -> pd.DataFrame:
        now = int(time.time() * 1000)
        return self.candles(coin, interval, now - (bars + 2) * INTERVAL_MS[interval], now)

    # ---------------------------------------------------------- 资产上下文
    def asset_ctxs(self) -> dict[str, dict]:
        """coin -> {markPx, oraclePx, funding, openInterest, dayNtlVlm, maxLeverage, szDecimals}"""
        meta, ctxs = self.info.meta_and_asset_ctxs()
        out = {}
        for u, c in zip(meta["universe"], ctxs):
            if u.get("isDelisted"):
                continue
            out[u["name"]] = {
                "markPx": float(c["markPx"]),
                "oraclePx": float(c["oraclePx"]),
                "funding": float(c["funding"]),
                "openInterest": float(c["openInterest"]),
                "dayNtlVlm": float(c["dayNtlVlm"]),
                "maxLeverage": int(u["maxLeverage"]),
                "szDecimals": int(u["szDecimals"]),
                "fetched_at": pd.Timestamp.now(tz="UTC"),
            }
        return out

    def depth_usd(self, coin: str, pct: float = 0.5) -> float:
        """±pct% 范围内的买卖深度（USD），取两侧较小者。l2Book 最多 20 档。"""
        book = self.info.l2_snapshot(coin)
        bids, asks = book["levels"]
        mid = (float(bids[0]["px"]) + float(asks[0]["px"])) / 2
        lo, hi = mid * (1 - pct / 100), mid * (1 + pct / 100)
        b = sum(float(x["px"]) * float(x["sz"]) for x in bids if float(x["px"]) >= lo)
        a = sum(float(x["px"]) * float(x["sz"]) for x in asks if float(x["px"]) <= hi)
        return min(a, b)

    # ---------------------------------------------------------- 账户（用主账户地址）
    def account(self, address: str) -> dict:
        return self.info.user_state(address)

    def open_orders(self, address: str) -> list[dict]:
        return self.info.frontend_open_orders(address)

    def rate_budget_left(self, address: str) -> int | None:
        try:
            r = self.info.user_rate_limit(address)
            return int(r["nRequestsCap"]) - int(r["nRequestsUsed"])
        except Exception:                      # 字段名以官方返回为准，取不到时返回 None
            return None
