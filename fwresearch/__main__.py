"""命令行：python -m fwresearch <命令>。所有输出写到 --out-dir（默认 research/lab/），并在标准输出打印 JSON 摘要。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from fwresearch import report
from fwresearch.ideas import IdeaBook

TIME_COLS = ("time", "exit_time", "close_time", "close_date")


def _stamp() -> str:
    return pd.Timestamp.now(tz="UTC").strftime("%Y%m%dT%H%M%SZ")


def _save(out_dir: str, name: str, data: dict, md: str) -> dict:
    d = Path(out_dir)
    d.mkdir(parents=True, exist_ok=True)
    j, m = d / f"{name}.json", d / f"{name}.md"
    j.write_text(json.dumps(data, ensure_ascii=False, indent=1, default=_json_default), "utf-8")
    m.write_text(md, "utf-8")
    return {"json": str(j), "markdown": str(m)}


def _json_default(o):
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return float(o)
    if isinstance(o, (np.bool_,)):
        return bool(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    return str(o)


def _time_col(df: pd.DataFrame) -> str:
    for c in TIME_COLS:
        if c in df.columns:
            return c
    raise ValueError(f"逐笔文件缺少时间列（{', '.join(TIME_COLS)} 之一）")


# ---------------------------------------------------------------- factors
def cmd_factors(a) -> int:
    from fwresearch.data import load_panel
    from fwresearch.factors import evaluate, zoo

    panel = load_panel(a.source, a.data_dir, a.coins.split(","), a.tf, a.start, a.end)
    factors = [] if a.zoo == "vt" else zoo.available(panel, a.ids.split(",") if a.ids else None)
    extra, vt_skipped = {}, []
    if a.zoo in ("vt", "all"):
        from fwresearch.factors.vt_bridge import compute_vt_zoo

        from fwresearch.factors.vt_bridge import DERIVED_NOTE

        extra, vt_skipped = compute_vt_zoo(panel, a.vt_zoos.split(",") if a.vt_zoos else None)
    res = evaluate.evaluate(panel, factors, a.horizon, a.mode, extra=extra)
    res["skipped"] += vt_skipped
    res["source"] = {"kind": a.source, "dir": str(a.data_dir), "tf": a.tf, "zoo": a.zoo}
    if extra:
        res["vt_note"] = DERIVED_NOTE
    paths = _save(a.out_dir, f"factors-{_stamp()}", res, report.factors_md(res))
    print(json.dumps({"counts": res["counts"], "fdr_significant": res["n_fdr_significant"], "mode": res["mode"],
                      **paths}, ensure_ascii=False))
    return 0


# ---------------------------------------------------------------- stats
def cmd_stats(a) -> int:
    from fwresearch.stats import evaluate_trades

    df = pd.read_csv(a.trades)
    tc = _time_col(df)
    n_trials = max(a.n_trials, 1)
    if a.ideas and Path(a.ideas).exists():
        n_trials = max(n_trials, IdeaBook(a.ideas).n_trials())
    res = evaluate_trades(df, a.risk, n_trials, a.trial_sharpe_std, time_col=tc)
    res["input"] = str(a.trades)
    paths = _save(a.out_dir, f"stats-{_stamp()}", res, report.stats_md(res))
    print(json.dumps({"verdict": res["verdict"], "n_trades": res["n_trades"], "failed": res.get("failed"), **paths},
                     ensure_ascii=False))
    return 0


# ---------------------------------------------------------------- matrix
def load_matrix_trades(results_dir: str) -> pd.DataFrame:
    files = sorted(Path(results_dir, "trades").glob("*.csv"))
    frames = [pd.read_csv(f) for f in files if f.stat().st_size > 0]
    frames = [f for f in frames if len(f)]
    if not frames:
        raise SystemExit(f"{results_dir}/trades/ 下没有逐笔文件：先用研究矩阵 run 跑出结果")
    return pd.concat(frames, ignore_index=True)


def cmd_matrix(a) -> int:
    from fwresearch.stats import bootstrap_mean_ci, evaluate_trades, evaluate_trials, sharpe_ratio

    df = load_matrix_trades(a.results)
    tc = _time_col(df)
    stress = set(a.stress.split(",")) if a.stress else set()
    per = {v: g for v, g in df.groupby("variant") if v not in stress}
    t = evaluate_trials(per, tc)
    n_trials = t["n_trials"]
    if a.ideas and Path(a.ideas).exists():
        n_trials += max(IdeaBook(a.ideas).n_trials() - 1, 0)       # 当前想法已算在方案里，只加其余回测过的想法
    std, label = t["trial_sharpe_std"], "各方案每笔 Sharpe 的标准差"
    base = per.get(a.base)
    if base is None:
        raise SystemExit(f"没有基准方案 {a.base} 的交易")
    if std is None:
        jobs = [sharpe_ratio(g["R"]) for _, g in base.groupby("job") if len(g) >= 5]
        jobs = [x for x in jobs if np.isfinite(x)]
        std = float(np.std(jobs, ddof=1)) if len(jobs) >= 2 else None
        label = "代理值：只有 1 个方案，用基准方案各任务（标的×年份）Sharpe 的标准差（模型推断）"
    pooled = evaluate_trades(base, a.risk, n_trials, std, time_col=tc)
    res = {"base_variant": a.base, "trials_eval": t, "n_trials_used": n_trials, "trial_sharpe_std": std,
           "trial_sharpe_std_label": label, "pooled": pooled, "verdict": pooled["verdict"]}
    for v in stress:
        g = df[df["variant"] == v]
        if len(g) >= 5:
            res["stress"] = {"variant": v, "n": int(len(g)), **bootstrap_mean_ci(g["R"])}
    paths = _save(a.out_dir, f"matrix-{_stamp()}", res, report.matrix_md(res))
    print(json.dumps({"verdict": res["verdict"], "n_trials": n_trials, "pbo": t.get("pbo", {}).get("pbo"), **paths},
                     ensure_ascii=False, default=_json_default))
    return 0


# ---------------------------------------------------------------- idea
def cmd_idea(a) -> int:
    book = IdeaBook(a.file)
    if a.op == "add":
        it = book.add(a.title, a.hypothesis, a.mechanism, a.falsification, a.source, a.ref or "",
                      [x for x in (a.factors or "").split(",") if x], [x for x in (a.tags or "").split(",") if x])
        print(json.dumps({"id": it.id, "status": it.status}, ensure_ascii=False))
    elif a.op == "move":
        it = book.move(a.id, a.to, a.reason, a.by, a.approver)
        print(json.dumps({"id": it.id, "status": it.status}, ensure_ascii=False))
    elif a.op == "attach":
        it = book.attach(a.id, a.kind, a.path or "", a.summary or "")
        print(json.dumps({"id": it.id, "evidence": len(it.evidence), "last": it.evidence[-1]}, ensure_ascii=False))
    elif a.op == "show":
        print(json.dumps(book.get(a.id).__dict__, ensure_ascii=False, indent=1, default=str))
    elif a.op == "trials":
        print(json.dumps({"n_trials": book.n_trials()}))
    else:
        tb = book.table()
        print(tb.to_string(index=False) if len(tb) else "（还没有想法）")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m fwresearch", description="研究前端：找想法、筛因子、统计检验")
    sub = ap.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("factors", help="筛因子")
    f.add_argument("--source", choices=["hl", "freqtrade", "csv"], required=True)
    f.add_argument("--data-dir", required=True)
    f.add_argument("--coins", default="BTC,ETH,SOL,BNB,HYPE")
    f.add_argument("--tf", default="1d")
    f.add_argument("--horizon", type=int, default=1)
    f.add_argument("--mode", choices=["auto", "time_series", "cross_section"], default="auto")
    f.add_argument("--zoo", choices=["builtin", "vt", "all"], default="builtin")
    f.add_argument("--vt-zoos", default="qlib158,alpha101,gtja191", help="--zoo vt/all 时取哪些 Vibe-Trading 因子库")
    f.add_argument("--ids", default="", help="只评估这些内置因子（逗号分隔）")
    f.add_argument("--start")
    f.add_argument("--end")
    f.add_argument("--out-dir", default="research/lab")
    f.set_defaults(fn=cmd_factors)

    s = sub.add_parser("stats", help="对一份逐笔交易做统计检验")
    s.add_argument("--trades", required=True, help="CSV：至少含 R 列与时间列（time/exit_time/close_time/close_date）")
    s.add_argument("--risk", type=float, default=0.005, help="单笔风险占权益比例（只用于路径类检验）")
    s.add_argument("--n-trials", type=int, default=1, help="为得到这条策略一共试过多少种方案")
    s.add_argument("--trial-sharpe-std", type=float, default=None)
    s.add_argument("--ideas", default="research/ideas.jsonl")
    s.add_argument("--out-dir", default="research/lab")
    s.set_defaults(fn=cmd_stats)

    m = sub.add_parser("matrix", help="研究矩阵的多方案检验（DSR、PBO、成本压力）")
    m.add_argument("--results", default="research/results")
    m.add_argument("--base", default="base")
    m.add_argument("--stress", default="cost2x", help="压力测试方案（不计入尝试次数）")
    m.add_argument("--risk", type=float, default=0.005)
    m.add_argument("--ideas", default="research/ideas.jsonl")
    m.add_argument("--out-dir", default="research/lab")
    m.set_defaults(fn=cmd_matrix)

    i = sub.add_parser("idea", help="想法登记簿")
    i.add_argument("op", choices=["add", "list", "show", "move", "attach", "trials"])
    i.add_argument("--file", default="research/ideas.jsonl")
    i.add_argument("--id")
    i.add_argument("--title")
    i.add_argument("--hypothesis")
    i.add_argument("--mechanism")
    i.add_argument("--falsification")
    i.add_argument("--source", choices=["vibe-trading", "agent", "human"], default="agent")
    i.add_argument("--ref")
    i.add_argument("--factors")
    i.add_argument("--tags")
    i.add_argument("--to")
    i.add_argument("--reason")
    i.add_argument("--by", choices=["agent", "human"], default="agent")
    i.add_argument("--approver")
    i.add_argument("--kind")
    i.add_argument("--path")
    i.add_argument("--summary")
    i.set_defaults(fn=cmd_idea)
    return ap


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "vt-check":
        from fwresearch.vtcheck import main as vt_main

        return vt_main(argv[1:])
    if argv and argv[0] == "mcp-guard":
        from fwresearch.mcp_guard import main as guard_main

        return guard_main(argv[1:])
    a = build_parser().parse_args(argv)
    try:
        return a.fn(a)
    except (ValueError, KeyError) as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    sys.exit(main())
