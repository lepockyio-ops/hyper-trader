"""日报 / 周报：从日志生成 Markdown，并给 AI 助手准备只读提示词（手册第 9 节）。

AI 助手的边界：只读日志、写报告和单变量改动提案；不下单、不改参数、不解除熔断。
报告原则借鉴 Vibe-Trading：“算不出的值就明说，不编一个看似合理的数字”；
评审结构借鉴 ai-hedge-fund 的多角色方式。本模块默认不调用任何大模型。
"""
from __future__ import annotations

from collections import Counter

import pandas as pd

from s03.journal import Journal

REVIEW_ROLES = {
    "风控官": "检查是否有规则被绕过、止损未确认、熔断或额度告警，以及亏损是否超出设计。",
    "执行审计": "对比计划卡与成交：滑点、延迟、部分成交、改单次数、对账不一致。",
    "策略研究员": "结果是否落在回测分布 5%–95% 区间；按市场状态与标的分组。",
    "怀疑者": "找出最可能让本周结论失效的解释；列出证据不足之处。",
}


def build_report(journal: Journal, since: pd.Timestamp, until: pd.Timestamp | None = None) -> str:
    until = until or pd.Timestamp.now(tz="UTC")
    recs = [r for r in journal.read() if since <= pd.Timestamp(r["ts"]) <= until]
    kinds = Counter(r["kind"] for r in recs)
    closed = [r for r in recs if r["kind"] == "position_closed"]
    pnl = sum(float(r.get("pnl") or 0) for r in closed)
    alerts = [r for r in recs if r["kind"] in ("alert", "watchdog_alert", "kill_switch", "error")]
    rejects = Counter(x for r in recs if r["kind"] == "plan_rejected" for x in r.get("reasons", []))
    cycles = [r for r in recs if r["kind"] == "cycle"]
    lines = [
        f"# S03 报告 {since.date()} – {until.date()}（UTC）",
        "",
        f"- 循环次数：{len(cycles)}（应为 {int((until - since).total_seconds() // 900)} 次左右）",
        f"- 计划卡：{kinds.get('plan_card', 0)}；被拒：{kinds.get('plan_rejected', 0)}；"
        f"平仓：{len(closed)}；已实现盈亏：{pnl:.2f} USD",
        f"- 告警 / 错误 / 熔断：{len(alerts)}",
        "",
        "## 拒单原因",
        *[f"- {k}：{v}" for k, v in rejects.most_common()],
        "",
        "## 告警明细",
        *[f"- {r['ts']} {r['kind']} {r.get('msg') or r.get('reason') or r.get('error', '')}" for r in alerts[-50:]],
        "",
        "## 机器学习门控",
        *[f"- {k}：{v}" for k, v in Counter(
            (r.get("card") or {}).get("ml", "未记录") for r in recs if r["kind"] == "plan_card").most_common()],
        "",
        "## 无法计算的项目",
        "- 与回测分布的对比：需要先有 G2–G5 的回测结果文件（未提供则不填）。",
    ]
    if cycles:
        lines.insert(5, f"- 期末权益：{cycles[-1].get('equity')}")
    return "\n".join(lines)


def ai_review_prompt(report_md: str) -> str:
    roles = "\n".join(f"- {k}：{v}" for k, v in REVIEW_ROLES.items())
    return (
        "你是只读的复盘助手。下面是交易系统本期报告。请按以下角色分别写 3–5 条结论，"
        "每条标注证据标签（已核验事实 / 模型推断 / 未知），算不出的值写“未知”，不要编数字。"
        "最后最多提出 1 个单变量改动提案，写明假设和验证方法。你无权下单或修改参数。\n\n"
        f"{roles}\n\n---\n{report_md}"
    )
