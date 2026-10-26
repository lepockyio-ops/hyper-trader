import os
import socket
import tempfile

from s03.live.sdnotify import notify


def test_notify_sends_to_socket(monkeypatch):
    d = tempfile.mkdtemp()
    path = os.path.join(d, "n.sock")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    srv.bind(path)
    monkeypatch.setenv("NOTIFY_SOCKET", path)
    assert notify("WATCHDOG=1")
    assert srv.recv(64) == b"WATCHDOG=1"
    monkeypatch.delenv("NOTIFY_SOCKET")
    assert notify("READY=1") is False
