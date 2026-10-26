"""告警模块测试：阈值、冷却、限流、K 线收盘判断、OpenClaw 请求格式、白名单代理。"""
from __future__ import annotations

import json
import threading
import urllib.request

import pytest

from s03.alerts.core import SEV_CRIT, AlertConfig, AlertEngine, BarCloser, PositionView
from s03.alerts.openclaw import OpenClawConfig, OpenClawNotifier, build_request, check_url


def _warm(e, coin="BTC", n=300, px=100.0):
    for i in range(n):
        px *= 1 + (0.0005 if i % 2 else -0.0005)
        e.on_bar_1m(coin, i * 60, px)
    return px


def test_shock_fires_once_then_cooldown():
    e = AlertEngine(AlertConfig())
    px = _warm(e)
    a = e.on_bar_1m("BTC", 300 * 60, px * 1.01)
    assert [x.rule for x in a] == ["shock_1m"]
    b = e.on_bar_1m("BTC", 301 * 60, px * 1.02 * 1.01)          # 冷却期内
    assert b == []
    c = e.on_bar_1m("BTC", 320 * 60, px * 1.02 * 1.01 * 1.01)   # 15 分钟后
    assert [x.rule for x in c] == ["shock_1m"]


def test_shock_needs_min_samples_and_ignores_duplicate_bars():
    e = AlertEngine(AlertConfig())
    _warm(e, n=100)
    assert e.on_bar_1m("BTC", 100 * 60, 120.0) == []            # 样本不足 240
    assert e.on_bar_1m("BTC", 100 * 60, 150.0) == []            # 同一根重复推送被忽略


def test_range_15m_uses_prior_atr_only():
    e = AlertEngine(AlertConfig())
    for i in range(14):
        assert e.on_bar_15m("ETH", i * 900, 101, 99, 100) == []
    a = e.on_bar_15m("ETH", 14 * 900, 105, 99, 104)
    assert a and a[0].rule == "range_15m" and a[0].value == pytest.approx(3.0)


def test_near_stop_and_stop_missing_grace():
    e = AlertEngine(AlertConfig())
    e.on_price("SOL", 97.5, ts=0)
    p = PositionView("SOL", 100.0, 97.2, 97.2, has_exchange_stop=False, since_ts=0)
    a = e.check_positions([p], now=0)
    assert [x.rule for x in a] == ["near_stop"]                 # (97.5-97.2)/2.8 = 0.107R
    assert e.check_positions([p], now=10) == []                  # 无止损未满 20 秒
    b = e.check_positions([p], now=25)
    assert [x.rule for x in b] == ["stop_missing"] and b[0].severity == SEV_CRIT
    p2 = PositionView("SOL", 100.0, 97.2, 97.2, has_exchange_stop=True, since_ts=0)
    assert all(x.rule != "stop_missing" for x in e.check_positions([p2], now=400))


def test_funding_percentile_and_floor():
    e = AlertEngine(AlertConfig())
    e.seed_funding("BNB", [0.00001] * 200)
    assert e.on_funding("BNB", 0.00005, ts=0) == []              # 高于分位但低于绝对下限
    a = e.on_funding("BNB", 0.0003, ts=1000)
    assert a and a[0].rule == "funding_extreme"


def test_ref_deviation_and_feed_stale():
    e = AlertEngine(AlertConfig())
    e.on_price("BTC", 100.5, ts=0)
    assert e.on_ref_price("BTC", 100.0, ts=0)[0].rule == "ref_deviation"
    assert e.check_feed(["BTC"], now=20) == []
    assert e.check_feed(["BTC"], now=40)[0].rule == "feed_stale"


def test_llm_hourly_cap_but_critical_always_passes():
    e = AlertEngine(AlertConfig(max_llm_per_hour=2))
    e.on_price("BTC", 1, ts=0)
    alerts = [e.on_ref_price("BTC", 2, ts=t)[0] for t in (0, 1000, 2000)]
    assert [e.allow_llm(a, now=a.ts) for a in alerts] == [True, True, False]
    crit = e.check_positions([PositionView("BTC", 1, 0.5, 0.5, False, 0)], now=3000)
    crit = [a for a in crit if a.severity == SEV_CRIT] or e.check_positions(
        [PositionView("BTC", 1, 0.5, 0.5, False, 0)], now=3030)
    assert crit and e.allow_llm(crit[0], now=3030)


def test_bar_closer():
    c = BarCloser()
    assert c.push("BTC", "1m", {"t": 0, "o": 1, "h": 1, "l": 1, "c": 1}) is None
    assert c.push("BTC", "1m", {"t": 0, "o": 1, "h": 2, "l": 1, "c": 2}) is None
    closed = c.push("BTC", "1m", {"t": 60_000, "o": 2, "h": 2, "l": 2, "c": 2})
    assert closed["c"] == 2 and closed["t"] == 0


def test_openclaw_request_shape_and_no_secrets(monkeypatch):
    e = AlertEngine(AlertConfig())
    e.on_price("ETH", 101, ts=1_800_000_000)
    a = e.on_ref_price("ETH", 100, ts=1_800_000_000)[0]
    payload, headers = build_request(a, OpenClawConfig(), "tok-123", {"last_px": 101})
    assert set(payload) >= {"message", "name", "agentId", "sessionMode", "deliver", "timeoutSeconds"}
    assert payload["sessionMode"] == "isolated" and headers["Authorization"] == "Bearer tok-123"
    assert headers["Idempotency-Key"] == a.id and "tok-123" not in payload["message"]
    monkeypatch.setenv("HL_API_WALLET_KEY", "0xSECRET")
    assert "0xSECRET" not in payload["message"] and "暂停新开仓" in payload["message"]


def test_openclaw_url_policy():
    check_url("http://127.0.0.1:18789/hooks/agent")
    check_url("https://claw.example.ts.net/hooks/agent")
    with pytest.raises(ValueError):
        check_url("http://203.0.113.5:18789/hooks/agent")


class _Resp:
    def __init__(self, code, headers=None):
        self.status_code, self.headers = code, headers or {}


class _Sess:
    def __init__(self, codes):
        self.codes, self.calls = list(codes), []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append((url, json, headers))
        return _Resp(self.codes.pop(0), {"Retry-After": "0"})


def test_notifier_retries_503_but_not_401(monkeypatch):
    monkeypatch.setenv("OPENCLAW_HOOKS_TOKEN", "t" * 32)
    e = AlertEngine(AlertConfig())
    e.on_price("BTC", 101, ts=0)
    a = e.on_ref_price("BTC", 100, ts=0)[0]
    s = _Sess([503, 200])
    n = OpenClawNotifier(OpenClawConfig(enabled=True), log=lambda d: None, session=s)
    assert n.post(a) == 200 and len(s.calls) == 2
    s2 = _Sess([401])
    n2 = OpenClawNotifier(OpenClawConfig(enabled=True), log=lambda d: None, session=s2)
    assert n2.post(a) == 401 and len(s2.calls) == 1


def test_gate_only_allows_status_and_pause(monkeypatch):
    from http.server import ThreadingHTTPServer

    from s03.alerts.gate import make_handler

    paused = []
    tok = "g" * 32
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(lambda: {"ok": 1}, paused.append, tok, log=lambda d: None))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    def req(method, path, auth=True, body=None):
        r = urllib.request.Request(base + path, method=method, data=json.dumps(body).encode() if body else None,
                                   headers={"Authorization": f"Bearer {tok}"} if auth else {})
        try:
            with urllib.request.urlopen(r) as resp:
                return resp.status
        except urllib.error.HTTPError as err:
            return err.code

    assert req("GET", "/status") == 200
    assert req("GET", "/status", auth=False) == 401
    assert req("POST", "/pause", body={"reason": "test"}) == 200 and paused == ["test"]
    assert req("POST", "/resume") == 404
    assert req("POST", "/forceexit") == 404
    assert req("DELETE", "/pause") == 405
    srv.shutdown()


def test_guard_pause_blocks_new_entries(tmp_path):
    import pandas as pd

    from s03.config import RiskConfig, StrategyConfig
    from s03.risk.guard import AccountSnapshot, RiskGuard

    g = RiskGuard(RiskConfig(), StrategyConfig(), str(tmp_path / "KILL"))
    acct = AccountSnapshot(equity=10_000, day_start_equity=10_000, week_start_equity=10_000, open_positions={},
                           open_risk_usd=0.0, consecutive_losses=0, last_loss_time=None, rate_budget_left=None)
    assert g.global_block(acct, pd.Timestamp.now(tz="UTC")).ok
    g.set_pause("test", by="openclaw")
    v = g.global_block(acct, pd.Timestamp.now(tz="UTC"))
    assert not v.ok and "paused_by_operator" in v.reasons and not g.killed()
