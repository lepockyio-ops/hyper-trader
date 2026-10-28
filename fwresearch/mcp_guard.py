"""Vibe-Trading MCP 的白名单代理（stdio）。

OpenClaw 研究 agent ──stdio──▶ mcp_guard ──stdio──▶ vibe-trading-mcp

- tools/list 的返回只保留白名单里的工具；
- tools/call 调用白名单外的工具时，代理直接返回错误，请求不会到达 Vibe-Trading；
- 子进程只拿到白名单环境变量（不含任何交易所密钥）；每次拒绝写一行审计日志到 stderr 与 --audit 文件。

Vibe-Trading 自身没有“只读模式”开关（它暴露 82 个工具，其中有 trading_* 连接器工具与 paper_trade_step），
所以由这一层强制。白名单只含：找想法（研究目标、技能、网页 / 文档阅读、行情与宏观数据、多智能体讨论）、
筛因子（factor_analysis、alpha_zoo、alpha_bench）以及它自己的研究回测与运行结果读取。

  python -m fwresearch mcp-guard [--allow-file f] [--audit f] -- /opt/s03/vt-venv/bin/vibe-trading-mcp

MCP stdio 传输：每行一个 JSON-RPC 消息（UTF-8，行内不含换行）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

DEFAULT_ALLOW = frozenset({
    # 找想法
    "list_skills", "load_skill", "start_research_goal", "get_research_goal", "add_goal_evidence",
    "update_research_goal_status", "read_url", "read_document", "web_search", "list_swarm_presets", "run_swarm",
    "get_swarm_status", "get_market_data", "get_macro_series", "search_symbol", "pattern_recognition",
    # 筛因子
    "factor_analysis", "alpha_zoo", "alpha_bench",
    # 它自己的研究回测与结果读取（只作粗筛，上线证据仍以本框架回测为准）
    "backtest", "read_run_artifact", "list_runs", "get_run_result", "list_strategies", "query_strategies",
    "get_strategy_evidence",
})
# 永远拒绝（即使误加进白名单）：任何交易、连接器、写文件、付费执行类工具
DENY_PREFIX = ("trading_", "paper_trade", "qveris_execute", "write_file", "adapt_strategy", "refresh_strategy",
               "retry_run", "reap_stale")
ENV_KEEP_PREFIX = ("PATH", "HOME", "LANG", "LC_", "TZ", "VIBE_TRADING_", "LANGCHAIN_", "OPENROUTER_", "DEEPSEEK_",
                   "OPENAI_", "ANTHROPIC_", "GROQ_", "OLLAMA_", "GEMINI_", "DASHSCOPE_", "MOONSHOT_", "ZHIPU_",
                   "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE")
ENV_DROP_SUBSTR = ("FREQTRADE", "BINANCE", "OKX", "HYPERLIQUID", "HL_", "WALLET", "PRIVATE_KEY", "EXCHANGE",
                   "TRADING_PROFILE", "BROKER")


def allowed(name: str, allow: frozenset[str]) -> bool:
    return name in allow and not name.startswith(DENY_PREFIX)


def child_env(src: dict[str, str]) -> dict[str, str]:
    out = {}
    for k, v in src.items():
        if not k.startswith(ENV_KEEP_PREFIX):
            continue
        if any(s in k.upper() for s in ENV_DROP_SUBSTR):
            continue
        out[k] = v
    return out


def load_allow(path: str | None) -> frozenset[str]:
    if not path:
        return DEFAULT_ALLOW
    names = [x.strip() for x in Path(path).read_text("utf-8").splitlines() if x.strip() and not x.startswith("#")]
    return frozenset(n for n in names if not n.startswith(DENY_PREFIX))


class Guard:
    def __init__(self, cmd: list[str], allow: frozenset[str], audit: str | None = None,
                 stdin=None, stdout=None, env: dict | None = None):
        self.allow, self.audit = allow, audit
        self.inp = stdin or sys.stdin.buffer
        self.out = stdout or sys.stdout.buffer
        self.lock = threading.Lock()
        self.list_ids: set = set()
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     env=child_env(env if env is not None else dict(os.environ)))

    def _write(self, obj: dict) -> None:
        data = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
        with self.lock:
            self.out.write(data)
            self.out.flush()

    def _log(self, msg: dict) -> None:
        line = json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **msg}, ensure_ascii=False)
        print(line, file=sys.stderr, flush=True)
        if self.audit:
            with open(self.audit, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")

    # 客户端 → 子进程
    def pump_in(self) -> None:
        for raw in iter(self.inp.readline, b""):
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            m = msg.get("method")
            if m == "tools/call":
                name = (msg.get("params") or {}).get("name", "")
                if not allowed(name, self.allow):
                    self._log({"event": "blocked", "tool": name})
                    if "id" in msg:
                        self._write({"jsonrpc": "2.0", "id": msg["id"],
                                     "error": {"code": -32601, "message": f"s03 mcp-guard：工具 {name} 不在研究白名单内"}})
                    continue
                self._log({"event": "call", "tool": name})
            elif m == "tools/list" and "id" in msg:
                self.list_ids.add(msg["id"])
            self.proc.stdin.write(json.dumps(msg).encode("utf-8") + b"\n")
            self.proc.stdin.flush()
        try:
            self.proc.stdin.close()
        except OSError:
            pass

    # 子进程 → 客户端
    def pump_out(self) -> None:
        for raw in iter(self.proc.stdout.readline, b""):
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if msg.get("id") in self.list_ids and isinstance(msg.get("result"), dict):
                self.list_ids.discard(msg["id"])
                tools = msg["result"].get("tools") or []
                kept = [t for t in tools if allowed(t.get("name", ""), self.allow)]
                msg["result"]["tools"] = kept
                self._log({"event": "list", "offered": len(tools), "kept": len(kept)})
            self._write(msg)

    def run(self) -> int:
        t = threading.Thread(target=self.pump_out, daemon=True)
        t.start()
        self.pump_in()
        rc = self.proc.wait()
        t.join(timeout=5)
        return rc


def main(argv: list[str]) -> int:
    if "--" not in argv:
        print("用法：python -m fwresearch mcp-guard [--allow-file f] [--audit f] -- <vibe-trading-mcp 命令>", file=sys.stderr)
        return 2
    i = argv.index("--")
    opts, cmd = argv[:i], argv[i + 1:]
    allow_file = opts[opts.index("--allow-file") + 1] if "--allow-file" in opts else None
    audit = opts[opts.index("--audit") + 1] if "--audit" in opts else None
    return Guard(cmd, load_allow(allow_file), audit).run()
