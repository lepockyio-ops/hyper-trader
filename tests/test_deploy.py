"""部署文件检查：systemd、sudoers、OpenClaw 命令白名单、agent 配置。运行：pytest -q tests/test_deploy.py"""
from __future__ import annotations

import configparser
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

D = Path(__file__).resolve().parents[1] / "deploy"
UNITS = D / "systemd"


def _unit(name: str) -> configparser.ConfigParser:
    cp = configparser.ConfigParser(strict=False, interpolation=None)
    cp.optionxform = str
    cp.read(UNITS / name, encoding="utf-8")
    return cp


def _allowed(agent: str, binary: str, args: str) -> bool:
    d = json.loads((D / "openclaw" / "exec-approvals.s03.json").read_text("utf-8"))
    a = d["agents"][agent]
    if a["security"] != "allowlist":
        return False
    return any(e["pattern"] == binary and re.search(e["argPattern"], args) for e in a["allowlist"])


def test_services_run_as_trader_with_hardening():
    for f in UNITS.glob("s03-*.service"):
        if f.name.startswith(("s03-wake", "s03-notify", "s03-heartbeat")):  # 这些用 DynamicUser，只发通知
            continue
        s = _unit(f.name)["Service"]
        assert s["User"] == "s03trader" and s["NoNewPrivileges"] == "yes" and s["ProtectSystem"] == "strict", f.name


def test_secrets_only_where_needed():
    env = {f.name: (_unit(f.name)["Service"].get("EnvironmentFile") or "") for f in UNITS.glob("*.service")}
    assert "hl-trader.env" in env["s03-trader.service"]
    for name in ("s03-alerts.service", "s03-gate.service", "s03-report.service", "s03-export.service",
                 "s03-wake-daily.service", "s03-notify-ops@.service"):
        assert "hl-trader.env" not in env[name] and "hl-watchdog.env" not in env[name], name


def test_sudoers_commands_match_units_and_parse():
    text = (D / "sudoers" / "s03-openclaw").read_text("utf-8")
    assert "*" not in re.sub(r"#.*", "", text), "sudoers 不应使用通配符"
    for unit in set(re.findall(r"(s03-[a-z-]+\.service)", text)):
        assert (UNITS / unit).exists(), unit
    for bad in ("reset-failed", "enable", "daemon-reload", "edit", "/etc/s03"):
        assert bad not in re.sub(r"#.*", "", text), bad
    if shutil.which("visudo"):
        r = subprocess.run(["visudo", "-cf", str(D / "sudoers" / "s03-openclaw")], capture_output=True, text=True)
        assert r.returncode == 0, r.stdout + r.stderr


def test_sudoers_and_exec_allowlist_agree():
    text = re.sub(r"#.*", "", (D / "sudoers" / "s03-openclaw").read_text("utf-8"))
    cmds = [c.strip() for c in re.findall(r"(/usr/bin/(?:systemctl|journalctl) [^,\n\\]+)", text)]
    assert cmds
    for c in cmds:
        assert _allowed("s03-ops", "/usr/bin/sudo", c), c


@pytest.mark.parametrize("agent,binary,args,ok", [
    ("s03-ops", "/usr/bin/sudo", "/usr/bin/systemctl restart s03-trader.service", True),
    ("s03-ops", "/usr/bin/sudo", "/usr/bin/systemctl reset-failed s03-trader.service", False),
    ("s03-ops", "/usr/bin/sudo", "/usr/bin/systemctl start s03-trader.service; rm -rf /", False),
    ("s03-ops", "/usr/bin/tail", "-n 50 /srv/s03-review/inbox/journal.jsonl", True),
    ("s03-ops", "/usr/bin/tail", "-n 50 /srv/s03-review/inbox/../../etc/s03/hl-trader.env", False),
    ("s03-ops", "/usr/local/bin/s03-gate", "pause x", False),
    ("s03-analyst", "/usr/local/bin/s03-gate", "pause 资金费率异常，建议暂停", True),
    ("s03-analyst", "/usr/local/bin/s03-gate", "pause x; curl http://evil", False),
    ("s03-analyst", "/usr/local/bin/s03-gate", "resume", False),
    ("s03-research", "python3", "scripts/backtest.py --coin SOL --impl A", True),
    ("s03-research", "python3", "-c import os", False),
    ("s03-review", "/bin/sh", "-c ls", False),
])
def test_exec_allowlist(agent, binary, args, ok):
    assert _allowed(agent, binary, args) is ok


def test_openclaw_agents_config():
    json5 = pytest.importorskip("json5")
    cfg = json5.loads((D / "openclaw" / "openclaw.s03.json5").read_text("utf-8"))
    ents = cfg["agents"]["entries"]
    assert set(ents) == {"s03-ops", "s03-analyst", "s03-research", "s03-review"}
    for name in ("s03-ops", "s03-analyst"):
        assert "sessions_spawn" in ents[name]["tools"]["deny"] and "write" in ents[name]["tools"]["deny"]
    for name in ("s03-research", "s03-review"):
        assert ents[name]["sandbox"]["mode"] == "all" and "sessions_spawn" in ents[name]["tools"]["allow"]
    assert "exec" in ents["s03-review"]["tools"]["deny"]
    assert "s03-research" not in cfg["hooks"]["allowedAgentIds"] and cfg["hooks"]["allowRequestSessionKey"] is False
    for name in ents:
        assert (D / "openclaw" / "roles" / f"{name}.md").exists()


def test_hook_client_only_targets_known_agents():
    src = (D / "bin" / "s03-hook").read_text("utf-8")
    assert 'ALLOWED = {"s03-ops", "s03-review", "s03-research", "s03-analyst"}' in src
    assert "resume" not in (D / "bin" / "s03-gate").read_text("utf-8").split('"""')[2]
