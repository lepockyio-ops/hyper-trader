"""想法登记簿：把“找想法”变成可追溯的流程，并如实记录尝试次数（Deflated Sharpe 需要它）。

流程（状态只能按箭头走）：
  proposed  ──▶ screening ──▶ backtesting ──▶ validated ──▶ approved（只能由人批准，写进手册第 4–5 节）
      │             │              │               │
      └─────────────┴──────────────┴───────────────┴──▶ rejected（任何阶段都可以，必须写原因）

每个想法必须写清：假设（会发生什么）、机制（为什么会发生）、证伪条件（什么结果说明它是错的）。
来源可以是 Vibe-Trading 的研究目标（通过 mcp-guard 调用）、OpenClaw 研究 agent，或你本人。
存储：一个追加写入的 JSONL 文件（默认 research/ideas.jsonl），每次变更一行，当前状态由全部记录折叠得到，
历史不可改写，便于复盘“试过多少个想法、哪些被否掉了”。
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

DEFAULT_PATH = Path(os.environ.get("FW_IDEAS_FILE", "research/ideas.jsonl"))
STATUSES = ("proposed", "screening", "backtesting", "validated", "approved", "rejected")
NEXT = {"proposed": {"screening", "rejected"}, "screening": {"backtesting", "rejected"},
        "backtesting": {"validated", "rejected", "backtesting"}, "validated": {"approved", "rejected"},
        "approved": {"rejected"}, "rejected": set()}
SOURCES = ("vibe-trading", "agent", "human")
EVIDENCE_KINDS = ("factors", "backtest", "stats", "note", "vibe-trading")


@dataclass
class Idea:
    id: str
    title: str
    hypothesis: str
    mechanism: str
    falsification: str
    source: dict
    created: str
    status: str = "proposed"
    factors: list = field(default_factory=list)
    tags: list = field(default_factory=list)
    evidence: list = field(default_factory=list)
    history: list = field(default_factory=list)
    n_backtests: int = 0


class IdeaBook:
    def __init__(self, path: str | Path = DEFAULT_PATH):
        self.path = Path(path)

    # ------------------------------------------------------------ 读写
    def _events(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(x) for x in self.path.read_text("utf-8").splitlines() if x.strip()]

    def _append(self, ev: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(ev, ensure_ascii=False, default=str) + "\n")

    def all(self) -> dict[str, Idea]:
        ideas: dict[str, Idea] = {}
        for ev in self._events():
            k = ev["kind"]
            if k == "create":
                ideas[ev["id"]] = Idea(**ev["idea"])
            elif ev["id"] in ideas:
                it = ideas[ev["id"]]
                if k == "status":
                    it.status = ev["to"]
                    if ev["to"] == "backtesting":
                        it.n_backtests += 1
                elif k == "evidence":
                    it.evidence.append(ev["evidence"])
                it.history.append({k2: ev[k2] for k2 in ev if k2 not in ("idea",)})
        return ideas

    def get(self, iid: str) -> Idea:
        ideas = self.all()
        if iid not in ideas:
            raise KeyError(f"没有这个想法：{iid}")
        return ideas[iid]

    # ------------------------------------------------------------ 操作
    def add(self, title: str, hypothesis: str, mechanism: str, falsification: str, source_kind: str,
            source_ref: str = "", factors: list[str] | None = None, tags: list[str] | None = None,
            now: pd.Timestamp | None = None) -> Idea:
        for name, val in (("title", title), ("hypothesis", hypothesis), ("mechanism", mechanism),
                          ("falsification", falsification)):
            if not val or len(val.strip()) < 4:
                raise ValueError(f"{name} 不能为空（假设、机制、证伪条件三项都必须写）")
        if source_kind not in SOURCES:
            raise ValueError(f"来源必须是 {SOURCES} 之一")
        now = now or pd.Timestamp.now(tz="UTC")
        day = f"{now:%Y%m%d}"
        n = sum(1 for i in self.all() if i.startswith(f"I-{day}-")) + 1
        idea = Idea(id=f"I-{day}-{n:03d}", title=title.strip(), hypothesis=hypothesis.strip(), mechanism=mechanism.strip(),
                    falsification=falsification.strip(), source={"kind": source_kind, "ref": source_ref},
                    created=now.isoformat(), factors=list(factors or []), tags=list(tags or []))
        self._append({"kind": "create", "id": idea.id, "at": now.isoformat(), "idea": idea.__dict__})
        return idea

    def move(self, iid: str, to: str, reason: str, by: str = "agent", approver: str | None = None) -> Idea:
        it = self.get(iid)
        if to not in STATUSES:
            raise ValueError(f"状态必须是 {STATUSES} 之一")
        if to not in NEXT[it.status]:
            raise ValueError(f"不能从 {it.status} 直接到 {to}；允许：{sorted(NEXT[it.status]) or '无（已终止）'}")
        if not reason or len(reason.strip()) < 4:
            raise ValueError("必须写明原因")
        if to == "validated" and not any(e.get("kind") == "stats" and e.get("verdict") == "通过" for e in it.evidence):
            raise ValueError("进入 validated 前必须附上一份结论为“通过”的统计检验（idea attach --kind stats）")
        if to == "approved":
            if by != "human" or not approver:
                raise ValueError("approved 只能由人批准：需要 --by human --approver <姓名>（agent 不得执行此操作）")
        self._append({"kind": "status", "id": iid, "at": pd.Timestamp.now(tz="UTC").isoformat(), "from": it.status,
                      "to": to, "reason": reason.strip(), "by": by, "approver": approver})
        return self.get(iid)

    def attach(self, iid: str, kind: str, path: str = "", summary: str = "", verdict: str | None = None) -> Idea:
        self.get(iid)
        if kind not in EVIDENCE_KINDS:
            raise ValueError(f"证据类型必须是 {EVIDENCE_KINDS} 之一")
        if kind == "stats" and path:
            verdict = verdict or _verdict_from(path)
        ev = {"kind": kind, "path": path, "summary": summary, "verdict": verdict,
              "at": pd.Timestamp.now(tz="UTC").isoformat()}
        self._append({"kind": "evidence", "id": iid, "at": ev["at"], "evidence": ev})
        return self.get(iid)

    def n_trials(self) -> int:
        """回测过的想法总次数（同一想法每进入一次 backtesting 记一次）：DSR 的尝试次数下限。"""
        return sum(i.n_backtests for i in self.all().values())

    def table(self) -> pd.DataFrame:
        rows = [{"id": i.id, "status": i.status, "title": i.title, "source": i.source.get("kind"),
                 "n_backtests": i.n_backtests, "evidence": len(i.evidence)} for i in self.all().values()]
        return pd.DataFrame(rows)


def _verdict_from(path: str) -> str | None:
    try:
        d = json.loads(Path(path).read_text("utf-8"))
    except (OSError, ValueError):
        return None
    v = d.get("verdict") or (d.get("pooled") or {}).get("verdict")
    return v if v in ("通过", "不通过", "证据不足") else None


_ID = re.compile(r"^I-\d{8}-\d{3}$")


def valid_id(s: str) -> bool:
    return bool(_ID.match(s))
