# hyper-trader：Hyperliquid 永续自动交易框架（模板版 v2.6 · 策略留空）

这是一个**不含交易策略的框架**，交易规则由你写成插件接入。框架已经提供：

- 行情数据；
- 风控，包括止损护栏、按风险定仓、杠杆与强平距离、事件窗口和熔断；
- 下单与对账、影子模式（不下单的模拟运行）、逐笔回放回测；
- 研究前端：找想法、筛因子、统计检验；
- 告警、复盘，以及由 OpenClaw agent 运维。

默认标的是 BTC、ETH、SOL、BNB、HYPE 五个永续合约。默认策略是空白模板：可以运行，但不会开任何仓位。

> **风险提示**：本仓库是研究与流程工具，不构成投资建议，不保证收益。任何策略都必须先通过 G1–G8 验证门槛，主网只能由你本人手动解锁（G9）。

**你是 AI agent？** 先读 [`AGENTS.md`](AGENTS.md)。里面写了你能做什么、不能做什么，以及每类任务的确切命令。

---

## 目录

1. [这个仓库能做什么、不能做什么](#1-这个仓库能做什么不能做什么)
2. [准备环境](#2-准备环境)
3. [10 分钟本地试跑（不需要任何密钥）](#3-10-分钟本地试跑不需要任何密钥)
4. [配置清单：要改哪些文件](#4-配置清单要改哪些文件)
5. [接入你的策略（3 步）](#5-接入你的策略3-步)
6. [从想法到上线的完整流程](#6-从想法到上线的完整流程)
7. [可选：接入 Vibe-Trading 找想法](#7-可选接入-vibe-trading-找想法)
8. [部署到服务器、交给 OpenClaw 运营](#8-部署到服务器交给-openclaw-运营)
9. [日常命令速查](#9-日常命令速查)
10. [常见问题](#10-常见问题)
11. [文档地图](#11-文档地图)

---

## 1. 这个仓库能做什么、不能做什么

| 能做 | 不能做 / 不会做 |
|---|---|
| 把你的交易规则（策略插件）变成可回测、可影子运行、可实盘的程序 | 替你想出能赚钱的策略；自带的示例策略只用来验证框架能跑通 |
| 对任何策略插件执行风控：止损距离 0.8–3%、单笔风险、总风险、强平距离 ≥ 3 倍止损、相关性合并、事件窗口（日历无效即拒绝新开仓）、最长持有、熔断。保证范围与已知限制见 `docs/FRAMEWORK.md`、`docs/REFERENCE.md` | 让策略绕过这些风控；保证回放与实盘成交一致 |
| 研究前端：登记想法 → 筛因子（跨币种比较）→ 回测 → 统计检验（Deflated Sharpe、PBO 等） | 把统计检验“通过”当作收益保证 |
| 由 OpenClaw agent 启停服务、巡检、做研究和复盘 | 让 agent 接触私钥、改参数、恢复交易或解锁主网 |

阈值标注贯穿整个仓库：**用户设定** = 你决定；**示例值** = 默认值，未经验证；**经数据验证** = 有回测证据。

## 2. 准备环境

| 需要 | 版本 / 说明 | 必须？ |
|---|---|---|
| Linux 或 macOS | 服务器部署按 Ubuntu / Debian 写 | 是 |
| Python | 3.11 及以上 | 是 |
| 网络 | 能访问 `api.hyperliquid.xyz` 和 `www.okx.com`（只读的公开行情） | 是 |
| Hyperliquid 子账户 + API 钱包 | 只在测试网 live 及之后需要；API 钱包只能交易、不能提现 | 后期 |
| OpenClaw | 2026.10.8 已验证；**需要 Node ≥ 24.22.1 或 ≥ 26.12.1** | 部署时 |
| Vibe-Trading | `vibe-trading-ai==0.1.18`，装在独立虚拟环境 | 可选 |

**密钥规则**：私钥只放在服务器上 root 0600 的环境文件或本机环境变量里，不写进仓库，也不贴进任何聊天（包括发给 AI）。

## 3. 10 分钟本地试跑（不需要任何密钥）

```bash
git clone <你的仓库地址> hyper-trader && cd hyper-trader
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pytest -q                                            # 约 160 个测试；未装 Vibe-Trading 时跳过 1 个

cp config/settings.example.toml config/settings.toml
sed -i 's|^event_file = .*|event_file = ""|' config/settings.toml          # 本地不读服务器上的事件文件

# 下载 5 个币的 OKX 永续历史（只读、公开；日线自动多取约一年）
python3 scripts/research_matrix.py fetch --coins BTC,ETH,SOL,BNB,HYPE --since 2026-09-01

# 筛因子：5 个币 → 自动用跨币种比较，报告写到 research/lab/
python3 scripts/run.py lab factors

# 看框架对当前行情的判断（空白模板会显示“策略未配置”）
python3 scripts/screen_now.py

# 用示例策略跑一次回放回测，确认整条链路能跑通（结果不代表可交易）
sed -i 's|^plugin = .*|plugin = "s03.strategies.examples.sma_cross:SmaCrossExample"|' config/settings.toml
python3 scripts/backtest.py --coin SOL --okx-pages 60
```

这几条命令在 2026-10-28 的一台干净机器上跑通过：下载约 3 分钟，回测约 40 秒，示例策略交易少于 30 笔，报告会写“证据不足”。
试完记得把 `plugin` 改回空白模板，或改成你自己的策略。

## 4. 配置清单：要改哪些文件

| 文件 | 改什么 | 谁来改 | 默认 |
|---|---|---|---|
| `config/settings.toml`（由 `settings.example.toml` 复制） | `[strategy] plugin`（你的策略）、`coins`（标的）、`[strategy.params]`（策略参数）、风险数字、`news_file`（新闻事件守卫，可选） | 你 | 空白模板；BTC、ETH、SOL、BNB、HYPE；`network="testnet"`、`mode="shadow"` |
| 环境变量 `HL_ACCOUNT_ADDRESS`、`HL_API_WALLET_KEY` | 账户地址、API 钱包私钥（服务器上放在 `/etc/s03/hl-trader.env`） | 你 | 无 |
| 环境变量 `HL_ALLOW_MAINNET` | 主网解锁口令（见 `s03/config.py`），对应 G9 | 只能是你 | 未设置 = 主网锁定 |
| `deploy/openclaw/openclaw.s03.json5` | 合并进 OpenClaw 配置：hooks token、模型 id；装了 Vibe-Trading 时把 `mcp.servers["vibe-trading"].enabled` 改为 `true` | 你 | Vibe-Trading 关闭 |
| `deploy/openclaw/exec-approvals.s03.json` | 4 个 agent 的命令白名单（`openclaw approvals set --file …` 导入） | 你（通常不用改） | — |
| `/etc/s03/blockbeats.env` | BlockBeats API Key（只放这一个）；新闻守卫（与 Odaily 同时必需）和研究 MCP 共用 | 你 | 启用新闻守卫时必填 |
| `/etc/s03/vibe-trading.env` | 只放大模型配置，绝不放交易所密钥 | 你 | 不填也能用因子库和行情工具 |

**增减标的**：只改 `settings.toml` 的 `coins`。新币要在 `scripts/backtest.py` 和 `scripts/research_matrix.py` 的 `PLATFORM_MAX` 里补上平台最大杠杆（以 Hyperliquid `meta` 为准）。
筛因子默认用跨币种比较，至少要 5 个币；少于 5 个时自动改用单币择时口径（时间序列 IC）。

## 5. 接入你的策略（3 步）

1. **写规格**：填 `docs/STRATEGY_TEMPLATE.md`，包括适用市场、入场信号、止损、仓位、离场、失效条件和对应的想法 id。
2. **写插件**：复制 `s03/strategies/template.py` 为 `s03/strategies/my_strategy.py`。
   - 必须实现 `context()`（今天这个币能不能做）和 `step()`（入场单是挂、改还是撤）。
   - 按需实现 `take_profit_legs()`（分批止盈）和 `on_daily_close()`（策略自己的离场）。
   - 参数用 `self.p("名字", 默认值)` 读取，写在 `[strategy.params]`。
   - 接口说明在 `s03/strategy/api.py` 和 `docs/FRAMEWORK.md`。
3. **接上并验证**：在 `settings.toml` 设 `plugin = "s03.strategies.my_strategy:MyStrategy"`，然后运行 `pytest -q` → 研究矩阵 → 统计检验（第 6 节）。

插件必须是纯函数，不读网络、时钟和文件；回测和实盘调用的是同一段代码。

## 6. 从想法到上线的完整流程

```
登记想法 → 筛因子 → 写成插件 → 研究矩阵回测 → 统计检验 → 你批准 → 影子模式 → 测试网 live ≥ 4 周 → 你解锁主网（小额）
 lab idea   lab factors            research_matrix  lab matrix   idea move     mode=shadow   network=testnet     HL_ALLOW_MAINNET
```

```bash
# ① 登记想法：假设、机制、证伪条件三项必填
python3 scripts/run.py lab idea add --title "…" --hypothesis "…" --mechanism "…" --falsification "…" --source human

# ② 筛因子（5 个币，跨币种比较；只有 alive / reversed 且 FDR 显著才算候选）
python3 scripts/run.py lab factors --tf 1d --horizon 3

# ③ 写成插件后：组合回测（主证据）+ 研究矩阵（诊断；逐笔交易另存 research/results/trades/）
python3 scripts/research_matrix.py portfolio --since 2022-01-01 --variant base
python3 scripts/research_matrix.py plan --years 2023-2025 --impl A --variants base,stop_floor,cost2x,funding2x
python3 scripts/research_matrix.py run --job BTC-2024-A-base          # 对 jobs.json 里每一格执行
python3 scripts/research_matrix.py aggregate                          # 只汇总清单里、当前版本、成功的任务

# ④ 统计检验：Bootstrap、按周 Bootstrap、分段一致性、Deflated Sharpe、PBO、样本外段、压力方案
python3 scripts/run.py lab stats --trades research/results/portfolio/base.csv --holdout-start 2025-01-01
python3 scripts/run.py lab matrix --holdout-start 2025-01-01

# ⑤ 批准（只能由你在终端执行）
python -m fwresearch idea move --id I-… --to approved --reason "已审阅报告" --by human --approver <你的名字>

# ⑥ 影子模式：主网行情，不下任何单
python3 scripts/run.py loop
```

之后的测试网必测项、G8、G9 见 `docs/REFERENCE.md` 的“测试网必测项”和“推荐顺序”。

## 7. 可选：接入 Vibe-Trading 找想法

[Vibe-Trading](https://github.com/HKUDS/Vibe-Trading) 提供大模型研究、多智能体讨论、读网页 / 文档和 478 个因子。本框架通过一个白名单代理（`mcp-guard`）把它接给 OpenClaw 研究 agent：

- 只放行 26 个研究工具；交易、连接器、写文件类工具一律拒绝；
- 不传任何交易所密钥；
- 每次调用写审计日志。

```bash
# 在服务器上（root）
python3 -m venv /opt/s03/vt-venv && /opt/s03/vt-venv/bin/pip install "vibe-trading-ai==0.1.18"   # 约 1.1 GB、2 分钟
install -D -m 0755 /opt/s03/hl-s03/deploy/bin/s03-vt-mcp /usr/local/lib/s03/s03-vt-mcp
install -m 0640 -o root -g openclaw /opt/s03/hl-s03/deploy/env/vibe-trading.env.example /etc/s03/vibe-trading.env

# 自检：版本、因子库桥接、MCP 握手、白名单、拒绝交易工具、密钥过滤，全部通过时 ok: true
python3 scripts/run.py lab vt-check --vt-bin /opt/s03/vt-venv/bin

# 全量因子库
VT_PYTHON=/opt/s03/vt-venv/bin/python python3 scripts/run.py lab factors --zoo all
```

然后在 OpenClaw 配置里把 `mcp.servers["vibe-trading"].enabled` 改为 `true`，运行 `openclaw mcp probe vibe-trading`，应显示 26 个工具。
完整的验证记录（哪些配置因实测而修改）见 `docs/RESEARCH_LAB.md` 的“安装验证记录”。

Vibe-Trading 只用于找想法和粗筛。它的回测按“下一根开盘调仓”成交、不模拟止损单；它的加密行情是 OKX 现货、按 UTC+8 切日。所以上线证据一律以本框架的回测为准。

### 新闻事件守卫：Odaily + BlockBeats（v2.5）

只用来做两件事。一是**重大事件守卫**：按币种或交易所暂停新开仓，只收紧不放宽；守卫文件 30 分钟不更新就失效，不会卡住交易。二是**存档与历史回填**：作为候选因子去检验。不用新闻情绪调整仓位、杠杆或进出场。

两个来源同时接入，都是必需来源：Odaily 免费，历史可回填到 2018 年；BlockBeats 付费会员，另有 ETF 资金流、稳定币、持仓量等数据。一家某轮失败时守卫状态为 `degraded`，另一家的封禁照常生效并告警。v2.5 起事件规则**只看正文、不看标题**（逐句判断）；在调规则时没看过的 7 个月数据上，封禁时间占比最高 3.6%，但命中里误报占多数。实测对比、规则说明和偏差见 `docs/RESEARCH_LAB.md` 的“新闻来源：BlockBeats 与 Odaily”。

**上线前先回放**：用 `news backfill` 补历史，再用 `news replay` 看规则在过去会封多久、封的是不是真事件。规则与封禁时长都是示例值。

```bash
# 详细步骤见 deploy/README.md 的“可选：新闻事件守卫”一节
python3 scripts/run.py lab news check          # 需要环境变量 BLOCKBEATS_API_KEY；两个来源都应 ok: true
python3 scripts/run.py lab news backfill --sources odaily --since 2024-01-01   # 回填历史（约 1 小时，可中断续跑）
python3 scripts/run.py lab news replay --source odaily --since 2024-01-01 --verbose
python3 scripts/run.py lab news blocks --guard-out /var/lib/s03-news/news_blocks.json   # 看当前哪些币被暂停
```

## 8. 部署到服务器、交给 OpenClaw 运营

按 [`deploy/README.md`](deploy/README.md) 逐条执行。它会建立：

- 独立系统用户、只读的代码目录、root 0600 的密钥文件；
- systemd 服务、sudoers 白名单；
- 4 个 agent：运维、告警分析、研究、复盘。

部署后先运行 `openclaw config patch --file deploy/openclaw/openclaw.s03.json5 --dry-run` 校验配置，再运行 `openclaw doctor`。

分工原则：**agent 负责操作（启动、巡检、研究、复盘），程序负责交易决策，你负责上线、恢复和改参数。**
即使 agent 被提示注入，它最坏也只能停服务或暂停新开仓，交易所端的止损始终有效。

## 9. 日常命令速查

| 目的 | 命令 |
|---|---|
| 跑测试 | `pytest -q` |
| 影子 / 实盘主循环 | `python3 scripts/run.py loop`（模式由 `settings.toml` 决定） |
| 暂停新开仓 / 恢复（恢复只能由你执行） | `python3 scripts/run.py pause "原因"` / `python3 scripts/run.py resume` |
| 熔断（撤单并平仓） | `python3 scripts/run.py kill "原因"` |
| 复盘指标 | `python3 scripts/run.py metrics --days 7` |
| 筛因子 / 统计检验 / 想法 | `python3 scripts/run.py lab factors` / `lab matrix` / `lab idea list` |
| Vibe-Trading 自检 | `python3 scripts/run.py lab vt-check` |
| 告警进程、白名单代理 | `python3 scripts/run.py alerts` / `python3 scripts/run.py gate` |

## 10. 常见问题

| 现象 | 原因与处理 |
|---|---|
| `lab factors` 报“缺少这些标的的数据：HYPE…” | 还没下载该币的历史：`python3 scripts/research_matrix.py fetch --coins HYPE`，或用 `--coins` 只选已有的币 |
| 截面样本从 2025-02 才开始 | HYPE 永续在 OKX 2025-02-21 上市，跨币种比较只用 5 个币**同时**有数据的日期 |
| 因子显示 `insufficient` | IC 观测太少（截面 < 60、时间序列 < 12 个月）。资金费因子常见：OKX 只能下载约 3 个月资金费率 |
| 所有因子都是 dead | 不到两年的数据、5 个高度相关的币，本来就很难有显著结论。不要为了出结果去调阈值 |
| 回测报告写“证据不足” | 交易少于 30 笔（G1）。延长回测区间或增加标的，不要降低门槛 |
| 某个币突然不再开新仓，日志里有 `news_block` | 新闻事件守卫（Odaily / BlockBeats）命中了这个币或交易所的重大事件（时长见 `docs/RESEARCH_LAB.md`）。用 `lab news blocks` 查看原因和到期时间；不要为了恢复交易去改规则 |
| 守卫状态是 `stale` | 采集定时器 30 分钟没有更新（Key 失效、限额或网络）。交易程序已自动忽略守卫，修好采集后自动恢复 |
| `openclaw` 提示 Node 版本不支持 | 2026.10.8 需要 Node ≥ 24.22.1 或 ≥ 26.12.1 |
| `openclaw doctor` 提示沙箱会过滤 MCP 工具 | 用仓库里最新的 `openclaw.s03.json5`：研究 agent 已加 `tools.sandbox.tools.alsoAllow: ["vibe-trading__*"]` |
| `vt-check` 的 `mcp_tools.missing_from_upstream` 不为空 | Vibe-Trading 升级后改了工具名；回到 0.1.18，或同步更新 `fwresearch/mcp_guard.py` 的白名单并重跑测试 |
| 主网下单被拒绝“主网未解锁” | 设计如此：通过 G1–G8 后由你本人设置 `HL_ALLOW_MAINNET` |

## 11. 文档地图

| 文档 | 内容 |
|---|---|
| [`AGENTS.md`](AGENTS.md) | 给 AI agent 的操作守则与任务命令 |
| [`docs/FRAMEWORK.md`](docs/FRAMEWORK.md) | 框架与策略的分工、15 分钟周期、上线顺序 |
| [`docs/STRATEGY_TEMPLATE.md`](docs/STRATEGY_TEMPLATE.md) | 策略规格填写模板（含验证门槛 G0–G9） |
| [`docs/RESEARCH_LAB.md`](docs/RESEARCH_LAB.md) | 研究前端：流程、统计检验、因子口径、Vibe-Trading 安装验证记录 |
| [`docs/REFERENCE.md`](docs/REFERENCE.md) | 模块目录、机器学习、告警、密钥、测试网必测项、已知限制 |
| [`docs/manual-hyperliquid.md`](docs/manual-hyperliquid.md) | 完整交易流程手册（第 1–19 节；v2.6 去掉了 S-03 残留） |
| [`deploy/README.md`](deploy/README.md) | 服务器部署与 OpenClaw agent |
| [`fwresearch/NOTICE.md`](fwresearch/NOTICE.md) | 移植代码的来源与许可（Vibe-Trading，MIT） |
