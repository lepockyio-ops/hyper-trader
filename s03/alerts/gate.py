"""OpenClaw 白名单代理（只监听本机）。OpenClaw 只能通过它做两件事：

  GET  /status   只读摘要：模式、持仓（标的、入场价、止损价、剩余比例）、暂停/熔断状态、最近告警
  POST /pause    暂停新开仓（写 PAUSE_ENTRY 标记）；不撤止损、不平仓

没有恢复、下单、平仓、改参数、熔断的接口：恢复只能由你在服务器上运行 run.py resume。
认证：Authorization: Bearer <OPENCLAW_GATE_TOKEN>（与 OpenClaw hooks token 不同，各自专用）。
摘要里不含账户地址、余额、API 钱包信息。
"""
from __future__ import annotations

import hmac
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

GATE_TOKEN_ENV = "OPENCLAW_GATE_TOKEN"


def make_handler(status_fn, pause_fn, token: str, log=print):
    class Handler(BaseHTTPRequestHandler):
        server_version = "s03-gate"

        def _auth(self) -> bool:
            got = self.headers.get("Authorization", "")
            ok = got.startswith("Bearer ") and hmac.compare_digest(got[7:], token)
            if not ok:
                self._send(401, {"error": "unauthorized"})
            return ok

        def _send(self, code: int, body: dict) -> None:
            data = json.dumps(body, ensure_ascii=False, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if not self._auth():
                return
            if self.path == "/status":
                self._send(200, status_fn())
            else:
                self._send(404, {"error": "not allowed"})

        def do_POST(self):
            if not self._auth():
                return
            if self.path != "/pause":
                self._send(404, {"error": "not allowed"})
                return
            n = min(int(self.headers.get("Content-Length") or 0), 4096)
            try:
                reason = str(json.loads(self.rfile.read(n) or b"{}").get("reason", ""))[:300]
            except (ValueError, AttributeError):
                reason = ""
            pause_fn(reason or "openclaw")
            log({"kind": "gate_pause", "reason": reason})
            self._send(200, {"paused": True, "note": "只停新开仓；恢复需人工在服务器执行 resume"})

        def do_PUT(self):
            self._send(405, {"error": "method not allowed"})

        do_DELETE = do_PATCH = do_PUT

        def log_message(self, fmt, *args):     # 不把请求头（含 token）写进日志
            return

    return Handler


def serve(status_fn, pause_fn, port: int = 8765, log=print) -> None:
    token = os.environ.get(GATE_TOKEN_ENV)
    if not token or len(token) < 24:
        raise RuntimeError(f"请设置足够长的环境变量 {GATE_TOKEN_ENV}（≥ 24 字符）")
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(status_fn, pause_fn, token, log))
    httpd.serve_forever()
