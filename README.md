# hyper-trader：Hyperliquid 永续自动交易框架（策略留空，可接入任意策略）

v2.0 起，这是一个**不含交易策略的框架**：数据、风控、下单与对账、止损与分批止盈、影子模式、回放回测、
告警、复盘、OpenClaw 运维都已就绪；交易规则由你写成一个**策略插件**接入（`s03/strategy/api.py`）。
默认使用空白模板 `BlankStrategy`：可以运行，但不会产生任何入场。原先的 S-03 策略保留在 git 历史中（v1.x）。
`s03` 是框架的命名空间（包名、服务名、agent 名），与策略无关。

## 接入你的策略（3 步）

1. 按 `docs/STRATEGY_TEMPLATE.md` 写好策略规格：适用市场、信号、止损、仓位、离场、验证门槛。
2. 复制 `s03/strategies/template.py` 为 `s03/strategies/my_strategy.py`，实现 `context()`、`step()`，
   按需实现 `take_profit_legs()`、`on_daily_close()`；参数写在 settings.toml 的 `[strategy.params]`。
3. 在 settings.toml 设置 `[strategy] plugin = "s03.strategies.my_strategy:MyStrategy"`，跑 `pytest -q`、
   回放回测（`scripts/backtest.py`、`scripts/research_matrix.py`），再按 G1–G9 逐级上线。

框架对任何策略都强制执行：止损距离护栏（`stop_min_pct`–`stop_max_pct`）、单笔风险与总风险、强平距离 ≥ 3 倍止损、
相关性合并、事件窗口、最长持有天数、日/周故障保护、熔断与暂停。插件不能绕过。
示例插件 `s03/strategies/examples/sma_cross.py`（均线示例）只用于验证框架能跑通，不代表可交易。

**不代表任何策略有效**：回测与影子模式必须先通过 G1–G8，主网需要你手动解锁（G9）。

## v1.0 改进（2026-10-26）

| 改进 | 文件 |
| --- | --- |
| 事件日历改用官方来源（美联储 FOMC 日程、BLS 与 BEA 的 iCal），每天生成 events.json；交易程序读取，文件过旧即停新仓 | `s03/events_calendar.py`、`run.py events`、`deploy/systemd/s03-events.*` |
| 最小止损护栏（`stop_min_mode`，v2.0 起由框架统一执行）与资金费预留上限（`funding_cap_frac_of_stop`） | `s03/strategy/api.py`、`s03/risk/sizing.py` |
| 随机性质测试（Hypothesis）：止损范围、强平距离、风险预算、告警冷却 | `tests/test_properties.py` |
| 研究矩阵：子 agent 并行回测、复核子 agent 重跑比对哈希 | `scripts/research_matrix.py`、`deploy/openclaw/roles/tasks/` |
| 复盘指标由程序计算（R 倍数、胜率、盈亏比、连亏、离场原因） | `s03/report/metrics.py`、`run.py metrics` |
| systemd 看门狗（进程卡住也重启）与外部心跳 | `s03/live/sdnotify.py`、`deploy/bin/s03-heartbeat` |

## 目录

| 路径 | 作用 | 对应手册 |
| --- | --- | --- |
| `s03/strategy/api.py` | **策略插件接口**：Context、ArmState、EntryIntent、Position、ExitIntent、StrategyPlugin、框架止损护栏 `check_stop`、加载器 | 第 5 节 |
| `s03/strategies/template.py` | **空白策略模板**（默认；不产生入场） | 第 5 节 |
| `s03/strategies/examples/sma_cross.py` | 示例插件（只用于验证框架） | — |
| `s03/signals/indicators.py` | EMA、ATR、摆动点、AVWAP（纯函数，供策略使用） | 第 4 节 |
| `s03/risk/sizing.py` | 仓位公式、单仓逐仓杠杆公式、强平距离 | 第 7 节 |
| `s03/risk/guard.py` | 风控层：拒单、日/周故障保护、连亏暂停、相关性合并、额度、事件窗口、熔断 | 第 7、8 节 |
| `s03/execution/executor.py` | 唯一调用 exchange 端点的模块；入场触发单与止损成组提交（normalTpsl） | 第 12 节 |
| `s03/execution/precision.py` | 价格 5 位有效数字 / 小数位、数量精度 | 官方 tick-and-lot-size |
| `s03/execution/cloid.py` | 确定性 cloid，重启不重复下单 | 第 12 节 |
| `s03/data/hl_data.py` | info 端点只读数据、深度、额度查询 | 第 3 节 |
| `s03/data/okx_ref.py` | OKX 参考价与历史（只读） | 第 3 节 |
| `s03/data/recorder.py` | 录制存档、缺失与异常检查、交叉核对 | 第 3 节 |
| `s03/live/loop.py` | 15 分钟循环：对账 → 管理持仓 → 调用策略插件 → 框架风控 → 下单；websocket 成交监听 | 第 8 节 |
| `s03/live/watchdog.py` | 看门狗：心跳中断时只撤入场单，保留止损 | 第 12 节 |
| `s03/research/replay.py` | 15 分钟逐笔回放（实现 A / B），与实盘调用同一个策略插件和止损护栏 | 第 6 节 |
| `s03/research/sim.py` | 成交模拟（回放与影子模式共用） | 第 6 节 |
| `s03/report/daily.py` | 日报 / 周报 + AI 多角色只读评审提示词 | 第 9 节 |
| `scripts/` | 命令行入口、筛选、回测、测试网必测项 | — |
| `s03/ml/features.py` | 机器学习特征（只用信号时刻已知数据；训练与实盘共用） | 第 14 节 |
| `s03/ml/events.py` | 候选事件数据集：每次挂入场单即一个事件，标签为扣成本 R | 第 14 节 |
| `s03/ml/validate.py` | 净化滚动验证、离群检测、Monte Carlo、验收与提案 | 第 14 节 |
| `s03/ml/gate.py` | 实盘门控：默认关闭；只降风险不放大；需批准哈希；过期/离群回退 | 第 14 节 |
| `s03/alerts/core.py` | 告警核心：波动、持仓、资金费率、参考价、数据中断 7 条规则 + 冷却 + 限流（纯计算，与 freqtrade 版共用） | 第 16 节 |
| `s03/alerts/hl_monitor.py` | 行情监控进程：WebSocket 行情 → 告警 → OpenClaw（不需要私钥，不消耗 LLM token） | 第 16 节 |
| `s03/alerts/openclaw.py` | 调用 OpenClaw 官方 webhook `/hooks/agent`；后台队列，不阻塞交易 | 第 16 节 |
| `s03/alerts/gate.py` | OpenClaw 白名单代理：只有 GET /status 与 POST /pause | 第 15、16 节 |
| `deploy/` | 由 OpenClaw agent 运行整套流程：systemd 服务、sudoers 白名单、4 个 agent 的配置与职责说明、命令白名单 | 第 17 节 |
| `docs/FRAMEWORK.md`、`docs/STRATEGY_TEMPLATE.md` | 框架说明与策略规格填写模板 | — |
| `tests/` | 71 个测试（含策略接口、空白模板、示例插件、回放、框架护栏） | G7 |

## 安装

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp config/settings.example.toml config/settings.toml
pytest -q
```

## 推荐顺序（与手册 30 天计划一致）

1. `python scripts/screen_now.py` —— 只读，看当前策略插件对各标的的日线判断（空白模板会提示“未配置策略”）。
2. `python scripts/run.py record` —— 每天跑一次（可用 cron），积累 HL 自有数据。
3. `python scripts/backtest.py --coin SOL --impl A` 与 `--impl B`；加 `--fee-mult 2 --slip-mult 3` 做 G5。
4. `python scripts/run.py loop` —— 默认 `mode="shadow"`：用主网实时行情跑完整逻辑，**不下任何单**。
5. 测试网：设置环境变量后运行 `python scripts/testnet_checks.py --coin BTC`，确认检查 1–5。
6. 检查全部通过后，把 `mode` 改为 `live`、`network` 保持 `testnet` 跑满 4 周（G8）。
7. 主网需要同时设置 `network="mainnet"` 与环境变量 `HL_ALLOW_MAINNET`（值见 `s03/config.py`）。

## 机器学习优化（第 14 节，离线、只产出提案）

```bash
python scripts/ml_optimize.py --okx-pages 2000            # 建事件集 → 验证 → var/ml/proposal_*.json
python scripts/ml_optimize.py --okx-pages 2000 --save-model   # 验收通过才另存候选模型
python scripts/run.py ml-approve                          # 你本人在服务器终端批准（记录哈希）
# 之后手动把 config/settings.toml 的 [ml] enabled 改为 true，并重新通过 G2–G8
```

事件来自你的策略插件每次“挂入场单”；少于 `min_events`（300）一律判定“证据不足”，不要为了通过而降低门槛。

## 行情监控与告警（第 16 节）

```bash
python scripts/run.py alerts          # 独立进程：WebSocket 盯盘，按阈值告警；不需要私钥
python scripts/run.py gate            # OpenClaw 白名单代理（127.0.0.1:8765）
python scripts/run.py pause "原因"     # 暂停新开仓（不撤止损、不平仓）
python scripts/run.py resume          # 恢复：只能由你手动执行
```

分工：15 分钟策略信号与下单 = 交易主循环（不用 LLM）；实时监控与告警判断 = `alerts` 进程（不用 LLM）；
只有告警才交给 OpenClaw 分析；OpenClaw 唯一能做的动作是经代理“暂停新开仓”；恢复、上线、改参数 = 你。
`[openclaw] enabled` 默认为 false。打开前设置 `OPENCLAW_HOOKS_TOKEN`（hooks 专用）和 `OPENCLAW_GATE_TOKEN`（代理专用）。

## 由 OpenClaw agent 运行整套流程（第 17 节）

见 `deploy/README.md`：4 个 agent（运维、告警分析、研究、复盘）分工、安装步骤、上线前演练。
agent 负责启动、停止、巡检、研究、复盘；交易决策始终由程序完成；权限靠独立系统用户、sudoers、文件权限和沙箱限定。

## 密钥

只通过环境变量提供，绝不写进文件、日志或任何对话：

```bash
export HL_ACCOUNT_ADDRESS=0x...   # 主账户或子账户地址（查询与对账用）
export HL_API_WALLET_KEY=0x...    # API 钱包私钥：只能交易、不能提现
```

建议：交易放在单独的子账户，子账户只放当前阶段允许使用的资金；看门狗使用另一个具名 API 钱包。

## 测试网必测项（未通过前不得进入 live）

| 编号 | 问题 | 不通过时 |
| --- | --- | --- |
| 1 | 买入方向触发单（tpsl=sl）能否开新仓 | 把 `strategy.entry_impl` 改为 `"B"` |
| 2 | 只减仓市价止损能否挂出并可见 | 停止，排查 |
| 3 | scheduleCancel 是否撤掉止损单 | 若撤掉：有持仓时绝不能用（代码已按此处理），确认后保留 |
| 4 | 公式算出的逐仓杠杆下，强平价是否低于止损价 | 调大 `liq_to_stop_multiple` |
| 5 | 成组下单（normalTpsl）后止损子单是否随入场成交生效 | 依赖成交监听补挂止损；需评估 15 秒内的空窗风险 |

## 已知限制与待办

- **信号数量取决于你的策略**：先用 `scripts/research_matrix.py` 算出每年交易笔数，再决定能否满足 G1（≥ 100 笔）。
- **相关性规则**：BTC/ETH/SOL 30 日相关系数通常 > 0.8，按手册“合并计一笔风险”，同一时间基本只会有 1 笔这类持仓。想放宽就调 `risk.corr_group_risk_cap`。
- **地址动作额度**：策略每次改入场单会同时改附带止损（2 次动作）；每 15 分钟改单的策略，4 个标的全天待触发时每天约 768 次。风控层在剩余额度 < 2,000 时拒绝新开仓。
- **数据源**：回放默认用 OKX USDT 永续数据代替 HL（HL 只保留 5,000 根 K 线），需用 `recorder.cross_check_closes` 测量价差并计入滑点。
- `userRateLimit` 返回字段、成组下单、触发单开仓等行为以测试网实测为准。

本代码是研究与流程工具，不构成投资建议，不保证收益。
