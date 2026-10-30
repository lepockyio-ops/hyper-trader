"""把检验结果写成 Markdown（给人看，也给 OpenClaw 复盘 / 研究 agent 读）。数字全部来自程序输出。"""
from __future__ import annotations

import json

from typing import Any

import pandas as pd


def _md_table(rows: list[dict], cols: list[str]) -> str:
    if not rows:
        return "（无）"
    df = pd.DataFrame(rows)
    cols = [c for c in cols if c in df.columns]
    head = "| " + " | ".join(cols) + " |\n| " + " | ".join("---" for _ in cols) + " |\n"
    body = "\n".join("| " + " | ".join("" if pd.isna(r[c]) else str(r[c]) for c in cols) + " |"
                     for _, r in df[cols].iterrows())
    return head + body


def factors_md(res: dict[str, Any], top: int = 20) -> str:
    c = res.get("counts", {})
    mode = {"time_series": "时间序列 IC（每个标的内部、按月；检验择时）",
            "cross_section": "截面 Rank IC（跨币种比较；检验在几个币之间挑强弱）"}[res["mode"]]
    lines = [
        "# 因子筛选报告（程序生成）", "",
        f"- 标的：{', '.join(res['assets'])}（{res['n_assets']} 个）；区间 {res['start'][:10]} → {res['end'][:10]}",
        f"- 口径：{mode}；前向 {res['horizon']} 根 K 线收益，不重叠采样",
        f"- 因子 {res['n_factors']} 个：alive {c.get('alive', 0)}、reversed {c.get('reversed', 0)}、dead {c.get('dead', 0)}、"
        f"样本不足 {c.get('insufficient', 0)}（IC 观测 < {res['thresholds'].get('min_ic_obs')}）；"
        f"经 BH 多重检验（FDR {res['thresholds']['fdr']}）仍显著 {res['n_fdr_significant']} 个",
        f"- 分类阈值：{res['threshold_label']}",
        *([f"- 截面样本：{res['coverage']['dates_usable']} 个日期（{res['coverage']['usable_from']} 起，≥ {res['coverage']['min_assets']} 个标的同时有数据）；"
           f"各标的首个有效日期：{', '.join(f'{k} {v}' for k, v in res['coverage']['first_valid'].items())}"]
          if res.get("coverage") else []),
        "", "## 按 |IR| 排序的前 %d 个" % top, "",
        _md_table(res["table"][:top], ["id", "theme", "ic_mean", "ir", "ic_positive_ratio", "ic_count", "t", "p_adj_bh",
                                       "category", "fdr_significant"]),
        "", "## 怎么读", "",
        "- alive 且 fdr_significant = True 才算“候选”：它只说明因子与之后收益有统计相关，不说明能赚钱。",
        "- 候选因子要写成想法（idea add），再写成策略插件，用本框架回测（含成本、止损、资金费），最后过统计检验。",
        "- reversed 表示方向相反的相关，同样需要先写出机制再用；dead 的因子不要靠调参数“救活”。",
        "- 截面口径每个时刻只有 5 个币排序，单期 IC 噪声很大；只有一两年数据时结论很容易不稳定。",
        "- 截面 IC 只说明“相对强弱”，所有币同涨同跌的部分被抵消；择时型策略请加 --mode time_series 再看一次。",
    ]
    if res.get("vt_note"):
        lines += ["", f"Vibe-Trading 因子：{res['vt_note']}。"]
    if res.get("skipped"):
        lines += ["", f"跳过 {len(res['skipped'])} 个因子（缺数据列或计算出错），详见 JSON。"]
    return "\n".join(lines) + "\n"


def stats_md(res: dict[str, Any], title: str = "统计检验报告") -> str:
    lines = [f"# {title}（程序生成）", "", f"- 交易笔数：{res['n_trades']}", f"- 结论：**{res['verdict']}**"
             + (f"（{res.get('reason')}）" if res.get("reason") else ""),
             f"- 阈值：{res['gate_label']}"]
    if res.get("failed"):
        lines.append(f"- 未通过：{', '.join(res['failed'])}")
    ch = res.get("checks", {})
    names = {"bootstrap_mean_R": "期望 R 的 Bootstrap 区间下限 > 0", "sign_flip": "符号翻转检验 p < 阈值",
             "segment_consistency": "分段一致性：期望 R > 0 的时间段占比（同一批结果切段，不是训练-测试验证）",
             "block_bootstrap": "按周汇总 R 的 Bootstrap 区间下限 > 0", "holdout": "样本外段期望 R > 0",
             "deflated_sharpe": "Deflated Sharpe（按尝试次数折扣）"}
    if ch:
        lines += ["", "| 检验 | 结果 | 关键数字 |", "| --- | --- | --- |"]
        for k, c in ch.items():
            p = {True: "通过", False: "不通过", None: "未判定"}[c.get("pass")]
            if k == "bootstrap_mean_R":
                num = f"期望 R {c['mean_R']}，{int(c['confidence'] * 100)}% 区间 [{c['ci_lower']}, {c['ci_upper']}]"
            elif k == "sign_flip":
                num = f"p = {c['p_value']}"
            elif k == "segment_consistency":
                num = (f"{c.get('profitable_windows')}/{c.get('windows_with_trades')} 个窗口为正"
                       if "error" not in c else c["error"])
            elif k == "block_bootstrap":
                num = (f"{c['blocks']} 个时间块，区间 [{c['ci_lower']}, {c['ci_upper']}]" if "error" not in c else c["error"])
            elif k == "holdout":
                num = (f"自 {c['start']} 起 {c['n']} 笔，期望 R {c['mean_R']}" if "mean_R" in c
                       else c.get("error") or c.get("note", ""))
            else:
                num = (f"DSR = {c.get('deflated_sharpe_ratio')}，尝试 {c.get('n_trials')} 次，运气门槛 Sharpe {c.get('expected_maximum_sharpe')}"
                       if "deflated_sharpe_ratio" in c else c.get("note") or c.get("error", ""))
            lines.append(f"| {names.get(k, k)} | {p} | {num} |")
    if "sharpe" in res:
        s = res["sharpe"]
        lines += ["", f"每笔 Sharpe {s['sharpe_per_trade']}，偏度 {s['skew']}，峰度 {s['kurtosis']}，"
                      f"PSR(>0) {s.get('psr_vs_0', s.get('psr_error'))}。"]
    if "monte_carlo_path" in res and "error" not in res["monte_carlo_path"]:
        m = res["monte_carlo_path"]
        lines += [f"Monte Carlo 排列（只检验回撤路径，不检验优势）：实际最大回撤 {m['actual_max_dd']}，"
                  f"随机顺序的中位数 {m['simulated_max_dd_median']}、最差 5% {m['simulated_max_dd_p5']}。"]
    if res.get("checks", {}).get("segment_consistency", {}).get("windows"):
        lines += ["", "## 分段一致性", "", _md_table(res["checks"]["segment_consistency"]["windows"],
                                                   ["window", "start", "end", "trades", "expectancy_R", "sum_R", "win_rate"])]
    lines += ["", "通过统计检验只说明“这段历史里的结果不太像运气”，不保证未来收益；上线仍须按 G0–G9 依次通过。"]
    return "\n".join(lines) + "\n"


def matrix_md(res: dict[str, Any]) -> str:
    t = res["trials_eval"]
    lines = ["# 研究矩阵统计检验（程序生成）", "",
             f"- 方案（尝试）{t['n_trials']} 个，加上想法登记簿里回测过的想法，计入 DSR 的尝试次数：{res['n_trials_used']}",
             f"- 各方案 Sharpe 离散度：{res['trial_sharpe_std']}（{res['trial_sharpe_std_label']}）", "",
             "## 各方案", "", _md_table([{"方案": k, **v} for k, v in t["trials"].items()],
                                       ["方案", "n_trades", "expectancy_R", "sharpe_per_trade"])]
    pbo = t.get("pbo", {})
    if "pbo" in pbo:
        lines += ["", f"PBO（过拟合概率）= {round(pbo['pbo'], 3)}，按 {pbo['months']} 个月、{pbo['n_splits']} 种切分；"
                      f"样本内 → 样本外的 Sharpe 斜率 {round(pbo['performance_degradation'], 3)}（负数说明越挑越差）。"]
    else:
        lines += ["", f"PBO：{pbo.get('note') or pbo.get('error')}"]
    for s in res.get("stress_all") or []:
        lines += ["", f"压力方案（{s['variant']}）：{s['n']} 笔，期望 R {s.get('mean_R')}，区间下限 {s.get('ci_lower')}。"]
    if res.get("verdict_parts"):
        lines += ["", f"**总判定：{res['verdict']}**（各部分：{json.dumps(res['verdict_parts'], ensure_ascii=False, default=str)}）",
                  f"_{res.get('note', '')}_"]
    lines += ["", stats_md(res["pooled"], f"基准方案（{res['base_variant']}）合并检验").replace("# ", "## ", 1)]
    return "\n".join(lines)
