"""fwresearch：框架的研究前端（找想法、筛因子、统计检验）。

与具体策略、具体交易所无关，hyper-trader 与 freq-trader 两个模板共用同一份代码。
只依赖 numpy / pandas / 标准库；不联网、不读密钥、不下单。

  python -m fwresearch factors ...     筛因子：内置因子库（可选接 Vibe-Trading 全量因子库）算 IC / IR，分 alive / reversed / dead
  python -m fwresearch stats ...       统计检验：逐笔 R 的 Bootstrap 区间、符号翻转检验、Monte Carlo 排列、滚动分段
  python -m fwresearch matrix ...      研究矩阵的多方案检验：Deflated Sharpe（按尝试次数折扣）与 PBO（过拟合概率）
  python -m fwresearch idea ...        想法登记簿：假设 → 筛因子 → 回测 → 统计检验 → 人工批准写进策略规格
  python -m fwresearch mcp-guard ...   Vibe-Trading MCP 的白名单代理（只放行研究类工具）
  python -m fwresearch vt-check        Vibe-Trading 安装自检（版本、因子库桥接、MCP 握手、放行与拒绝、密钥过滤）

部分代码移植自 Vibe-Trading（MIT），见 NOTICE.md。
"""
__version__ = "1.1.0"
