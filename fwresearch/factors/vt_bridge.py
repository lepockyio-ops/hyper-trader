"""可选：调用你自己安装的 Vibe-Trading（独立虚拟环境）计算它的全量因子库（478 个，含 Qlib158 / Alpha101 / GTJA191）。

为什么用子进程：Vibe-Trading 安装后的顶层包名是 `src`、`backtest`，直接 import 会与别的项目冲突，
而且它的依赖很多、版本迭代快（0.1.x）。这里只把行情宽表交给它算因子值，算好的结果拿回本框架评估，
评估口径（前向收益、IC、分类、多重检验）始终用本框架的 evaluate.py。

  VT_PYTHON=/opt/s03/vt-venv/bin/python python -m fwresearch factors --zoo all ...

安全：子进程用 python -I 运行（不读当前目录、不读用户 site），环境变量只传白名单，不传任何交易所密钥。
数据交换用 numpy .npz（allow_pickle=False）+ JSON，不用 pickle：两边的 pandas / pyarrow 版本可以不同
（2026-10-28 实测：用 pickle 传 DataFrame 时，Vibe-Trading 环境因缺 pyarrow 无法反序列化）。
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

WORKER = Path(__file__).with_name("vt_worker.py")
ENV_KEEP = ("PATH", "HOME", "LANG", "LC_ALL", "TZ", "VIBE_TRADING_HOME")


def vt_python() -> str | None:
    p = os.environ.get("VT_PYTHON")
    return p if p and Path(p).exists() else None


def write_frames(path: Path, frames: dict[str, pd.DataFrame]) -> None:
    """宽表 → npz：每张表存 values（float64）、index（UTC 纳秒整数）、columns（JSON 字符串）。"""
    arrays = {}
    for k, df in frames.items():
        idx = pd.DatetimeIndex(df.index)
        idx = idx.tz_convert("UTC") if idx.tz is not None else idx.tz_localize("UTC")
        arrays[f"{k}__v"] = df.to_numpy(dtype="float64")
        arrays[f"{k}__i"] = idx.as_unit("ns").asi8
        arrays[f"{k}__c"] = np.array(json.dumps([str(c) for c in df.columns]))
    np.savez(path, **arrays)


def read_frames(path: Path) -> dict[str, pd.DataFrame]:
    out = {}
    with np.load(path, allow_pickle=False) as z:
        keys = sorted({k.rsplit("__", 1)[0] for k in z.files})
        for k in keys:
            out[k] = pd.DataFrame(z[f"{k}__v"], index=pd.to_datetime(z[f"{k}__i"], utc=True),
                                  columns=json.loads(str(z[f"{k}__c"])))
    return out


DERIVED_NOTE = ("vwap 用典型价 (high+low+close)/3 近似、amount 用 volume×close 近似（模型推断：K 线数据没有逐笔成交额）；"
                "依赖这两列的 Vibe-Trading 因子是近似值")


def with_derived(panel: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """Vibe-Trading 的 alpha101 / gtja191 有 83 个因子要求 vwap 或 amount 列（2026-10-28 实测），K 线里没有，按近似补上。"""
    out = {k: v for k, v in panel.items() if k in ("open", "high", "low", "close", "volume")}
    if "vwap" not in panel:
        out["vwap"] = (panel["high"] + panel["low"] + panel["close"]) / 3
    if "amount" not in panel:
        out["amount"] = panel["volume"] * panel["close"]
    return out


def compute_vt_zoo(panel: dict[str, pd.DataFrame], zoos: list[str] | None = None, timeout: int = 1800,
                   python: str | None = None) -> tuple[dict[str, pd.DataFrame], list[dict]]:
    py = python or vt_python()
    if not py:
        raise RuntimeError("未找到 Vibe-Trading 的 Python：请设置 VT_PYTHON=<vibe-trading 虚拟环境>/bin/python")
    env = {k: v for k, v in os.environ.items() if k in ENV_KEEP}
    with tempfile.TemporaryDirectory() as d:
        src, dst, skp = Path(d) / "panel.npz", Path(d) / "out.npz", Path(d) / "skipped.json"
        write_frames(src, with_derived(panel))
        cmd = [py, "-I", str(WORKER), str(src), str(dst), str(skp), json.dumps(zoos or [])]
        r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout, cwd=d)
        if r.returncode != 0:
            raise RuntimeError(f"Vibe-Trading 因子计算失败：{r.stderr[-1500:]}")
        factors = read_frames(dst) if dst.exists() else {}
        skipped = json.loads(skp.read_text("utf-8")) if skp.exists() else []
    return {f"vt:{k}": v for k, v in factors.items()}, skipped
