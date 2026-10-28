# 研究前端：找想法、筛因子、统计检验（v2.2）

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

## 命令一览

| 命令 | 作用 |
|---|---|
| `python3 scripts/run.py lab factors [--zoo builtin|vt|all] [--mode auto|cross_section|time_series] [--horizon N]` | 筛因子（5 个币时默认跨币种比较），输出 `research/lab/factors-*.json|md` |
| `python3 scripts/run.py lab stats --trades <csv>` | 对一份逐笔交易做统计检验 |
| `python3 scripts/run.py lab matrix [--base base] [--stress cost2x]` | 研究矩阵的多方案检验 |
| `python3 scripts/run.py lab idea add|list|show|move|attach|trials` | 想法登记簿（`research/ideas.jsonl`，只追加，建议提交到仓库） |
| `python3 scripts/run.py lab vt-check [--vt-bin DIR]` | Vibe-Trading 安装自检 |

来源与许可见 `fwresearch/NOTICE.md`。
