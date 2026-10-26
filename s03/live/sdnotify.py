"""systemd 通知（标准库实现）：READY=1、WATCHDOG=1。没有 NOTIFY_SOCKET（如本地调试）时什么都不做。
服务文件设置 Type=notify 与 WatchdogSec 后，进程卡住超过 WatchdogSec 未发送 WATCHDOG=1，systemd 会重启它。"""
from __future__ import annotations

import os
import socket


def notify(msg: str) -> bool:
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return False
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(addr)
            s.sendall(msg.encode())
        return True
    except OSError:
        return False
