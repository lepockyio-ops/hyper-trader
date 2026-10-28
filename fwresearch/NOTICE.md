# fwresearch — 来源与许可

本目录的部分代码移植或改写自以下项目。移植时保留了原有数学定义，改动了接口以适配本框架（逐笔 R、少标的时间序列 IC），并补充了测试。

## Vibe-Trading（MIT License）

- 仓库：https://github.com/HKUDS/Vibe-Trading
- 参考提交：`d01ea76`（2026-10-28），包版本 `vibe-trading-ai==0.1.18`
- Copyright (c) 2026 Vibe-Trading Contributors

移植内容：

| 本目录 | 上游文件 | 内容 |
|---|---|---|
| `stats.py` | `agent/backtest/validation.py` | Monte Carlo 排列检验、Bootstrap Sharpe 置信区间、滚动分段（walk-forward）一致性 |
| `stats.py` | `agent/src/quantlib/multipletesting.py` | Probabilistic / Deflated Sharpe、期望最大 Sharpe、Benjamini-Hochberg、CSCV 过拟合概率（PBO） |
| `factors/evaluate.py` | `agent/src/factors/factor_analysis_core.py`、`bench_runner.py` | 截面 Rank IC、IC 统计、alive / reversed / dead 分类阈值、不重叠的 h 期前向收益 |
| `factors/ops.py` | `agent/src/factors/base.py` | 时间序列算子的语义（NaN 传播、禁止负位移） |

MIT 许可全文：

```
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## 因子公式

- `factors/zoo.py` 中 `q158_*` 因子按 Microsoft Qlib 的 Alpha158 特征定义重新实现（Qlib 采用 Apache License 2.0，
  https://github.com/microsoft/qlib ，`qlib/contrib/data/handler.py` 的 `Alpha158`）。这里只复用公式定义，没有复制 Qlib 代码。
- `a101_*` 因子按 Z. Kakushadze, "101 Formulaic Alphas"（2015，arXiv:1601.00991）的公式从头实现。
- `crypto_*` 因子为本框架自写。

## 不包含的内容

Vibe-Trading 的大模型 agent、数据加载器、交易连接器、回测引擎都**没有**复制进来。找想法时通过
`mcp-guard` 白名单代理调用你自己安装的 Vibe-Trading（独立虚拟环境），交易类工具一律被代理拒绝。
