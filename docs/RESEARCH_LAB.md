# 研究前端：找想法、筛因子、统计检验（v2.7）

本框架的研究前端由 `fwresearch/` 提供，hyper-trader 与 freq-trader 两个模板共用同一份代码。
它借用了 [Vibe-Trading](https://github.com/HKUDS/Vibe-Trading)（MIT）的三项强项，但**交易逻辑、回测口径和上线门槛仍然由本框架负责**：

| 环节 | 用什么 | 由谁执行 |
|---|---|---|
| 找想法 | Vibe-Trading 本体（大模型研究、多智能体讨论、读网页 / 文档、行情与宏观数据），经 `mcp-guard` 白名单代理接给 OpenClaw 研究 agent | `s03-research` agent、你 |
| 筛因子 | 默认用跨币种比较（截面 IC，5 个币：BTC、ETH、SOL、BNB、HYPE）；内置因子库 55 个，可选调用 Vibe-Trading 的 478 个 | 程序 |
| 回测 | 本框架：`s03/research/replay.py` 15 分钟逐笔回放（与实盘同一插件、同一风控） | 程序 |
| 统计检验 | 从 Vibe-Trading 移植的检验 + 本框架补充的逐笔 R 检验 | 程序 |
| 批准 | 想法登记簿里的 `approved`，以及把策略写进手册第 4–5 节 | 只能是你 |

## 一条想法的完整流程

```
proposed ──▶ screening ──▶ backtesting ──▶ validated ──▶ approved（只能由人）
   └──────────────┴──────────────┴──────────────┴──▶ rejected（写原因）
```

1. **登记想法**（假设、机制、证伪条件三项必填）：
   ```bash
   python3 scripts/run.py lab idea add --title "资金费极端后的反转" \
     --hypothesis "资金费 21 期均值处于过去 120 期最高的 5% 时，之后 3 天收益为负" \
     --mechanism "多头拥挤、杠杆被动平仓" \
     --falsification "样本外 3 日收益不为负，或因子检验 FDR 不显著" \
     --source vibe-trading --ref <Vibe-Trading 研究目标 id> --factors crypto_funding_mean21
   ```
2. **筛因子**：
   ```bash
   python3 scripts/run.py lab factors --coins BTC,ETH,SOL,BNB,HYPE --tf 1d --horizon 3      # 默认读 research/data（`scripts/research_matrix.py fetch` 生成）
   python3 scripts/run.py lab idea attach --id I-20261028-001 --kind factors --path research/lab/factors-….json
   python3 scripts/run.py lab idea move --id I-20261028-001 --to screening --reason "因子 alive 且 FDR 显著"
   ```
3. **回测**：把想法写成策略插件（`docs/STRATEGY_TEMPLATE.md`），跑研究矩阵；进入回测前改状态（每次都计入尝试次数）：
   ```bash
   python3 scripts/run.py lab idea move --id I-20261028-001 --to backtesting --reason "写成插件 v1"
   python3 scripts/research_matrix.py plan --years 2022-2025 --impl A --variants base,stop_floor,cost2x
   python3 scripts/research_matrix.py run --job BTC-2024-A-base      # 每格一条，逐笔交易另存 research/results/trades/
   ```
4. **统计检验**：
   ```bash
   python3 scripts/run.py lab matrix                     # 研究矩阵全部方案：DSR、PBO、成本压力 + 基准方案的合并检验
   python3 scripts/run.py lab idea attach --id I-20261028-001 --kind stats --path research/lab/matrix-….json
   python3 scripts/run.py lab idea move --id I-20261028-001 --to validated --reason "统计检验通过"   # 结论不是“通过”会被拒绝
   ```
5. **批准**（只能由你在终端执行，agent 通过 `python3 scripts/run.py lab` 调用时会被拒绝）：
   ```bash
   python -m fwresearch idea move --id I-20261028-001 --to approved --reason "已审阅报告" --by human --approver Pocky
   ```
   批准只是记录。真正让策略生效的仍然是你把插件放进只读的代码目录、并按 G6–G9 走完影子运行与小额实盘。

## 统计检验与判定阈值

阈值全部是**示例值**（`fwresearch/stats.py` 的 `DEFAULT_GATES`），未经数据验证；改动时在研究报告里写明。

| 检验 | 回答的问题 | 判定（示例值） | 来源 |
|---|---|---|---|
| 交易笔数 | 样本够不够 | < 30 笔 → “证据不足”，不做其余判定 | 手册 G1 |
| 期望 R 的 Bootstrap 区间 | 平均每笔是否真的 > 0 | 95% 区间下限 > 0 | 本框架 |
| 符号翻转检验 | 同上，换一种方法 | p < 0.05 | 本框架 |
| 滚动分段（5 段） | 优势是否集中在某一段行情 | 期望 R > 0 的段数占比 ≥ 60% | Vibe-Trading `walk_forward_analysis`，改为逐笔 R |
| Deflated Sharpe | 扣掉“试了很多次挑最好的”之后还剩多少 | DSR > 0.95（尝试 ≥ 2 次时才判） | Vibe-Trading `multipletesting` |
| PBO（CSCV） | 挑出的最佳方案在样本外是否仍好于中位数 | < 0.5（方案 ≥ 2 个时报告） | Vibe-Trading `multipletesting` |
| Monte Carlo 排列 | 回撤路径是否特殊 | 只报告，不判定 | Vibe-Trading `monte_carlo_test` |
| Bootstrap Sharpe 区间 | 每笔 Sharpe 的不确定性 | 只报告 | Vibe-Trading `bootstrap_sharpe_ci` |

两点说明：

- **Monte Carlo 排列不检验优势**：打乱交易顺序不会改变平均值，它只说明回撤路径是否偶然。所以“有没有优势”交给 Bootstrap 与符号翻转检验。
- **尝试次数要如实记录**：DSR 的尝试次数 = 研究矩阵的方案数（不含 `cost2x` 这类压力方案）+ 想法登记簿里其他回测过的想法数。少记会让结论偏乐观。

## 因子检验的口径

- **默认：跨币种比较（截面 IC）**。每个时刻把 5 个币按因子值排序，看它和之后收益的排序是否一致（Spearman），再对时间取平均。
  回答的是“因子高的币，之后是否比因子低的币涨得多”，对应在几个币之间挑强弱的策略。所有币同涨同跌的部分被排序抵消，所以它**不检验择时**。
  - 只有 5 个币**同时**有数据的日期才计入。HYPE 永续在 OKX 2025-02-21 上市，所以截面样本从 2025-02-21 起；报告的“截面样本”一行会写明日期数和各币首个有效日期。
  - 每个时刻只有 5 个点排序，单期 IC 噪声很大，结论靠时间维度累积。
- **可选：时间序列 IC**（`--mode time_series`），回答“这个因子高的时候，这个币之后涨得多不多”，对应择时型策略。
  因子用过去 120 根 K 线的均值和标准差标准化，收益用过去 120 根的波动率标准化，所有标准化只用 t 时刻已知的数据，逐根相乘后按月平均。
  一个踩过的坑：如果在每个月内部直接算 Spearman，月内均值和方差会用到当月后面的数据，纯随机游走上 MA60 会得到 t≈15 的假显著。测试专门防止这个问题复发。
- **分类**：alive（IC > 0.02、正 IC 占比 ≥ 55%、|t| > 2）、reversed（IC < −0.02、|t| > 2）、dead。阈值来自 Vibe-Trading，属于示例值。
  另做 Benjamini-Hochberg 多重检验（FDR 0.1）；`fdr_significant = True` 才算候选。
- **观测数下限（v2.2 新增，示例值）**：截面 IC 少于 60 个、时间序列 IC 少于 12 个月的因子记为 `insufficient`，不参与显著性判断。
  起因：OKX 只能下载约 3 个月的资金费率，资金费因子只有 6 个截面观测，却得到 t=4.4、FDR 显著。
- 每个内置因子都有防前视测试；截面与时间序列两种口径在随机游走上都有 null 测试。
- **真实数据试跑（2026-10-28，OKX 永续日线，5 个币，截面口径，613 个日期 2025-02-21 → 2026-10-27）**：
  内置 55 个 + Vibe-Trading 408 个 = 463 个因子；0 个通过 FDR；15 个名义上 reversed（多重检验后不显著），14 个样本不足。
  这个结果符合预期：不到两年的数据、5 个高度相关的币，不足以支持任何因子结论。

## 接入 Vibe-Trading（可选）

```bash
sudo python3 -m venv /opt/s03/vt-venv && sudo /opt/s03/vt-venv/bin/pip install "vibe-trading-ai==0.1.18"
sudo install -m 0755 deploy/bin/s03-vt-mcp /usr/local/lib/s03/s03-vt-mcp
sudo install -m 0640 -o root -g openclaw deploy/env/vibe-trading.env.example /etc/s03/vibe-trading.env   # 只填大模型配置
# OpenClaw 配置：mcp.servers["vibe-trading"].enabled 改为 true，然后 openclaw doctor
# 全量因子库：VT_PYTHON=/opt/s03/vt-venv/bin/python python3 scripts/run.py lab factors --zoo all
```

安全设计：

- Vibe-Trading 装在独立虚拟环境 `/opt/s03/vt-venv`，固定版本 `vibe-trading-ai==0.1.18`；升级前先跑 `tests/test_fwresearch.py`。
- OpenClaw 启动的是 `s03-vt-mcp` 包装脚本，它经 `python -m fwresearch mcp-guard` 再启动 `vibe-trading-mcp`：
  - `tools/list` 只返回白名单里的 26 个研究工具；
  - 白名单外的调用由代理直接拒绝，不会到达 Vibe-Trading。`trading_*`、`paper_trade_step`、`write_file` 等即使误加进白名单也会被拒绝；
  - 子进程只拿到白名单环境变量，交易所、钱包、券商相关的变量一律过滤；每次调用和拒绝都写入 `research/lab/mcp-audit.log`。
- OpenClaw 配置里另有 `toolFilter.include`、`codex.agents`、各 agent 的 `vibe-trading__*` 允许 / 拒绝规则，以及研究 agent 的沙箱工具闸门 `tools.sandbox.tools.alsoAllow`。只有 `s03-research` 能用（已用 OpenClaw 2026.10.8 实测）。
- MCP 服务器运行在 Gateway 主机上，而不是研究沙箱里，所以 `/etc/s03/vibe-trading.env` 只放大模型配置，不放任何交易所密钥。
- 装好后运行 `python3 scripts/run.py lab vt-check` 自检。
- 不装 Vibe-Trading 也能用：筛因子（内置因子库）、统计检验、想法登记簿都不依赖它。

## 安装验证记录（2026-10-28，v2.2）

在一台干净的 Linux 机器上，按本文件的方法安装 Vibe-Trading 和 OpenClaw，逐项验证。**加粗**的是因此修改了配置或代码的地方。

| 项目 | 实测结果 | 处理 |
|---|---|---|
| 安装 `vibe-trading-ai==0.1.18` | 需要 Python ≥ 3.11；独立虚拟环境约 1.1 GB，约 2 分钟。安装后的顶层包名是 `src`、`backtest`、`cli` | 必须放在独立虚拟环境（`/opt/s03/vt-venv`），不能和框架装在一起 |
| CLI | `vibe-trading --version` 输出 0.1.18；`alpha list` 列出 478 个因子 | 无 |
| **因子库桥接的数据格式** | 用 pickle 传 DataFrame 时，Vibe-Trading 环境缺 pyarrow，无法反序列化 | **改为 numpy npz（allow_pickle=False）+ JSON**，两边的 pandas 版本可以不同，也不再有 pickle 风险 |
| **Vibe-Trading 因子缺列** | 83 个因子要求 `vwap` 或 `amount` 列，K 线里没有 | **按近似补上**：vwap = (high+low+close)/3，amount = volume×close；报告里注明这是近似值（模型推断） |
| 真实数据全量因子 | 5 个币、613 个日期上算出 408 个；38 个因缺行业标签或输出几乎全为 NaN 被跳过；耗时约 10 秒 | 无 |
| **小样本假显著** | 资金费因子只有 6 个观测，却得到 t=4.4、FDR 显著 | **新增观测数下限**（截面 60、时间序列 12），不足记为 `insufficient` |
| MCP 握手（经 mcp-guard） | 协议 2025-06-18；服务器提供 82 个工具，代理只放行 26 个，白名单里的工具上游全部存在 | 无 |
| 拒绝交易类工具 | `trading_account`、`paper_trade_step`、`write_file` 均被代理拒绝，并写入审计日志 | 无 |
| 放行研究类工具 | `alpha_zoo`、`get_market_data`（BTC-USDT、HYPE-USDT）、`start_research_goal` 调用成功 | 无 |
| 密钥过滤 | 注入的交易所密钥类变量没有传到 Vibe-Trading 子进程 | 无 |
| `run_swarm`（多智能体讨论） | 没有配置大模型时返回 `LANGCHAIN_MODEL_NAME is not set` | 需要在 `/etc/s03/vibe-trading.env` 填大模型配置（不是交易所密钥）；本次未配置，未验证讨论结果 |
| `web_search` | 验证机所在网络下没有返回结果 | 未验证；与网络环境有关 |
| Vibe-Trading 的加密行情 | `get_market_data` 的 BTC-USDT 是 **OKX 现货**，日线以 **UTC+8** 切日（时间戳 16:00 UTC） | 只用于找想法；回测和因子检验一律用框架自己的永续、UTC 切日数据，两者不要混用 |
| 写入位置 | 只写 `VIBE_TRADING_HOME`（会话库、记忆、swarm 记录），没有写用户主目录 | 包装脚本已把它设为 `/srv/s03-research/.vibe-trading` |
| OpenClaw 版本 | 2026.10.8 需要 **Node ≥ 24.22.1 或 ≥ 26.12.1**；Node 22 会提示 node:sqlite 截断问题 | 写入 README 的环境准备 |
| 配置键名 | `openclaw config patch --file deploy/openclaw/openclaw.s03.json5 --dry-run` 通过（校验器对未知键会报错） | 去掉了原来的“核对”标记 |
| MCP 工具名 | 规范名是 `<服务器>__<工具>`，`vibe-trading__*` 写法正确（OpenClaw 自带文档 tools/multi-agent-sandbox-tools） | 无 |
| **沙箱工具闸门** | `openclaw doctor` 告警：研究 agent 在沙箱里运行，MCP 工具会在发给模型前被过滤 | **`s03-research` 增加 `tools.sandbox.tools.alsoAllow: ["vibe-trading__*"]`**；修改后告警消失 |
| **Codex 运行时的投射范围** | schema 说明：不写 `codex.agents` 时，服务器会投射给所有 Codex 运行时的 agent | **增加 `codex: { agents: ["s03-research"] }`** |
| MCP 自检 | `openclaw mcp doctor`：ok；`openclaw mcp probe vibe-trading`：26 个工具 | 无。probe 提示这些工具没有安全注解，在需要确认的会话模式下调用前会请求批准 |
| 命令白名单导入 | `openclaw approvals set --file deploy/openclaw/exec-approvals.s03.json` 导入成功；以 `_` 开头的注释键会被忽略，不必删除 | 无 |
| doctor 其他告警 | s03-ops / s03-analyst 只有 exec（受命令白名单和 sudoers 限制）；各 agent 不含 `skill_workshop` | 有意为之：agent 不应该自己编写技能 |
| HYPE 交易对 | Hyperliquid：HYPE 永续，平台最大杠杆 10 倍（API 实测）；OKX：HYPE-USDT-SWAP 于 2025-02-21 上市（API 实测）；Binance：HYPEUSDT 永续 2025-05-30 前后上线（来源方观点，验证机所在地区无法访问 Binance API） | 两个模板都已加入 HYPE |

**一条命令复查**（装好后、升级 Vibe-Trading 后都应运行）：

```bash
python3 scripts/run.py lab vt-check --vt-bin /opt/s03/vt-venv/bin
```

它依次检查版本、因子库桥接、MCP 握手、白名单、拒绝交易工具、密钥过滤，全部通过时 `ok: true`。

## BlockBeats（律动）新闻：接入判断与接法（v2.3，2026-10-29）

> v2.4 更正：BlockBeats 历史可以回填到 2020-01，事件规则已重写，并加入 Odaily 作为第二来源，见下一节“新闻来源：BlockBeats 与 Odaily”。

**结论：有限接入。** 只做两件事：

1. 重大事件守卫：按币种暂停新开仓，只收紧不放宽。
2. 新闻和市场数据存档：几个月后作为候选因子去检验。

**不做**：用新闻情绪调整仓位、杠杆、止损或入场。没有历史数据能证明它有用；新闻发出时价格多半已反应（模型推断）。

### 优劣势

| | 内容 | 证据 |
|---|---|---|
| 优势 | 被盗、下架、执法、暂停提现、脱锚、宕机、大额解锁这类事件发生时，避免在可能跳空的时候开新仓 | 模型推断；现在没有历史可回测 |
| 优势 | 免费、整理好的全市场数据（ETF 资金流、稳定币市值、持仓量、美债、美元指数等），可做候选因子 | 已核验接口存在；有没有用待检验 |
| 优势 | 研究 agent 找想法时能读到中文加密圈最快的快讯 | — |
| 劣势 | 没有情绪字段和币种标签，只能用可复现的关键词规则判断（规则与时长都是示例值） | 已核验官方文档 |
| 劣势 | ~~历史能取多深未知~~（v2.4 实测：可回填到 2020-01）；时间口径为北京时间字符串 | 已核验 |
| 劣势 | 调用限额与使用条款未公开 | 未知 |
| 劣势 | 新闻正文是外部文本，可能夹带针对 AI 的指令 | 只交给沙箱里的研究 agent，或先转成结构化字段 |

### 两个官方库的审阅结果

| 库 | 结论 | 原因（已核验事实） |
|---|---|---|
| [blockbeats-MCP](https://github.com/BlockBeatsOfficial/blockbeats-MCP) 1.0.4，MIT | **采用，但要修一处**，只给 `s03-research` | 源码 388 行，只有 GET 请求，没有文件或进程操作；npm 包与 GitHub 上的 dist 一致。**但 `BASE_URL` 写的是 `http://`**，服务器在 HTTP 上直接返回 200、不跳转，API Key 会明文传输。`s03-install-blockbeats-mcp` 安装时固定版本，改为 `https://` 并校验；`s03-bb-mcp` 启动前再检查一次 |
| [blockbeats-skill](https://github.com/BlockBeatsOfficial/blockbeats-skill) 1.0.3 | **不采用**，只当接口参考 | 让 agent 自己执行 `curl`，等于给 agent 一条往外发数据的通道；还内置了未经验证的买卖规则，例如“情绪指标 < 20 → 潜在买入”“ETF 单日净流入 > 5 亿美元 → 强烈买入信号”，不符合“未经检验不进策略” |
| [RESTful-API](https://github.com/BlockBeatsOfficial/RESTful-API) | 不采用 | 只有一份 README，描述 2023 年的旧免 Key 接口；验证机连接被重置 |

框架自己的采集器（`fwresearch/blockbeats.py`）直接调用官方 Pro API，只用 HTTPS，Key 不进日志和输出。

### 接法

| 部分 | 文件 / 命令 | 行为 |
|---|---|---|
| 采集与存档 | `s03-news.timer`（每 5 分钟）→ `python -m fwresearch news collect` | 拉取“全部”和“重要”快讯；按（id, 分类）只追加存档，记录收到时间；生成 `news_blocks.json` |
| 事件规则 | `fwresearch/blockbeats.py` 的 `COINS`、`VENUES`、`CATEGORIES`、`EXCLUDE` | 同一段文字同时出现“币种或交易所”和“风险类别”才命中。封禁时长：被盗 24 小时、下架 72 小时、执法 12 小时、暂停提现 12 小时、脱锚 6 小时、宕机 6 小时、大额解锁 6 小时（均为示例值）。交易所级事件封禁该交易所上的全部币种 |
| 交易程序读取 | `s03/risk/guard.py` 的 `news_block()`：`[strategy] news_file` 指向守卫文件后，在下单前检查，并撤掉该币已挂的入场单 | 只在守卫状态为 `ok` 时封禁；文件超过 30 分钟未更新就忽略并记日志，不会卡住交易；回测不应用（没有历史） |
| 市场数据快照 | `s03-news-data.timer`（每天 00:40 UTC）→ `news data` | 存 ETF、稳定币、持仓量、美债、美元指数、Bitfinex 多头、顶底指标等原始返回 |
| 研究 agent | MCP 服务器 `blockbeats`（默认关闭）经 `mcp-guard` 放行 18 个只读工具 | 只给 `s03-research`，其他 agent 一律拒绝 |
| 以后的检验 | `news daily --out research/news_daily.csv`，再 `factors --tf 1d --news-csv research/news_daily.csv` | 存档满 60 天以上才有意义；按币新闻量、重要新闻量、风险命中数作为候选因子，过了 FDR 才写进策略 |

### 安装验证记录（2026-10-29）

| 项目 | 结果 |
|---|---|
| 官方 MCP 安装与修补 | `blockbeats-mcp@1.0.4` + `@modelcontextprotocol/sdk@1.32.2` 固定版本安装；`http://` 改为 `https://`，校验通过 |
| 经 mcp-guard 握手 | 18 个工具全部放行；`write_file`、`trading_place_order` 等被拒绝 |
| 走 HTTPS 访问真实接口 | 用无效测试 Key 调用，服务器返回 `Invalid API key`，说明请求经 HTTPS 到达官方服务器 |
| 框架采集器 | `news check` 用无效 Key 返回 `status 101`，输出里没有 Key；不设 Key 时给出申请指引 |
| OpenClaw 2026.10.8 | `config patch --dry-run` 通过；`mcp probe blockbeats` 显示 18 个工具；`mcp doctor` 正常；没有沙箱过滤告警 |
| 代理环境 | Node 自带的 fetch 默认不走 `HTTPS_PROXY`，要设置 `NODE_USE_ENV_PROXY=1`（mcp-guard 已放行这个变量） |
| 真实 Key（v2.4 补测） | `news check` ok；10 个数据接口全部可用；时间为北京时间字符串；未见限频响应头 |

## 新闻来源：BlockBeats 与 Odaily，同时接入（v2.4，2026-10-29）

用真实 BlockBeats Key 和 Odaily 公开接口实测后的结论：**两家同时接入，但各有分工**；接入前先修好了事件规则（v2.3 的规则误报严重，见下）。

### 实测对比（已核验事实，2026-10-29）

| 项目 | BlockBeats Pro API | Odaily 公开接口 |
|---|---|---|
| 认证 / 费用 | 需付费 Key | 不需要 Key |
| 快讯历史 | 约 16.8 万条，可翻页回到 2020-01（每页最多 50 条） | 约 38 万条，可翻页回到 2018-05（每页最多 100 条） |
| 最近一周数量 | 1,099 条（“重要”636） | 1,106 条（“重要”519） |
| 早期密度 | 2023–2024 年约为 Odaily 的一半 | 更全 |
| 时间字段 | `create_time` 为北京时间字符串（与框架的解析一致） | `publishTimestamp` 毫秒 UTC |
| 分类 | 重要、原创、首发、链上、融资、预测市场、AI | 交易所公告、链上数据、宏观政策、项目动向等 12 类 |
| 结构化数据 | 有：ETF 资金流、稳定币市值、合约持仓（含 Hyperliquid）、Bitfinex 多头、美债、美元指数等 10 个接口全部可用 | 没有 |
| 文档与实际不符 | `size` 超过 50 也只返回 50 | `isImportant=true` 过滤不生效（每条自带该字段，可用） |
| 限频 / 条款 | 无限频响应头；条款未查 | 无限频响应头；使用条款未知 |

**速度与覆盖**：同一周按标题相似度匹配到的同一条新闻，发布时间中位差约 30 秒（Odaily 略早，四分位 −5 分钟到 +1.5 分钟），可以视为一样快。事件层面高度重合；但在 6 个历史事件复核里，有 2 个只被其中一家用可识别的标题报道（币安认罪和解只在 Odaily 命中，Hyperliquid 订单系统故障只在 BlockBeats 命中）。所以第二个来源主要带来**冗余**（一家断了守卫照常）和少量**召回**。

### v2.3 事件规则的问题与修正

用两家最近一周的快讯回放 v2.3 规则，封禁时间占比：ETH 88%、BTC 56%、SOL 29–49%、`@binance` 21–25%，几乎全是误报。原因是规则看整段正文：第三方协议被盗时“损失 8.5 枚 BTC”、交易对的计价币（AAVE/BTC）、“星球午讯”汇总帖、“成功阻止攻击”“没有发生黑客攻击”这类否定句都被当成事件。

v2.4 规则（`fwresearch/blockbeats.py` 的 `classify`，v2.5 已改为只看正文，见下文）：

- 只看标题；
- 被盗 / 宕机按币种封禁时，币种必须是主语（“以太坊主网…”“Solana 网络…”“Solana：…”官方声明）；
- 下架 / 解锁 / 脱锚要求币种紧挨风险词，交易对、杠杆对下架不算；
- 交易所级事件要求交易所名和风险词都在标题里；交易所暂停个别代币充提不封整个交易所；Hyperliquid 平台事件同时封 HYPE；
- 排除汇总帖、已恢复 / 否认 / 未受影响 / 判决 / 例行维护类标题。

修正后：同一周两家的封禁时间占比都是 0（这一周没有涉及这 5 个币及 3 个交易所的真实事件）；6 个历史事件里命中了 Solana 主网停摆（2024-02-06）、Hyperliquid 朝鲜黑客地址交易（2024-12-23）、币安认罪和解（2023-11-21）、Hyperliquid 订单系统故障（2025-07-29），未误封 JELLY 下架当天的 HYPE（只剩 1 条边缘标题命中）。**这套规则是在 1 周快讯和 6 个事件上人工调的，有过拟合风险**（模型推断）；上线前用 `news backfill` + `news replay` 在别的年份复核误报和漏报。

### v2.5（2026-10-29）：两家同时作为必需来源；事件规则改为只看正文

按你的决定做了两处改动：

1. **两家都是必需来源。** 采集默认 `odaily,blockbeats`，`/etc/s03/blockbeats.env` 不再可选。某一家这一轮失败时，守卫状态为 `degraded`：剩下那家的封禁照常生效，交易程序记日志告警（HL 写 `news_guard_degraded`，FT 打 warning，`s03ft status` 显示各来源状态）。
2. **规则只看正文，不看标题。** 去掉媒体开头（“BlockBeats 消息，10 月 29 日，”“Odaily星球日报讯”）后逐句判断，同一句里要同时有币种或交易所和风险词：
   - 汇总帖（正文是“1. … 2. …”编号列表）整条跳过；整条里有“已恢复 / 否认 / 没有发生 / 未受影响 …”整条跳过；
   - 背景回顾句（“此前…”“去年…”）、假设和提醒句（“若…”“建议…”“谨防…”）、“黑客松”不算；
   - 币种被盗 / 宕机要求币种是主语（币种后 12 个字内跟“主网 / 网络 / 验证者…”）；“在 BNB Chain 上铸造”、个人私钥被盗不算；
   - 交易所被盗类：资金“存入 / 转入 / 还款”交易所、“在 Hyperliquid 上交易 / 做多”不算；“比特币安全”“代币安全港”里的“币安”不算；
   - 整条提到升级维护时，不判暂停提现。

**同样的数据上对比（已核验事实；规则与时长仍是示例值）：**

| 数据 | v2.4 只看标题 | v2.5 只看正文 |
|---|---|---|
| 最近一周（两家合并） | 封禁占比都是 0 | HYPE 4%（HyperLabs 申请赎回 18 万枚 HYPE），其余 0 |
| 6 个历史事件（调规则用的样本） | Solana 停摆只有 Odaily 识别；币安和解只有 Odaily；订单系统故障只有 BlockBeats | Solana 停摆两家都识别；币安和解两家都识别（共 25 条）；另识别出 2023-05 以太坊信标链出块故障；订单系统故障仍只有 BlockBeats |
| 2026-04 至 10 月 Odaily 回放（**调规则时没看过的数据**，3.3 万条） | 22 次命中，单个目标封禁占比最高 2.4% | 48 次命中（38 条快讯），最高 3.6%（`@binance`、ETH） |

**结论（模型推断）：** 只看正文召回更好（同一事件两家都能识别，冗余更有意义），代价是误报约翻倍。在没调过规则的 7 个月里，两种规则的命中**大多数都是误报**（第三方协议被盗、诉讼评论、行业报告等），真实相关事件只有少数几条（例如 2026-10-09 美国司法部审查币安合规、HYPE 大额解除质押）。因为封禁总时长占比不到 4%，代价有限；但上线前仍应在更长的历史上回放，再决定要不要继续收紧，或改成“标题和正文同时命中才封”。

### 分工

| 用途 | 来源 | 原因 |
|---|---|---|
| 实盘事件守卫 | 两家合并，两家都是必需来源（任一家命中就封；同一事件不叠加时长；一家失败时状态 degraded） | 一家故障时另一家顶上；互补召回 |
| 新闻因子 / 事件研究 | **只用一家**，建议 Odaily（`news daily --source odaily`） | 历史最长、早期更全；两家合并会重复计数 |
| 市场数据因子 | BlockBeats（`news data` 每日快照） | 只有它有 ETF 资金流、稳定币、持仓量等 |
| 研究 agent 读新闻 | BlockBeats MCP（经 mcp-guard，18 个只读工具） | Odaily 不需要 MCP，研究 agent 用框架命令即可 |

**回填数据的偏差**（写进任何结论里）：回填的是现在还在网站上的快讯，删掉的不会出现，正文可能被改过，“重要”标记可能是事后补的；回填行的“收到时间”用的是发布时间。BlockBeats 里 `bottom_top_indicator` 自带“小于 20 潜在买入、大于 80 潜在卖出”的说明，是来源方观点，未经检验，不进策略。

### 命令

| 命令 | 作用 |
|---|---|
| `python -m fwresearch news check` | 两个来源的连通性自检（都应 ok: true） |
| `python -m fwresearch news collect` | 一轮采集（定时器每 5 分钟跑一次）；`--sources odaily,blockbeats` 可指定来源 |
| `python -m fwresearch news backfill --sources odaily --since 2020-01-01` | 回填历史到 `research/news/news/backfill-odaily.jsonl`；默认每页间隔 1 秒，可中断续跑 |
| `python -m fwresearch news replay --source odaily --since 2024-01-01 --verbose` | 用当前规则回放：每个币 / 交易所被封的时间占比和命中标题 |
| `python -m fwresearch news daily --source odaily` | 按币每日新闻计数，给 `factors --news-csv` |

## 上线判定与研究流程（v2.7，来自 v2.6 的外部审查）

**上线判定只有一个入口**（freq-trader：`s03ft research verdict --idea <id>`；hyper-trader：`python3 scripts/research_matrix.py verdict --idea <id>`），
只用组合回测的逐笔记录，并要求：

1. **样本外段事先登记**：`lab idea holdout --id <id> --start <日期>`，只能在想法进入 backtesting 之前登记一次，之后不能改。
   判定读登记值；命令行 `--holdout-start` 只作参考（判“证据不足”）。
2. **样本外按入场时间归属**：切分日之后入场的交易才算样本外；之前入场、之后平仓的跨边界持仓两边都不算，单独报告笔数；
   样本外段至少占全部时间跨度的 20%（示例值；G2 写的是最后 30%）。
3. **必需压力方案随策略声明**：freq-trader 的策略类属性 `research_stress`（默认 `cost2x`、`delay1`、`stopslip`，`research_stop_slip` 默认 0.2%）；
   hyper-trader 的插件属性 `research_stress`（默认 `cost2x`、`funding2x`）。`plan` 与 `portfolio --all` 把它写进清单。
   每个方案四种状态：没有运行、运行失败 → 证据不足；基准有交易而压力方案零交易 → 不通过；否则期望 R > 0 为通过（示例门槛）。
   改动 `research_stress` 要写进策略规格，并在回测之前定好。
4. **研究数据缺口**（没有历史事件文件、资金费或日线没覆盖回测区间、某个币数据不足）存在时，最多判“证据不足”。
5. 符号翻转检验只报告、不参与判定（零假设下 R 关于 0 对称的前提，在止损截断、盈利长尾时不成立）。
6. `lab matrix`（单币 × 单年）只出“诊断”，不再给上线判定。

**结果可复核**：组合回测每次运行一个独立目录 `research/results/portfolio/<方案>/run-<时间>/`，`latest.json` 原子替换，
运行失败也会成为 latest（状态 error），旧的成功结果不会再被读到；判定时核对状态、版本指纹和逐笔文件的 sha256。
版本指纹包含：代码、解析后的最终配置（freq-trader 用 `freqtrade show-config`，含附加配置与 `FREQTRADE__…` 环境变量，密钥显示为 REDACTED；
hyper-trader 用解析后的策略 / 成本 / 风控设置）、依赖版本、历史事件文件、数据文件的**内容**校验和。
`verify` 要求两次都成功且都有哈希；订单哈希包含 freqtrade 导出的全部订单字段（回测导出本身不含挂单状态）。

**回放的事件窗口（hyper-trader）**：回放现在读取历史事件文件，事件前后不新开仓、撤掉已挂入场单，与实盘一致。

**止损滑点压力 `stopslip`（freq-trader）**：对止损离场的交易，事后再扣“额外滑点 × 入场名义价值”，按初始风险折成 R。
这是近似：不改变触发时点，部分止盈后的剩余仓位按全部名义价值扣（偏保守）。

**仍未解决（结论里要写明）**：真正的滚动“训练 → 冻结 → 测试”未自动化；尝试次数仍靠登记；资金费翻倍（freq-trader）、
跳空与排队未实现；hyper-trader 的组合回测是交易层近似；回测不应用新闻守卫。

## 研究流程的修正（v2.6，来自外部审查）

**统计判定（`fwresearch/stats.py`）**：

| 情况 | v2.5 及以前 | v2.6 |
|---|---|---|
| 30–99 笔 | 可以判“通过” | 最多判“证据不足”（G1 要求 ≥ 100 笔） |
| 某项检验无法判定（DSR 缺数据、未指定样本外段等） | 被排除，其余通过就算通过 | 判“证据不足”，并列出缺哪项 |
| PBO、成本压力 | 计算了但不进总判定 | `lab matrix` 的总判定：任一部分不通过 → 不通过；缺任一部分 → 证据不足 |
| 样本外 | 没有 | `--holdout-start <日期>`：之后的交易单独检验，期望 R > 0 且 ≥ 10 笔（示例值）；不指定 → 证据不足 |
| 逐笔不独立 | 只有逐笔 Bootstrap | 另加按周汇总 R 的 Bootstrap（必需检验） |
| `walk_forward` | 名字像滚动验证 | 报告里改叫“分段一致性”：同一批结果按时间切段，不是“训练 → 冻结 → 样本外”的滚动验证 |
| 只登记 1 种方案 | DSR 未判定 | DSR 退化为 PSR 判定，并提示如实登记尝试次数 |

符号翻转检验假设零假设下 R 关于 0 对称；止损截断在 −1R、盈利长尾时只作参考，以 Bootstrap 为主。

**可复核性**：`plan` 写入任务清单 `research/jobs.json` 和版本指纹（代码、配置、数据文件清单等的哈希）；`run` 先删旧结果再跑；
`aggregate` 与 `lab matrix` 只用清单里、成功、指纹一致的任务（被排除的会列出来）。freq-trader 的回测一律 `--cache none`
（freqtrade 的缓存不包含框架基类和外部文件）；复核哈希包含净盈亏、费用、资金费、止损和全部订单。`base` 方案固定 `stop_min_mode = skip`。

**组合回测是主证据**：研究矩阵（单币 × 单年）只作诊断——测不到组合约束、年末强制平仓、合并的逐笔记录不是一个账户的路径。

| 模板 | 命令 | 做法 |
|---|---|---|
| freq-trader | `s03ft research portfolio --variant base --timerange 20220101-` | freqtrade 原生的多交易对、连续时间、共享余额回测（含框架全部入场检查） |
| hyper-trader | `python3 scripts/research_matrix.py portfolio --since 2022-01-01` | 每个币连续时间回放，再按同时持仓数、总风险取舍、共享权益复利（`fwresearch/portfolio.py`，交易层近似：被挡掉之后的信号不重算、不查相关性） |

组合回测的逐笔 CSV 在 `research/results/portfolio/<方案>.csv`，可交给 `lab stats --trades … --holdout-start …`。
权益曲线按已平仓交易计算，不含持仓浮亏。

**压力方案（每个只改一个因素）**：freq-trader 有 `cost2x`（每边 0.19%）、`delay1`（入场推迟一根 K 线，只在回测生效）；
hyper-trader 有 `cost2x`（手续费 × 2、滑点 × 3，止损单也按此滑点）、`funding2x`。`lab matrix --stress auto` 用清单里跑过的压力方案，`cost2x` 必须有。
**仍未实现（结论里要写明）**：freq-trader 的止损额外滑点与跳空、资金费 × 2、未成交与排队；hyper-trader 回放不应用事件窗口与新闻守卫。
freq-trader 回测要用历史事件：`s03ft events build --out research/events_history.json --history`，研究矩阵会自动读取。

## 命令一览

| 命令 | 作用 |
|---|---|
| `python3 scripts/run.py lab factors [--zoo builtin|vt|all] [--mode auto|cross_section|time_series] [--horizon N]` | 筛因子（5 个币时默认跨币种比较），输出 `research/lab/factors-*.json|md` |
| `python3 scripts/run.py lab stats --trades <csv> --holdout-start <日期>` | 对一份逐笔交易做统计检验（不指定样本外段 → 证据不足） |
| `python3 scripts/run.py lab matrix --holdout-start <日期> [--stress auto]` | 研究矩阵的多方案检验（含 PBO、压力方案的总判定） |
| `python3 scripts/run.py lab idea add|list|show|move|attach|trials` | 想法登记簿（`research/ideas.jsonl`，只追加，建议提交到仓库） |
| `python3 scripts/run.py lab vt-check [--vt-bin DIR]` | Vibe-Trading 安装自检 |
| `python3 scripts/run.py lab news check|blocks|daily|backfill|replay` | 新闻（Odaily + BlockBeats）：自检、当前守卫、每日汇总；另有 `collect`、`backfill`、`replay`（见上一节） |

来源与许可见 `fwresearch/NOTICE.md`。
