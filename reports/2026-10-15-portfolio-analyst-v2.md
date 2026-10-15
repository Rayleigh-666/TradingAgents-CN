# 流程 4 组合级改造：组合分析师 v2.0

日期：2026-10-15　|　工作流：`general_portfolio_health_check`

## 一、为什么要改造

流程 4 原本复用 **4 个单股分析师**（`fundamentals_analyst_v2` / `market_analyst_v2` / `valuation_analyst_v1` / `sector_analyst_v2`）并行分析。它们的 prompt 写的是「`{market_type}` 市场分析师」，绑的工具按**单个股票代码**查数据；而流程 4 的输入是一份**持仓明细**（多只股票）。

结果：这些节点把自由文本「以下持仓组合的健康度体检」当成股票代码去调工具（必然查不到），然后靠 LLM 常识编出**伪组合报告**。

两个更硬的约束：

- **工作流引擎没有循环/遍历节点** —— `NodeType` 只有 start / end / analyst / researcher / trader / risk / manager / condition / parallel / merge / debate / post_processor，全仓 `foreach|loop` 零命中。所以「遍历 5 只持仓、逐只分析、再聚合」**无法用现有工作流表达**。
- **系统里没有可挂工作流节点的组合级分析师** —— `core/agents/adapters/*_v2.py` 全部是单 `ticker` 契约。

因此**「把 holdings 补进 4 个单股节点」这条路是负向的**：它会把「诚实地说不知道」变成「自信地说错」。正确做法是新建真正的组合级分析师。

## 二、改动内容

| 项 | 位置 | 说明 |
|---|---|---|
| A 层状态白名单 | `core/workflow/engine.py` `_build_internal_state_fields` | 新增 `holdings` / `total_position_pct` / `risk_preference` / `benchmark`。**不声明则入图即被 LangGraph 静默丢弃**（不报错、不告警） |
| 新组合适配器 | `core/agents/adapters/portfolio_analyst_v2.py`（新文件） | 模块级纯函数 `parse_holdings` / `compute_concentration` / `compute_portfolio_facts` / `render_portfolio_report` + `PortfolioAnalystV2` |
| 适配器注册 | `core/agents/adapters/__init__.py` | 加 import + `__all__`（漏了 `@register_agent` 不执行） |
| DAG | `scripts/seed_general_workflows.py` `build_portfolio_health_check` | 由「4 单股并行 → MERGE → 风险 → 交易」改为 **`start → a_pf(portfolio_analyst_v2) → end`** |
| DB | Mongo `workflows` | 重播种（幂等 upsert） |
| 单测 | `tests/test_portfolio_analyst_v2.py`（新文件） | 13 例，全离线 |

### 关键设计：绕开 B/C 两层 + 确定性计算

适配器**在 `execute()` 里自己读 `state["holdings"]`**（先例：`adapters/position/technical_analyst_v2.py:163-175` 就是这样读 `position_info` 的）。这一个选择带来三个好处：

1. **不需要改 B 层**（`core/agents/base.py::base_vars`）
2. **不需要改 C 层**（Mongo `prompt_templates` 里 9 个模板文档）
3. 适配器内部用 **Python 循环**遍历持仓 → 绕开了「引擎无循环节点」的硬约束

并且 `execute()` **不调 `super().execute()`、不让 LLM 决定调哪个工具** —— 代码直接调用工具函数拿到真实指标后再渲染。这是刻意的：可验证、无幻觉、且极快。

**代价（已知取舍）**：报告目前是**纯确定性指标**，没有 LLM 写的叙述性体检与调仓建议。facts 与渲染已分离，接 LLM 是增量工作。

### 为什么连下游 `risk_manager_v2` / `trader_v2` 一起删

它们是**单股语义**节点，输入契约读的是**固定报告名**（`market_report` / `investment_plan` / `risky_opinion` 等）。新的组合分析师输出 `portfolio_report` —— 名字对不上，它们**读不到任何上游数据**。保留只会把伪报告从上游挪到下游。组合风险与建议的职责由适配器输出承担（含集中度定性判断、组合风险指标、数据完整性声明）。

## 三、端到端真实产出

> 以下为**实际运行结果**，非示例。任务 `03476b9c-63f8-409f-9d79-8684d020f630`，输入持仓 5 只，`market_type=cn`。

```markdown
# 持仓组合健康度体检

分析日期：2026-10-15　|　市场：cn　|　当前总仓位：0.0%　|　风险偏好：平衡　|　业绩基准：沪深300

> 本报告仅包含由本地行情库真实数据计算得到的指标；任何无法计算的项均明确标注为「不可用」并给出原因，不含推测数值。

## 一、持仓明细

| 代码 | 名称 | 权重(%) | 成本价 |
| --- | --- | --- | --- |
| 600519 | — | 30 | 1500 |
| 000858 | — | 25 | 180 |
| 300750 | — | 20 | 220 |
| 601318 | — | 15 | 45 |
| 002594 | — | 10 | 250 |

## 二、集中度分析

- 第一大标的权重：30.00%
- 前三大合计：75.00%
- 前五大合计：100.00%
- 赫芬达尔指数 HHI：0.2250（2250.0 点，0~1 小数口径）
- 定性判断：中度集中（需关注前几大标的的共振风险）
- 权重口径：按你提供的权重计算（已归一化到合计 100%）
- 原始权重合计：100.00%

## 三、组合风险指标（有效前沿）

**最大夏普比率组合**

- 年化收益：4.73%
- 年化波动率：25.92%
- 夏普比率：0.086

**最小波动组合**

- 年化收益：-8.42%
- 年化波动率：17.97%

- 参与计算的标的：600519, 300750, 601318, 002594
- 重叠交易日：240 个

## 四、数据完整性声明

- 持仓标的数：5
- 重叠交易日：240 个
- 被排除标的数：1
- 各标的本地日线根数（数据可用性证据）：
    - 600519：241 根
    - 000858：0 根
    - 300750：241 根
    - 601318：241 根
    - 002594：241 根

**因数据不足被排除的标的：**

- 000858：000858 数据不足（需要至少30个交易日），已排除。

**告警与降级说明：**

- 000858 数据不足（需要至少30个交易日），已排除。
- scipy 未安装，有效前沿使用蒙特卡洛模拟的近似结果。

## 五、说明

- 组合风险指标基于马科维茨均值-方差模型（最大夏普/最小波动），由本地历史日线计算得到，不含前瞻或推测数值。
```

**数据溯源**：集中度（topN / HHI）由 `compute_concentration` 对真实权重纯 Python 计算；风险指标来自 `get_efficient_frontier` 对**本地行情库**的实时计算（不联网）；`240` 个重叠交易日、各标的 `241/0/241/241/241` 根日线均为本地库实读。被排除的 `000858` 有明确原因，**报告内无一处编造数值**。

## 四、顺带发现的既有缺陷：两个同名函数口径不一致

`get_stock_daily_quotes` 在仓里有**两份实现**，结果不同：

```
core/tools/external/local_data.py:80          -> 600519 返回 183 根
core/skill_runtime/data_access.py:390         -> 600519 返回 241 根   <-- 正确
```

**根因**（`local_data.py:102-109`）：它把日期 `replace("-", "")` 成 `20250901` 再去做 `$gte/$lte`，但库里 `trade_date` **实际存的是 `2026-09-01`（带连字符）**。Mongo 按**字典序**比较字符串：`-`(0x2D) < `0`(0x30)，于是整段记录被**静默过滤** —— 查询成功、返回正常，只是少了一截数据。

**影响面**：

- 生产代码**零处调用**该版本 —— `quant_tools.py:17` 及所有技术指标工具（`volume_analysis` / `candlestick_patterns` / `support_resistance` / `fibonacci_tools` / `advanced_technical` / `crossover_signals` / `chart_patterns`）都用 `skill_runtime` 版本。
- 真实风险：`core/tools/external/code_generator.py:73` 与 `stock_data_catalog.py:161` 把 `local_data` 版本**推荐给 AI 生成的 skill**，那些生成物会静默丢数据。

**本轮处置**：`portfolio_analyst_v2` 使用 `skill_runtime` 版本（与 `get_efficient_frontier` 同源）—— 这是**必须**的，否则报告里的「各标的日线根数」会与「重叠交易日」自相矛盾。`local_data` 本体的 bug 未修，建议单独立项。

## 五、验证结果

| 验证 | 结果 |
|---|---|
| E2E 流程 4 | **17/17 通过**；`portfolio_report` 1106 字符、无降级标志 |
| E2E 流程 1（回归） | **20/20 通过**（62.4s）—— 证明 A 层改动没破坏其他流程 |
| 后端回归 | **115 passed / 0 failed**（8 个文件，含新增 13 例） |
| 语法 / 导入 / 注册 | 全部通过；`registry.is_registered("portfolio_analyst_v2") == True` |

> 流程 4 的 E2E 完成时间为 `t=0.0s`（对比原本 4 个 LLM 节点的 46.4s）。这是**预期行为** —— 适配器纯代码计算、不调 LLM，不是「假成功」。已通过读取报告原文确认内容真实。

## 六、剩余待办

1. **叙述性报告未接 LLM** —— 当前是纯确定性指标表，没有 LLM 写的体检叙述与调仓建议（facts 与渲染已分离，增量即可）。
2. `total_position_pct` 未传时报告显示 `0.0%` 而非「未提供」—— 应区分「缺省」与「真的是 0」。
3. `local_data.get_stock_daily_quotes` 的日期过滤 bug（含 code_generator 推荐链）未修。
4. 港股 / 美股持仓支持未做（当前明确提示仅支持 A 股，因底层 `zfill(6)` 对非 A 股会出错）。
5. `000858` 等标的本地行情缺失（0 根）属数据同步问题，非代码问题。
6. **本会话所有改动仍未 commit。**
