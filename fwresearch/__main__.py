"""命令行：python -m fwresearch <命令>。所有输出写到 --out-dir（默认 research/lab/），并在标准输出打印 JSON 摘要。"""
from __future__ import annotations

import argparse
import json
import os
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
def news_factors(csv_path: str, close: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """news daily 生成的长表 → 按币种的新闻因子宽表（只适用于日线 --tf 1d）。
    对齐：日期为 t 的日线在 t+1 日 00:00 UTC 收盘，而 t 日的计数按“收到时间”统计、在同一时刻统计完毕，
    前向收益从这根 K 线的收盘价算起，所以不需要再后移（不会用到未来）。"""
    df = pd.read_csv(csv_path)
    df["date"] = pd.to_datetime(df["date"], utc=True)
    out = {}
    for col in ("n_news", "n_important", "n_risk"):
        w = df.pivot_table(index="date", columns="coin", values=col, aggfunc="sum")
        w = w.reindex(index=close.index, columns=close.columns).fillna(0.0)
        first = df["date"].min()
        w[w.index < first] = np.nan                        # 存档开始之前没有数据，记 NaN 而不是 0
        out[f"news:{col}"] = w
    return out


def _news_clients(spec: str):
    """默认 odaily,blockbeats：两家同时采集（BlockBeats 需要 BLOCKBEATS_API_KEY，缺了会记为该来源失败、守卫状态 degraded）。
    auto = Odaily + 有 Key 时 BlockBeats（只用于没有付费会员的场景）。"""
    from fwresearch import blockbeats as bb
    from fwresearch.odaily import Odaily

    names = ["odaily"] + (["blockbeats"] if os.environ.get("BLOCKBEATS_API_KEY") else []) if spec == "auto" else spec.split(",")
    return [Odaily() if n == "odaily" else bb.BlockBeats() for n in names]


def cmd_news(a) -> int:
    from datetime import datetime, timedelta, timezone

    from fwresearch import blockbeats as bb

    if a.op == "daily":
        arch = bb.Archive(a.archive)
        rows = bb.daily_counts(arch.iter_all(), a.coins.split(","), a.source or None)
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(rows, columns=["date", "coin", "n_news", "n_important", "n_risk"]).to_csv(a.out, index=False)
        print(json.dumps({"rows": len(rows), "out": a.out, "source": a.source or "all"}, ensure_ascii=False))
        return 0
    if a.op == "blocks":
        blocks, status = bb.load_blocks(a.guard_out, max_age_min=a.max_age_min)
        print(json.dumps({"status": status, "blocks": blocks}, ensure_ascii=False, indent=1))
        return 0
    if a.op == "replay":
        until = datetime.fromisoformat(a.until).replace(tzinfo=timezone.utc) if a.until else datetime.now(timezone.utc)
        since = datetime.fromisoformat(a.since).replace(tzinfo=timezone.utc) if a.since else until - timedelta(days=7)
        items = (i for i in bb.Archive(a.archive).iter_all() if not a.source or i.get("source", "blockbeats") == a.source)
        res = bb.replay(items, since, until)
        if not a.verbose:
            res["hits"] = res["hits"][-30:]
        print(json.dumps(res, ensure_ascii=False, indent=1))
        return 0
    clients = _news_clients(a.sources)
    if a.op == "check":
        res = [bb.check(c) for c in clients]
        print(json.dumps(res, ensure_ascii=False))
        return 0 if all(r["ok"] for r in res) else 1
    if a.op == "collect":
        res = bb.collect(clients, bb.Archive(a.archive), a.guard_out, a.lang)
    elif a.op == "backfill":
        since = datetime.fromisoformat(a.since or "2020-01-01").replace(tzinfo=timezone.utc)
        arch = bb.Archive(a.archive)
        res = {"ok": True, "results": [bb.backfill(c, arch, since, a.sleep, a.max_pages,
                                                   log=lambda m: print(m, file=sys.stderr, flush=True)) for c in clients]}
    else:
        res = bb.snapshot_data(bb.BlockBeats(), bb.Archive(a.archive))
    print(json.dumps(res, ensure_ascii=False))
    return 0 if res.get("ok") else 1


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
    if a.news_csv:
        if a.tf != "1d":
            raise ValueError("--news-csv 只适用于日线（--tf 1d）：新闻计数按 UTC 日汇总")
        extra.update(news_factors(a.news_csv, panel["close"]))
    res = evaluate.evaluate(panel, factors, a.horizon, a.mode, extra=extra)
    res["skipped"] += vt_skipped
    res["source"] = {"kind": a.source, "dir": str(a.data_dir), "tf": a.tf, "zoo": a.zoo}
    if a.zoo in ("vt", "all"):
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
    res = evaluate_trades(df, a.risk, n_trials, a.trial_sharpe_std, time_col=tc, holdout_start=a.holdout_start or None)
    res["input"] = str(a.trades)
    paths = _save(a.out_dir, f"stats-{_stamp()}", res, report.stats_md(res))
    print(json.dumps({"verdict": res["verdict"], "n_trades": res["n_trades"], "failed": res.get("failed"), **paths},
                     ensure_ascii=False))
    return 0


# ---------------------------------------------------------------- matrix
STRESS_VARIANTS = {"cost2x", "delay1", "funding2x"}             # 压力方案：不计入尝试次数，单独判定


def load_matrix_trades(results_dir: str) -> pd.DataFrame:
    """只读当前任务清单（research/jobs.json）里、状态为 ok、代码指纹与清单一致的任务的逐笔文件。
    v2.5 及以前读目录下全部 CSV：旧实验、失败重跑留下的旧文件会混进来。"""
    rd = Path(results_dir)
    jobs_file = rd.parent / "jobs.json"
    if not jobs_file.exists():
        raise SystemExit(f"找不到任务清单 {jobs_file}：先运行研究矩阵 plan")
    manifest = json.loads(jobs_file.read_text("utf-8"))
    jobs = manifest["jobs"] if isinstance(manifest, dict) else manifest
    want = manifest.get("fingerprint") if isinstance(manifest, dict) else None
    frames, skipped = [], []
    for j in jobs:
        meta, csv = rd / f"{j}.json", rd / "trades" / f"{j}.csv"
        try:
            m = json.loads(meta.read_text("utf-8"))
        except (OSError, ValueError):
            skipped.append(f"{j}：没有结果")
            continue
        if m.get("status") != "ok":
            skipped.append(f"{j}：状态 {m.get('status')}")
            continue
        if want and m.get("fingerprint") != want:
            skipped.append(f"{j}：代码 / 配置指纹与清单不一致（结果来自别的版本）")
            continue
        if csv.exists() and csv.stat().st_size > 0:
            f = pd.read_csv(csv)
            if len(f):
                frames.append(f)
    if skipped:
        print(json.dumps({"skipped_jobs": skipped[:50], "n_skipped": len(skipped)}, ensure_ascii=False), file=sys.stderr)
    if not frames:
        raise SystemExit(f"{results_dir}/trades/ 下没有当前清单里的有效逐笔文件：先用研究矩阵 run 跑出结果")
    return pd.concat(frames, ignore_index=True)


def cmd_matrix(a) -> int:
    from fwresearch.stats import (DEFAULT_GATES, bootstrap_mean_ci, combine_verdicts, evaluate_trades, evaluate_trials,
                                  sharpe_ratio)

    df = load_matrix_trades(a.results)
    tc = _time_col(df)
    if a.stress == "auto":                                          # 清单里跑了哪些压力方案就用哪些；cost2x 必须有
        stress = ({"cost2x"} | (set(df["variant"]) & STRESS_VARIANTS))
    else:
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
    pooled = evaluate_trades(base, a.risk, n_trials, std, time_col=tc, holdout_start=a.holdout_start or None)
    res = {"base_variant": a.base, "trials_eval": t, "n_trials_used": n_trials, "trial_sharpe_std": std,
           "trial_sharpe_std_label": label, "pooled": pooled}
    pbo = t.get("pbo", {}).get("pbo")
    parts = {"pooled": pooled["verdict"], "pbo": (pbo < DEFAULT_GATES["pbo"]) if pbo is not None else None}
    for v in stress or {"(未指定)"}:
        g = df[df["variant"] == v]
        if len(g) >= 5:
            st = bootstrap_mean_ci(g["R"])
            res.setdefault("stress_all", []).append({"variant": v, "n": int(len(g)), **st})
            res["stress"] = res["stress_all"][0]
            parts[f"stress:{v}"] = st["mean_R"] > 0                         # 成本压力下期望 R 仍为正（示例门槛）
        else:
            parts[f"stress:{v}"] = None
    verdict, failed, missing = combine_verdicts(parts)
    res.update(verdict=verdict, verdict_parts=parts, verdict_failed=failed, verdict_missing=missing,
               note="矩阵是单币 × 单年的诊断；上线证据以组合回测（research portfolio）为准")
    paths = _save(a.out_dir, f"matrix-{_stamp()}", res, report.matrix_md(res))
    print(json.dumps({"verdict": res["verdict"], "failed": failed, "missing": missing, "n_trials": n_trials,
                      "pbo": pbo, **paths}, ensure_ascii=False, default=_json_default))
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
    f.add_argument("--news-csv", default="", help="news daily 生成的按币新闻计数，作为额外候选因子（存档满 60 天以上才有意义）")
    f.add_argument("--start")
    f.add_argument("--end")
    f.add_argument("--out-dir", default="research/lab")
    f.set_defaults(fn=cmd_factors)

    s = sub.add_parser("stats", help="对一份逐笔交易做统计检验")
    s.add_argument("--trades", required=True, help="CSV：至少含 R 列与时间列（time/exit_time/close_time/close_date）")
    s.add_argument("--risk", type=float, default=0.005, help="单笔风险占权益比例（只用于路径类检验）")
    s.add_argument("--n-trials", type=int, default=1, help="为得到这条策略一共试过多少种方案")
    s.add_argument("--trial-sharpe-std", type=float, default=None)
    s.add_argument("--holdout-start", default="", help="样本外段起点（UTC 日期）；不填则判“证据不足”")
    s.add_argument("--ideas", default="research/ideas.jsonl")
    s.add_argument("--out-dir", default="research/lab")
    s.set_defaults(fn=cmd_stats)

    m = sub.add_parser("matrix", help="研究矩阵的多方案检验（DSR、PBO、成本压力）")
    m.add_argument("--results", default="research/results")
    m.add_argument("--base", default="base")
    m.add_argument("--stress", default="auto", help="压力测试方案（不计入尝试次数）；auto = 清单里跑过的压力方案，cost2x 必须有")
    m.add_argument("--risk", type=float, default=0.005)
    m.add_argument("--holdout-start", default="", help="样本外段起点（UTC 日期）；不填则判“证据不足”")
    m.add_argument("--ideas", default="research/ideas.jsonl")
    m.add_argument("--out-dir", default="research/lab")
    m.set_defaults(fn=cmd_matrix)

    n = sub.add_parser("news", help="新闻（Odaily + BlockBeats）：采集存档、事件守卫、历史回填、守卫回放、每日汇总、自检")
    n.add_argument("op", choices=["collect", "data", "daily", "blocks", "check", "backfill", "replay"])
    n.add_argument("--sources", default=os.environ.get("S03_NEWS_SOURCES", "odaily,blockbeats"),
                   help="默认 odaily,blockbeats（两家同时）；也可 odaily / blockbeats / auto（有 Key 时才加 BlockBeats）")
    n.add_argument("--source", default="", help="daily / replay 只统计一个来源（避免两家重复计数）")
    n.add_argument("--since", default="", help="backfill / replay 起点（UTC 日期，如 2020-01-01）")
    n.add_argument("--until", default="", help="replay 终点（默认现在）")
    n.add_argument("--sleep", type=float, default=1.0, help="backfill 每页间隔秒数（对方限频未知，别调太小）")
    n.add_argument("--max-pages", type=int, default=None)
    n.add_argument("--verbose", action="store_true")
    n.add_argument("--archive", default=os.environ.get("S03_NEWS_ARCHIVE", "research/news"))
    n.add_argument("--guard-out", default=os.environ.get("S03_NEWS_FILE", ""), help="写出 / 读取事件守卫文件 news_blocks.json")
    n.add_argument("--lang", default="cn")
    n.add_argument("--coins", default="BTC,ETH,SOL,BNB,HYPE")
    n.add_argument("--out", default="research/news_daily.csv")
    n.add_argument("--max-age-min", type=float, default=30.0)
    n.set_defaults(fn=cmd_news)

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
