"""JSONL 日志：交易计划卡、执行记录、异常、熔断。AI 助手只读这个文件。

日志里永远不写私钥、API 密钥或助记词。
"""
from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from pathlib import Path

import pandas as pd


class Journal:
    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, kind: str, **fields) -> None:
        rec = {"ts": pd.Timestamp.now(tz="UTC").isoformat(), "kind": kind}
        for k, v in fields.items():
            rec[k] = asdict(v) if is_dataclass(v) else v
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")

    def read(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text("utf-8").splitlines() if line.strip()]
