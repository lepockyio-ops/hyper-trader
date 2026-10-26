"""第 14 节：机器学习策略优化（离线，只产出提案）。

  python scripts/ml_optimize.py --okx-pages 2000          # 4 个标的合并建事件集 → 净化滚动验证 → 提案
  python scripts/ml_optimize.py --okx-pages 2000 --save-model   # 验收通过时另存候选模型（仍未批准）
  python scripts/run.py ml-approve                         # 由你本人批准候选模型（记录哈希）

结果写入 var/ml/。它不会修改 config/settings.toml；开启 ml.enabled 需要你手动改配置，
并且模型必须重新通过 G2–G8。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd  # noqa: E402

from s03.config import load_settings  # noqa: E402
from s03.data import okx_ref  # noqa: E402
from s03.ml.events import build_events  # noqa: E402
from s03.ml.gate import save_bundle  # noqa: E402
from s03.ml.validate import evaluate, train_final, write_proposal  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--okx-pages", type=int, default=2000, help="15m 每页 100 根；2000 页约 5.7 年")
    ap.add_argument("--daily-pages", type=int, default=25)
    ap.add_argument("--save-model", action="store_true")
    a = ap.parse_args()

    s = load_settings()
    btc_daily = okx_ref.history("BTC", "1Dutc", a.daily_pages)
    frames = []
    for coin in s.strategy.coins:
        daily = btc_daily if coin == "BTC" else okx_ref.history(coin, "1Dutc", a.daily_pages)
        bars15 = okx_ref.history(coin, "15m", a.okx_pages)
        ev = build_events(coin, daily, bars15, s, btc_daily)
        print(f"{coin}: 15m {bars15.index[0]} → {bars15.index[-1]}，事件 {len(ev)}")
        frames.append(ev)
    events = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    os.makedirs("var/ml", exist_ok=True)
    events.to_csv("var/ml/events.csv", index=False)

    result = evaluate(events, s.ml)
    path = write_proposal(result, s.ml)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print("提案已写入：", path)

    if a.save_model and result.get("verdict") == "可提交人工审批":
        bundle = train_final(events, s.ml)
        h = save_bundle(bundle, s.ml.model_path + ".candidate")
        print("候选模型：", s.ml.model_path + ".candidate", "sha256", h)
        print("审批前请先人工阅读提案；批准用：python scripts/run.py ml-approve")


if __name__ == "__main__":
    main()
