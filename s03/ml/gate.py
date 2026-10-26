"""实盘用的机器学习门控（默认关闭）。

边界（由代码强制）：
- 只能“跳过一笔”或“把风险降到 reduced_scale”，返回的缩放系数永远 ≤ 1，不会放大仓位。
- 模型文件不存在、哈希与批准记录不符、过期（expiration_hours）或特征离群时，一律回到无过滤（scale = 1）。
- 不改入场、止损、分批或任何风控参数。
"""
from __future__ import annotations

import hashlib
import json
import pickle
from pathlib import Path

import pandas as pd

from s03.config import MLConfig


def file_sha256(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save_bundle(bundle: dict, path: str) -> str:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_bytes(pickle.dumps(bundle))
    return file_sha256(path)


class MLGate:
    def __init__(self, cfg: MLConfig, approved_hash_path: str = "var/ml/APPROVED_SHA256"):
        self.cfg = cfg
        self.bundle = None
        self.status = "disabled"
        if not cfg.enabled:
            return
        p, h = Path(cfg.model_path), Path(approved_hash_path)
        if not p.exists() or not h.exists():
            self.status = "missing_model_or_approval"
            return
        if file_sha256(str(p)) != h.read_text().strip():
            self.status = "hash_mismatch"
            return
        self.bundle = pickle.loads(p.read_bytes())       # 只加载你本人批准、哈希一致的文件
        self.status = "loaded"

    def decide(self, features: dict, now: pd.Timestamp | None = None) -> tuple[float, str]:
        """返回 (风险缩放系数 ≤ 1, 原因)。"""
        if self.bundle is None:
            return 1.0, self.status
        now = now or pd.Timestamp.now(tz="UTC")
        if now - self.bundle["trained_at"] > pd.Timedelta(hours=self.cfg.expiration_hours):
            return 1.0, "model_expired"
        x = pd.DataFrame([features])[self.bundle["features"]].astype(float).fillna(pd.Series(self.bundle["medians"]))
        if self.bundle["outlier"].predict(x)[0] != 1:
            return 1.0, "outlier_fallback"
        p = float(self.bundle["model"].predict_proba(x)[0, 1])
        scale = 1.0 if p >= self.cfg.take_threshold else self.cfg.reduced_scale
        return min(1.0, max(0.0, scale)), f"p={p:.3f}"


def approve(model_path: str, approved_hash_path: str = "var/ml/APPROVED_SHA256") -> str:
    """由你本人在服务器上运行，记录批准的模型哈希。AI 助手 / OpenClaw 不得调用。"""
    h = file_sha256(model_path)
    Path(approved_hash_path).write_text(h)
    Path(approved_hash_path).with_suffix(".log").open("a").write(
        json.dumps({"at": str(pd.Timestamp.now(tz="UTC")), "sha256": h}) + "\n")
    return h
