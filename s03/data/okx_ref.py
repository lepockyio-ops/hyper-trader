"""OKX 公开行情：只读，用于交叉核对价格和补充长历史（不交易）。"""
from __future__ import annotations

import pandas as pd
import requests

BASE = "https://www.okx.com/api/v5/market"
HEADERS = {"User-Agent": "s03-research/0.5"}


def ref_price(coin: str) -> float:
    r = requests.get(f"{BASE}/ticker", params={"instId": f"{coin}-USDT-SWAP"}, headers=HEADERS, timeout=10)
    r.raise_for_status()
    return float(r.json()["data"][0]["last"])


def history(coin: str, bar: str = "15m", pages: int = 10) -> pd.DataFrame:
    """OKX 永续历史 K 线（每页 100 根，从新到旧翻页）。bar: 15m / 1H / 1Dutc 等。"""
    rows, after = [], None
    for _ in range(pages):
        params = {"instId": f"{coin}-USDT-SWAP", "bar": bar, "limit": 100}
        if after:
            params["after"] = after
        r = requests.get(f"{BASE}/history-candles", params=params, headers=HEADERS, timeout=15)
        r.raise_for_status()
        data = r.json()["data"]
        if not data:
            break
        rows += data
        after = data[-1][0]
    df = pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "vol", "volCcy", "volQuote", "confirm"])
    df["time"] = pd.to_datetime(df["time"].astype("int64"), unit="ms", utc=True)
    for c in ("open", "high", "low", "close"):
        df[c] = df[c].astype(float)
    df["volume"] = df["volCcy"].astype(float)
    df = df[df["confirm"] == "1"]
    return df.set_index("time")[["open", "high", "low", "close", "volume"]].sort_index()
