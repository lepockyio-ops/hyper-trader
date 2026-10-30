from s03.report.metrics import alert_counts, compute, from_journal, to_markdown


def test_metrics_from_journal():
    recs = [{"kind": "position_closed", "coin": "SOL", "pnl": -50.0, "r": -1.0, "ts": "2026-10-01T00:00:00Z"},
            {"kind": "position_closed", "coin": "SOL", "pnl": -48.0, "r": -0.96, "ts": "2026-10-02T00:00:00Z"},
            {"kind": "position_closed", "coin": "SOL", "pnl": 160.0, "r": 3.2, "ts": "2026-10-03T00:00:00Z"},
            {"kind": "position_closed", "coin": "BTC", "pnl": 10.0, "r": None, "ts": "2026-10-04T00:00:00Z"},
            {"kind": "alert", "coin": "BTC", "rule": "shock_1m"}, {"kind": "cycle"}]
    m = compute(from_journal(recs))
    assert m["total"]["n"] == 4 and m["by_coin"]["SOL"]["max_consecutive_losses"] == 2
    assert m["by_coin"]["SOL"]["expectancy_r"] == round((-1 - 0.96 + 3.2) / 3, 3)
    assert m["total"]["r_known"] == 3 and m["by_coin"]["SOL"]["profit_factor"] == round(160 / 98, 2)
    assert alert_counts(recs) == {"BTC:shock_1m": 1}
    assert "| SOL | 3 |" in to_markdown(m, "t")


def test_total_streak_uses_account_close_order():
    """审查 P2：合计先按币分组再拼接，真实 2 连亏被报告成 1。"""
    from s03.report.metrics import compute

    trs = [{"coin": "BTC", "close_time": "2026-01-01", "r": -1, "pnl": -1, "exit_reason": "sl"},
           {"coin": "ETH", "close_time": "2026-01-02", "r": -1, "pnl": -1, "exit_reason": "sl"},
           {"coin": "BTC", "close_time": "2026-01-03", "r": 1, "pnl": 1, "exit_reason": "tp"},
           {"coin": "ETH", "close_time": "2026-01-04", "r": 1, "pnl": 1, "exit_reason": "tp"}]
    m = compute(trs)
    assert m["total"]["max_consecutive_losses"] == 2 and m["by_coin"]["BTC"]["max_consecutive_losses"] == 1
