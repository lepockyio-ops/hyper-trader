"""Vibe-Trading 安装自检：python -m fwresearch vt-check [--vt-bin DIR]

逐项检查并输出 JSON（ok = 全部通过）：
  1. vibe-trading 可执行、版本号（期望 0.1.18；不同时给出警告，不算失败）
  2. 因子库桥接：在 Vibe-Trading 的 Python 里用合成数据算 3 个因子
  3. MCP：经 mcp-guard 启动 vibe-trading-mcp，完成 initialize 握手、列工具（应只剩白名单）
  4. 拒绝：调用 trading_account，必须被代理拒绝
  5. 放行：调用 alpha_zoo（只读、不联网），必须成功
  6. 密钥过滤：子进程环境里不能出现交易所密钥类变量（由代理的 child_env 保证，这里再核对一次）
不联网（除 Vibe-Trading 自身启动时可能检查更新外），不需要大模型密钥，不写任何交易配置。
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import numpy as np
import pandas as pd

EXPECTED_VERSION = "0.1.18"


def _bin(vt_bin: str, name: str) -> str:
    return str(Path(vt_bin) / name)


class _Client:
    def __init__(self, cmd: list[str], env: dict):
        self.p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        self.q: queue.Queue = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=lambda: self.p.stderr.read(), daemon=True).start()

    def _read(self):
        for line in self.p.stdout:
            if line.strip():
                try:
                    self.q.put(json.loads(line))
                except ValueError:
                    pass

    def send(self, m: dict) -> None:
        self.p.stdin.write((json.dumps(m) + "\n").encode())
        self.p.stdin.flush()

    def wait(self, i: int, timeout: float = 180) -> dict:
        end = time.time() + timeout
        while time.time() < end:
            try:
                m = self.q.get(timeout=1)
            except queue.Empty:
                if self.p.poll() is not None:
                    raise RuntimeError(f"MCP 进程已退出，返回码 {self.p.returncode}")
                continue
            if m.get("id") == i:
                return m
        raise TimeoutError(f"等待响应 {i} 超时")

    def close(self):
        try:
            self.p.stdin.close()
            self.p.wait(timeout=20)
        except Exception:  # noqa: BLE001
            self.p.kill()


def run(vt_bin: str, timeout: float = 300) -> dict:
    checks: dict[str, dict] = {}
    py, cli, mcp = _bin(vt_bin, "python"), _bin(vt_bin, "vibe-trading"), _bin(vt_bin, "vibe-trading-mcp")
    home = tempfile.mkdtemp(prefix="vt-check-")
    base_env = {**os.environ, "VIBE_TRADING_HOME": os.environ.get("VIBE_TRADING_HOME", home)}

    # 1. 版本
    try:
        r = subprocess.run([cli, "--version"], capture_output=True, text=True, timeout=120, env=base_env)
        ver = (r.stdout + r.stderr).strip().split()[-1] if r.returncode == 0 else None
        checks["version"] = {"ok": ver is not None, "version": ver,
                             "warning": None if ver == EXPECTED_VERSION else f"框架按 {EXPECTED_VERSION} 验证，当前 {ver}"}
    except (OSError, subprocess.TimeoutExpired) as e:
        checks["version"] = {"ok": False, "error": str(e)}
        return {"ok": False, "checks": checks}

    # 2. 因子库桥接
    try:
        from fwresearch.factors.vt_bridge import compute_vt_zoo

        idx = pd.date_range("2025-01-01", periods=120, freq="D", tz="UTC")
        rng = np.random.default_rng(0)
        close = pd.DataFrame(100 * np.exp(np.cumsum(rng.normal(0, 0.02, (120, 5)), axis=0)), index=idx,
                             columns=["BTC", "ETH", "SOL", "BNB", "HYPE"])
        panel = {"open": close, "high": close * 1.01, "low": close * 0.99, "close": close, "volume": close * 0 + 1000.0}
        old = os.environ.get("VT_PYTHON")
        os.environ["VT_PYTHON"] = py
        try:
            fac, skipped = compute_vt_zoo(panel, ["qlib158"], timeout=timeout)
        finally:
            if old is None:
                os.environ.pop("VT_PYTHON", None)
            else:
                os.environ["VT_PYTHON"] = old
        checks["factor_bridge"] = {"ok": len(fac) >= 3, "computed": len(fac), "skipped": len(skipped)}
    except Exception as e:  # noqa: BLE001
        checks["factor_bridge"] = {"ok": False, "error": str(e)[:400]}

    # 3–6. MCP 经 mcp-guard
    env = {**base_env, "FREQTRADE__EXCHANGE__SECRET": "vt-check-canary", "BINANCE_API_SECRET": "vt-check-canary"}
    root = str(Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    c = _Client([sys.executable, "-m", "fwresearch", "mcp-guard", "--", mcp], env)
    try:
        c.send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "vt-check", "version": "1"}}})
        init = c.wait(1, timeout)
        checks["mcp_initialize"] = {"ok": "result" in init, "server": init.get("result", {}).get("serverInfo")}
        c.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        c.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = sorted(t["name"] for t in c.wait(2, timeout)["result"]["tools"])
        from fwresearch.mcp_guard import DEFAULT_ALLOW

        missing = sorted(set(DEFAULT_ALLOW) - set(tools))
        leaked = [t for t in tools if t.startswith(("trading_", "paper_trade", "write_file"))]
        checks["mcp_tools"] = {"ok": not leaked and len(tools) > 0, "count": len(tools), "missing_from_upstream": missing,
                               "leaked_trading_tools": leaked}
        c.send({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "trading_account", "arguments": {}}})
        r3 = c.wait(3, timeout)
        checks["mcp_block_trading"] = {"ok": "error" in r3, "message": (r3.get("error") or {}).get("message")}
        c.send({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                "params": {"name": "alpha_zoo", "arguments": {"action": "health"}}})
        r4 = c.wait(4, timeout)
        txt = ((r4.get("result") or {}).get("content") or [{}])[0].get("text", "")
        checks["mcp_allow_research"] = {"ok": "result" in r4 and not r4["result"].get("isError"), "sample": txt[:160]}
    except Exception as e:  # noqa: BLE001
        checks.setdefault("mcp_initialize", {"ok": False, "error": str(e)[:400]})
    finally:
        c.close()

    from fwresearch.mcp_guard import child_env

    leaked_env = [k for k in child_env(env) if "SECRET" in k or "FREQTRADE" in k or "BINANCE" in k]
    checks["secret_filter"] = {"ok": not leaked_env, "leaked": leaked_env}
    return {"ok": all(v.get("ok") for v in checks.values()), "vt_bin": vt_bin, "checks": checks}


def main(argv: list[str]) -> int:
    vt_bin = "/opt/s03/vt-venv/bin"
    if "--vt-bin" in argv:
        vt_bin = argv[argv.index("--vt-bin") + 1]
    elif os.environ.get("VT_PYTHON"):
        vt_bin = str(Path(os.environ["VT_PYTHON"]).parent)
    res = run(vt_bin)
    print(json.dumps(res, ensure_ascii=False, indent=1, default=str))
    return 0 if res["ok"] else 1
