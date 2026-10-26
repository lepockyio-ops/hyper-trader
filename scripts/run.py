"""入口。

  python scripts/run.py loop              # 按 settings.toml 的 mode 运行（默认 shadow：不下单）
  python scripts/run.py once              # 只跑一次循环（调试）
  python scripts/run.py watchdog          # 看门狗（建议部署在另一台机器）
  python scripts/run.py record            # 录制 HL K 线到 var/data
  python scripts/run.py report --days 7   # 生成周报和 AI 评审提示词
  python scripts/run.py kill "原因"        # 手动熔断（恢复需人工删除 var/KILL）
  python scripts/run.py ml-approve        # 你本人批准候选模型（只应在服务器终端手动运行）
  python scripts/run.py metrics --days 7  # 复盘指标（程序计算）→ var/reports/metrics_*.json / .md
  python scripts/run.py events            # 生成事件文件（官方日程；路径取 settings.toml 的 strategy.event_file）
  python scripts/run.py alerts            # 行情监控与告警（独立进程，不需要私钥，不消耗 LLM token）
  python scripts/run.py gate              # OpenClaw 白名单代理：只有 GET /status 与 POST /pause
  python scripts/run.py pause "原因"       # 暂停新开仓（不撤止损、不平仓）
  python scripts/run.py resume            # 恢复新开仓（只能由你手动执行）
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd  # noqa: E402

from s03.config import load_settings  # noqa: E402


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    cmd = sys.argv[1]
    s = load_settings()
    if cmd in ("loop", "once"):
        from s03.live.loop import LiveLoop

        lp = LiveLoop(s)
        lp.run_forever() if cmd == "loop" else lp.cycle()
    elif cmd == "watchdog":
        from s03.live.watchdog import run

        run(s)
    elif cmd == "record":
        from hyperliquid.utils import constants

        from s03.data.hl_data import HLData
        from s03.data.recorder import record

        record(HLData(constants.MAINNET_API_URL), s.runtime.data_dir, s.strategy.coins)
    elif cmd == "report":
        from s03.journal import Journal
        from s03.report.daily import ai_review_prompt, build_report

        days = int(sys.argv[3]) if len(sys.argv) > 3 and sys.argv[2] == "--days" else 1
        md = build_report(Journal(s.runtime.journal_path), pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=days))
        os.makedirs("var/reports", exist_ok=True)
        stamp = pd.Timestamp.now(tz="UTC").strftime("%Y%m%d")
        open(f"var/reports/report_{stamp}.md", "w", encoding="utf-8").write(md)
        open(f"var/reports/ai_prompt_{stamp}.md", "w", encoding="utf-8").write(ai_review_prompt(md))
        print(md)
    elif cmd == "kill":
        from s03.risk.guard import RiskGuard

        RiskGuard(s.risk, s.strategy, s.runtime.kill_flag_path).trip_kill(" ".join(sys.argv[2:]) or "manual")
        print("已写入熔断标记；主循环下一次运行时会撤单并平仓。")
    elif cmd == "ml-approve":
        import shutil

        from s03.ml.gate import approve

        cand = s.ml.model_path + ".candidate"
        if not os.path.exists(cand):
            sys.exit("没有候选模型：先运行 scripts/ml_optimize.py --save-model")
        if input("确认已阅读 var/ml/ 下的提案，并接受重新跑 G2–G8？输入 YES：").strip() != "YES":
            sys.exit("已取消")
        shutil.copyfile(cand, s.ml.model_path)
        print("已批准，sha256 =", approve(s.ml.model_path), "；仍需你手动把 [ml] enabled 设为 true")
    elif cmd == "metrics":
        import json

        from s03.journal import Journal
        from s03.report.metrics import alert_counts, compute, from_journal, to_markdown

        days = int(sys.argv[3]) if len(sys.argv) > 3 and sys.argv[2] == "--days" else 7
        since = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=days)
        recs = [r for r in Journal(s.runtime.journal_path).read() if pd.Timestamp(r["ts"]) >= since]
        m = compute(from_journal(recs))
        m["alerts"] = alert_counts(recs)
        m["window_days"] = days
        os.makedirs("var/reports", exist_ok=True)
        stamp = pd.Timestamp.now(tz="UTC").strftime("%Y%m%d")
        open(f"var/reports/metrics_{stamp}.json", "w", encoding="utf-8").write(json.dumps(m, ensure_ascii=False, indent=1))
        md = to_markdown(m, f"S-03 复盘指标（近 {days} 天）")
        open(f"var/reports/metrics_{stamp}.md", "w", encoding="utf-8").write(md)
        print(md)
    elif cmd == "events":
        from s03.events_calendar import main as events_main

        out = s.strategy.event_file or "var/events.json"
        sys.exit(events_main(["--out", out]))
    elif cmd == "alerts":
        from s03.alerts.hl_monitor import HLMonitor

        HLMonitor(s).run_forever()
    elif cmd == "pause":
        from s03.risk.guard import RiskGuard

        RiskGuard(s.risk, s.strategy, s.runtime.kill_flag_path, s.runtime.pause_flag_path).set_pause(
            " ".join(sys.argv[2:]) or "manual", by="operator")
        print("已暂停新开仓；持仓与交易所端止损不变。")
    elif cmd == "resume":
        p = s.runtime.pause_flag_path
        if os.path.exists(p):
            os.remove(p)
            print("已恢复新开仓。")
        else:
            print("当前没有暂停。")
    elif cmd == "gate":
        from s03.alerts.gate import serve
        from s03.journal import Journal
        from s03.risk.guard import RiskGuard

        j = Journal(s.runtime.journal_path)
        g = RiskGuard(s.risk, s.strategy, s.runtime.kill_flag_path, s.runtime.pause_flag_path)

        def status():
            from s03.live.state import LiveState

            st = LiveState.load(s.runtime.state_path)
            alerts = [r for r in j.read() if r.get("kind") == "alert"][-20:]
            return {"mode": s.runtime.mode, "network": s.runtime.network, "paused": g.paused(),
                    "killed": g.killed(),
                    "positions": [{"coin": p.coin, "entry_px": p.entry_px, "stop_px": p.stop_px,
                                   "remaining_frac": round(p.remaining / p.size, 3) if p.size else None}
                                  for p in st.positions.values()],
                    "armed": {c: a.get("phase") for c, a in st.arms.items()},
                    "recent_alerts": [{k: a.get(k) for k in ("ts", "coin", "rule", "severity", "detail")}
                                      for a in alerts]}

        serve(status, lambda reason: g.set_pause(reason, by="openclaw"),
              log=lambda d: j.write("gate", **{k: v for k, v in d.items() if k != "kind"}))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
