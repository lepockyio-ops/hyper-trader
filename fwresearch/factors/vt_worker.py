"""在 Vibe-Trading 的虚拟环境里运行：
  python -I vt_worker.py panel.npz out.npz skipped.json '["qlib158"]'
只调用它的因子注册表计算因子值；不加载 agent、不联网、不碰任何连接器。
数据交换用 npz（allow_pickle=False）+ JSON，格式与 vt_bridge.write_frames / read_frames 一致。"""
import json
import sys

import numpy as np
import pandas as pd


def read_frames(path):
    out = {}
    with np.load(path, allow_pickle=False) as z:
        for k in sorted({f.rsplit("__", 1)[0] for f in z.files}):
            out[k] = pd.DataFrame(z[f"{k}__v"], index=pd.to_datetime(z[f"{k}__i"], utc=True),
                                  columns=json.loads(str(z[f"{k}__c"])))
    return out


def write_frames(path, frames):
    arrays = {}
    for k, df in frames.items():
        idx = pd.DatetimeIndex(df.index)
        idx = idx.tz_convert("UTC") if idx.tz is not None else idx.tz_localize("UTC")
        arrays[f"{k}__v"] = df.to_numpy(dtype="float64")
        arrays[f"{k}__i"] = idx.as_unit("ns").asi8
        arrays[f"{k}__c"] = np.array(json.dumps([str(c) for c in df.columns]))
    np.savez(path, **arrays)


def main() -> int:
    src, dst, skp, zoos = sys.argv[1], sys.argv[2], sys.argv[3], json.loads(sys.argv[4])
    from src.factors.registry import get_default_registry   # Vibe-Trading 安装后的顶层包名是 src

    panel = read_frames(src)
    reg = get_default_registry()
    ids = [i for z in zoos for i in reg.list(zoo=z)] if zoos else reg.list()
    factors, skipped = {}, []
    for aid in ids:
        try:
            df = reg.compute(aid, panel)
            factors[aid] = df.reindex(index=panel["close"].index, columns=panel["close"].columns).astype("float64")
        except Exception as e:  # noqa: BLE001  缺列（如基本面、vwap、行业）等一律跳过并记录
            skipped.append({"id": aid, "reason": f"{type(e).__name__}: {str(e)[:200]}"})
    write_frames(dst, factors)
    with open(skp, "w", encoding="utf-8") as fh:
        json.dump(skipped, fh, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
