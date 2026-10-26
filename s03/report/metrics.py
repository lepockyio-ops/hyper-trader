"""复盘指标（程序计算，可复现）。复盘 agent 只读本模块的输出，不自己数笔数、算胜率。
Hyperliquid 版与 freqtrade 版共用同一个 compute()（freqtrade 版：monitor/review_metrics.py，由 s03-ft-export 调用）。"""
from __future__ import annotations

from collections import Counter, defaultdict


def compute(trades: list[dict]) -> dict:
    """trades: [{coin, pnl, r (可为 None), exit_reason, close_time}]，按平仓时间排序。"""
    def stats(ts: list[dict]) -> dict:
        n = len(ts)
        rs = [t["r"] for t in ts if t.get("r") is not None]
        wins = [t for t in ts if (t.get("pnl") or 0) > 0]
        streak = worst = 0
        for t in ts:
            streak = streak + 1 if (t.get("pnl") or 0) <= 0 else 0
            worst = max(worst, streak)
        gross_win = sum(t["pnl"] for t in wins)
        gross_loss = -sum(t["pnl"] for t in ts if (t.get("pnl") or 0) <= 0)
        return {
            "n": n,
            "win_rate": round(len(wins) / n, 3) if n else None,
            "expectancy_r": round(sum(rs) / len(rs), 3) if rs else None,
            "r_known": len(rs),
            "pnl": round(sum(t.get("pnl") or 0 for t in ts), 2),
            "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
            "max_consecutive_losses": worst,
            "exit_reasons": dict(Counter(t.get("exit_reason") or "unknown" for t in ts)),
        }

    by = defaultdict(list)
    for t in sorted(trades, key=lambda x: str(x.get("close_time"))):
        by[t["coin"]].append(t)
    return {"total": stats([t for v in by.values() for t in v]), "by_coin": {c: stats(v) for c, v in sorted(by.items())},
            "evidence": "已核验事实（由日志或交易记录逐笔计算）；n < 30 时任何结论都属证据不足"}


def to_markdown(m: dict, title: str) -> str:
    rows = [("合计", m["total"])] + list(m["by_coin"].items())
    lines = [f"# {title}", "", "| 标的 | 笔数 | 胜率 | 期望值（R） | 盈亏 | 盈亏比 | 最长连亏 | 离场原因 |",
             "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for name, s in rows:
        lines.append(f"| {name} | {s['n']} | {s['win_rate']} | {s['expectancy_r']} | {s['pnl']} | {s['profit_factor']} | "
                     f"{s['max_consecutive_losses']} | {s['exit_reasons']} |")
    lines += ["", m["evidence"]]
    return "\n".join(lines)


def from_journal(records: list[dict]) -> list[dict]:
    """Hyperliquid 版：从 journal 的 position_closed 记录取逐笔数据（v1.0 起记录 r）。"""
    out = []
    for r in records:
        if r.get("kind") != "position_closed":
            continue
        out.append({"coin": r.get("coin"), "pnl": float(r.get("pnl") or 0), "r": r.get("r"),
                    "exit_reason": r.get("exit_reason"), "close_time": r.get("ts")})
    return out


def alert_counts(records: list[dict]) -> dict:
    return dict(Counter(f"{r.get('coin')}:{r.get('rule')}" for r in records if r.get("kind") == "alert"))
