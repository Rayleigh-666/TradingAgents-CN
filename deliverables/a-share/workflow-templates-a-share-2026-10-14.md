# A 股主流可落地分析工作流样板清单（通用研究工作流选型）

**日期**：2026-10-14
**报告类型**：通用研究工作流（general）选型与落地方案
**分析标的/主题**：A 股市场主流分析工作流 · 7 大领域 · 21 个候选样板
**参与成员**：洪远见（宏观策略师）、潘明哲（市场解读师）、颜一深（个股研究员）、贾不贵（估值定价师）、连拓远（产业链分析师）、金向北（资金行为分析师）、方有安（风险诊断师）

---

## 📌 TL;DR（一句话核心结论 + 3 行依据）

- **核心结论**：21 个候选样板中，建议**首批只做 4 条**——「每日市场复盘 · 盘面日报」「市场情绪周期定位 · 仓位择时」「北向 · 两融增量资金面监控」「持仓组合健康度体检」。它们数据缺口最小、实现难度中低，且都落在平台现有默认流程**没有覆盖的层面**（盘面层 / 周期层 / 组合层）。
- **关键依据**：
  ① 平台已接入的是**日频结构化数据**（get_market_overview / get_limit_stats / get_market_breadth / get_north_flow / get_margin_trading / get_sector_data / get_historical_valuation_percentile_tool 等），因此盘面、情绪、资金三类样板的数据缺口最小；
  ② 差异化才有价值：平台默认「单股研究」已是 16 节点全量流程、「持仓分析」已有单标的管线（pa_technical_v2 等），新样板必须落在**盘面 / 周期 / 组合 / 主题**这些空白层；
  ③ 五处硬缺口已确认：**基金重仓与超配明细、减持/质押/解禁明细、炸板率明细、基金申赎、分地区营收与一致预期**——凡强依赖它们的样板一律不进首批。
- **建议动作**：🔵 分批推进 —— 首批 4 条先落地（其中 2 条为低难度，可在数日内跑通端到端），第二批 5 条在补齐代理指标口径与"传导节点"公共组件后排期。

---

## 🎯 核心结论卡片

| 项目 | 内容 |
|---|---|
| 候选样板总数 | 21 条（7 个领域 × 各 3 条） |
| 首批建议实现 | 4 条 · 盘面日报 / 情绪周期择时 / 北向两融监控 / 持仓组合体检 |
| 第二批 | 5 条 · 主线识别追踪 / 个股财务地雷排查 / 政策事件传导推演 / 产业链全景拆解 / 财报深度体检 |
| 暂缓 | 12 条（依赖缺口数据或触发频率过低） |
| 不建议实现 | 6 条（实时盘中类 3 条 + 择时预测/目标价类 2 条 + 无行动结论的仪表盘类 1 条） |
| 统一口径（强制） | ①「拥挤度」一律写为「资金拥挤度代理（资金流入持续性 + 估值分位）」；② 传导节点枚举固定为「一阶受益 / 二阶受益 / 反向受损」；③ 市场宽度双口径 `breadth_all`（含 ST 次新，供情绪）与 `breadth_core`（剔除，供资金） |
| 落地抓手 | 组合集中度 + 商誉/应收/现金流排雷 + 拥挤度代理 + 双口径宽度 |

---

## 一、候选样板总表（21 条）

### 1) 盘面解读领域 · 潘明哲

| # | 样板名称 | 解决什么问题 | 频率 | 难度 | 数据缺口 |
|---|---|---|---|---|---|
| P1 | 每日市场复盘 · 盘面日报 | 收盘一屏看清指数+量能+情绪+板块+风格 | 日频 | 低 | **无** |
| P2 | 市场主线识别 · 强势方向追踪 | 区分核心主线 vs 一日游脉冲并定位龙头 | 周频 + 事件 | 中 | 龙头/连板辨识需自建规则后处理 |
| P3 | 市场情绪周期定位 · 仓位择时 | 冰点/修复/主升/退潮定位 + 仓位区间 | 日→周 | 中 | **无硬缺口** |

### 2) 风险诊断领域 · 方有安

| # | 样板名称 | 解决什么问题 | 频率 | 难度 | 数据缺口 |
|---|---|---|---|---|---|
| R1 | 持仓组合健康度体检 | 组合层四维体检 + 健康度评级 + 调仓建议 | 日/周 | 中 | **无** |
| R2 | 个股财务地雷排查 | 商誉/应收/存货/现金流排雷 | 事件触发 | 中 | 存货质量、分部/主营拆解无专门工具 |
| R3 | 泡沫与拥挤度雷达 | 基本面驱动 vs 预期透支 + 拥挤度代理 | 周/事件 | 中 | 拥挤度依赖代理指标口径 |

### 3) 产业链主题领域 · 连拓远

| # | 样板名称 | 解决什么问题 | 频率 | 难度 | 数据缺口 |
|---|---|---|---|---|---|
| I1 | 产业链全景拆解与核心环节定位 | 上中下游拆解 + 核心价值环节锁定 | 按需/事件 | 中 | **无** |
| I2 | 题材标的分层与龙头卡位 | 核心龙头/中军/高弹性/观察性分层 | 事件驱动 | 中 | **无** |
| I3 | 产业链出海逻辑叠加筛选 | 出海方向叠加筛选 | 按需 | 中 | 分地区营收无专门工具 |

### 4) 资金行为领域 · 金向北

| # | 样板名称 | 解决什么问题 | 频率 | 难度 | 数据缺口 |
|---|---|---|---|---|---|
| M1 | 北向 · 两融增量资金面监控 | 判断增量入场还是存量博弈 + 风格/仓位倾向 | 日频 | **低** | 北向仅日频、两融 T+1 |
| M2 | 板块资金轮动共振扫描 | 找北向×主力×两融共振板块，区分趋势 vs 一日游 | 周频 | 中 | 北向行业明细降频；**无基金重仓/超配数据**（拥挤度用代理） |
| M3 | 个股资金异动体检 | 异动时判资金性质（吸筹/派发/杠杆/情绪脉冲） | 事件 | 中 | **无龙虎榜/十大股东/基金持仓**，机构身份无法确认 |

### 5) 个股研究领域 · 颜一深

| # | 样板名称 | 解决什么问题 | 频率 | 难度 | 数据缺口 |
|---|---|---|---|---|---|
| S1 | 财报深度体检工作流 | 季报/年报后判断利润含金量与暴雷隐患 | 季报/年报后 | 中 | 存货质量、分部/主营拆解无专门工具 |
| S2 | 业绩预告/快报事件驱动简报 | 预告/快报窗口速评预期差与爆雷 | 事件 | 中 | **无卖方一致预期**，预期差只能同比环比近似 |
| S3 | 个股质地评分与投资逻辑卡 | bottom-up 建档出评分卡与一页纸逻辑 | 按需 | 中 | 管理层/治理质量缺结构化数据 |

### 6) 估值定价领域 · 贾不贵

| # | 样板名称 | 解决什么问题 | 频率 | 难度 | 数据缺口 |
|---|---|---|---|---|---|
| V1 | 个股合理估值区间测算 | 当前估值在历史什么位置、靠什么消化 | 按需/季报后 | 中 | 一致预期缺失，只能靠历史分位与增速外推 |
| V2 | 同业相对估值对比与性价比排序 | 同赛道里谁贵谁便宜、溢价是否合理 | 按需 | 低-中 | **无** |
| V3 | 红利资产属性与股东回报质量评估 | 分红可持续性与现金流支撑 | 年报后 | 中 | 分红明细依赖财报附表 |

### 7) 宏观策略领域 · 洪远见

| # | 样板名称 | 解决什么问题 | 频率 | 难度 | 数据缺口 |
|---|---|---|---|---|---|
| A1 | A股宏观周期定位与风格轮动研判 | 当前宏观周期在哪一段、对应什么风格 | 周频 | 中 | 无宏观专用工具，靠指数/板块数据替代 |
| A2 | 政策/事件冲击的行业传导路径推演 | 政策或事件落地后，谁一阶受益、谁二阶受益 | 事件驱动 | 中 | 无宏观专用工具，靠 news + 指数/板块数据替代 |
| A3 | 隔夜海外映射 · A股盘前前瞻 | 隔夜外盘波动对 A 股哪些方向有映射 | 日频(盘前) | 中 | 无海外数据工具，依赖联网检索 |

> 全部 21 条标题均为成员原始交付标题。

---

## 二、首批推荐（4 条 · 含落地方案）

### 首批 1：每日市场复盘 · 盘面日报

**为什么首批做**：难度最低、数据零缺口、日频刚需（使用频率决定留存），平台工具已全覆盖，几乎纯数据搬运 + 模板化输出，投产比最高。

**节点设计**
```
START
 → TOOL  data_preparer_v2
         (指数/涨跌家数/涨跌停/连板高度/成交额/北向/两融/板块涨跌)
 → PARALLEL [ index_analyst_v2 | emotion_analyst_v2 | sector_analyst_v2 | market_analyst_v2 ]
 → MERGE → ANALYST timing_analyst_v2   (情绪微观数据 → 周期阶段映射)
 → MANAGER review_manager_v2            (汇总结构化解盘)
 → TOOL  report_generator_v2
 → END
```

**input_config.fields**
| name | label | type | required | 说明 |
|---|---|---|---|---|
| trade_date | 交易日 | date | 是 | 默认最近交易日 |
| watch_sectors | 关注板块 | multiselect | 否 | 命中则高亮 |
| watchlist | 自选股 | textarea | 否 | 输出联动提示 |
| research_depth | 分析深度 | select | 否 | 快速/标准/深度，默认标准 |
| quick_analysis_model | 快速分析模型 | select | 否 | 动态填充可用 LLM |
| deep_analysis_model | 深度分析模型 | select | 否 | 动态填充可用 LLM |

---

### 首批 2：市场情绪周期定位 · 仓位择时

**为什么首批做**：A 股特色方法论且被广泛使用，所需指标全为可得日频数据，平台 `identify_market_cycle` 已内置周期判断；三方风险辩论节点天然适配"当前阶段"这种有分歧的判断。

**节点设计**
```
START
 → TOOL  data_preparer_v2
         (涨停家数/连板高度/跌停家数/成交额/昨涨停今日溢价/两融序列
          + 市场宽度双口径 breadth_all / breadth_core)
 → PARALLEL [ emotion_analyst_v2 | timing_analyst_v2 | market_analyst_v2 | chip_distribution_analyst_v2 ]
 → MERGE → DEBATE [ risky_analyst_v2 ↔ safe_analyst_v2 ↔ neutral_analyst_v2 ]  (1–2 轮)
 → MANAGER risk_manager_v2   (收敛为仓位区间 + 风险提示)
 → TRADER  trader_v2         (可读择时简报)
 → END
```

**input_config.fields**
| name | label | type | required | 说明 |
|---|---|---|---|---|
| window_days | 观察窗口(日) | number | 否 | 默认 20 |
| current_position_pct | 当前仓位(%) | number | 否 | 用于给出调整建议 |
| breadth_scope | 涨跌家数口径 | select | 否 | 全市场含 ST / 剔除 ST 次新，默认全市场 |
| research_depth | 分析深度 | select | 否 | 默认标准 |

---

### 首批 3：北向 · 两融增量资金面监控

**为什么首批做**：难度低，`get_north_flow + get_margin_trading + get_market_breadth` 直接可得；**平台没有专职"资金面"角色**，正好用「提示词定制（agent_reshaping）」把通用分析师改写为资金面解读——这是验证该机制的最佳试点。

**节点设计**
```
START
 → TOOL  data_preparer_v2  (北向净流入序列/两融余额与结构/行业资金流/涨跌家数)
 → PARALLEL [ index_analyst_v2 | sector_analyst_v2 | market_analyst_v2 ]
            ↑ 三者统一做提示词定制：由"指数/板块/技术面"改写为"资金面解读"
              （成交量价配合、杠杆资金变化、行业资金流向）
 → MERGE → MANAGER research_manager_v2   (增量 vs 存量判定 + 风格与仓位倾向)
 → TRADER  trader_v2
 → END
```

**input_config.fields**
| name | label | type | required | 说明 |
|---|---|---|---|---|
| lookback_days | 回溯天数 | number | 否 | 默认 20 |
| focus_sectors | 关注行业 | multiselect | 否 | 缩窄资金流分析范围 |
| include_margin | 纳入两融 | boolean | 否 | 默认 true |

**口径约束**：北向仅日频、两融 T+1，输出必须标注"截至 T-1"，禁止表述为实时信号。

---

### 首批 4：持仓组合健康度体检

**为什么首批做**：差异化最强——平台已有的是**单标的**持仓管线（pa_technical_v2 / pa_fundamental_v2 / pa_risk_v2 / pa_advisor_v2），**组合层**（集中度、相关性、风险预算）完全空白；所需工具齐全、无缺口。

**节点设计**
```
START
 → TOOL  data_preparer_v2  (解析持仓/权重/成本 + 组合层数据)
 → PARALLEL [ 逐标的: fundamentals_analyst_v2 | market_analyst_v2 | valuation_analyst_v2 ]
   ∥ PARALLEL [ 组合层: sector_analyst_v2 (行业集中度/相关性/风格暴露) ]
 → MERGE → MANAGER risk_manager_v2  (四维汇总 + 逐只健康度评级)
 → CONDITION 是否存在高风险标的
      ├─ true  → DEBATE [ risky_analyst_v2 ↔ safe_analyst_v2 ] + neutral_analyst_v2 (仅争议标的, 1 轮)
      └─ false → 直连
 → TRADER  pa_advisor_v2  (调仓建议：总仓位区间 + 加减仓条件)
 → TOOL  report_generator_v2
 → END
```

**input_config.fields**
| name | label | type | required | 说明 |
|---|---|---|---|---|
| holdings | 持仓数据(JSON) | textarea | 是 | 代码/名称/权重/成本 |
| total_position_pct | 当前总仓位(%) | number | 否 | 用于仓位建议 |
| risk_preference | 风险偏好 | select | 否 | 保守/平衡/激进 |
| benchmark | 业绩基准 | select | 否 | 沪深300/中证500/中证1000 |

---

## 三、第二批（5 条 · 简述）

| # | 样板 | 节点要点 | 排期前置条件 |
|---|---|---|---|
| P2 | 市场主线识别 · 强势方向追踪 | data_preparer_v2 → 并行(sector/market/news/social) → **2 轮多情景辩论(bull↔bear)** → research_manager_v2 → report_generator_v2 | 先落地"龙头/连板辨识"规则后处理（晋级率、龙头打分） |
| A2 | 政策/事件冲击的行业传导路径推演 | data_preparer_v2 → 并行(macro 视角 index_analyst_v2 + news_analyst_v2 + sector_analyst_v2) → 辩论(bull↔bear 就"传导强度/时滞"分歧) → research_manager_v2 → report_generator_v2 | 复用 I1 的「传导节点」枚举（见行动清单 #3），与 I1 同步设计 |
| R2 | 个股财务地雷排查 | data_preparer_v2 → 并行(fundamentals/goodwill/receivables/cashflow 工具组) → risk_manager_v2 → report_generator_v2 | 与 S1 划清边界：R2 是**风险触发式排雷**，S1 是**研究式利润含金量建档**，共用工具但提示词与输出不同 |
| I1 | 产业链全景拆解与核心环节定位 | data_preparer_v2 → 并行(sector/news) → 辩论(bull↔bear 就"核心环节"分歧) → research_manager_v2 | 复用 A2 的「传导节点」枚举口径，一次设计两处复用 |
| S1 | 财报深度体检工作流 | data_preparer_v2 → 并行(三表分析工具组 + fundamentals_analyst_v2) → 辩论 → research_manager_v2 | 需先定义"利润含金量"评分口径；存货/分部拆解缺口需接受降级 |

---

## 四、暂缓与不建议

### 暂缓（12 条：依赖缺口数据或触发频率过低）
V1 个股合理估值区间测算、V2 同业相对估值对比与性价比排序、V3 红利资产属性与股东回报质量评估、A1 A股宏观周期定位与风格轮动研判、A3 隔夜海外映射·A股盘前前瞻、M2 板块资金轮动共振扫描、M3 个股资金异动体检、I2 题材标的分层与龙头卡位、I3 产业链出海逻辑叠加筛选、S2 业绩预告/快报事件驱动简报、S3 个股质地评分与投资逻辑卡、R3 泡沫与拥挤度雷达。

> 说明：并非价值低，而是**要么依赖缺口数据、要么触发频率低**（如 A3 海外映射），建议在首批跑通、缺口数据源评估完成后再排期。

### 不建议实现（6 条 · 成员一致识别的伪需求）

| 伪需求 | 为什么不做 |
|---|---|
| 实时盘中情绪监控看板 | 平台数据为日频/日更收盘口径，做不出真·分时情绪，只能给盘后近似值，价值虚高 |
| 涨停预测 / 明日打板信号 / 连板接力 | A 股打板高度博弈化，依赖盘口与瞬时资金行为，模型无法稳定复现，易误导 |
| 实时主力资金流向异动 | 资金数据为低频（北向日频、两融 T+1），"实时"是伪命题 |
| 分钟级盘中破位预警 | 无分钟级数据源；且本平台定位是研究而非交易执行 |
| 目标价 / 股价预测 | 无一致预期数据支撑，任何点位预测都是伪精确 |
| 纯情绪打分仪表盘（无行动结论） | 不接主线与仓位就是"好看没用"，必须落到"方向 + 仓位"才有产品价值 |

---

## 五、general 流程如何落到 UI（落地方式确认）

**结论：不需要新加页面，直接借用现有「通用研究」页。**

现有页面 `views/Analysis/GeneralAnalysis.vue`（路由 `/analysis/general`，侧边栏「通用研究」，菜单项硬编码于 `SidebarMenu.vue:38`）**就是 general 流程的宿主/播放器**：它请求 `GET /api/workflows`，只保留 `workflow_type === 'general'` 的流程，再按该流程的 `input_config.fields` 动态渲染表单。因此**往 `workflows` 集合写一条文档 = UI 立即可用，前端零改动**。

### 用户操作路径（现状已实现）

| 步骤 | 界面行为 | 依赖 |
|---|---|---|
| 1 | 左侧导航「通用研究」→「新分析」标签页 | 菜单项已存在 |
| 2 | 「流程选择」下拉列出全部 general 流程（`名称 v版本`，`is_default` 带「默认」标签）；有默认则自动选中，否则选第一条；选中后显示流程 `description` | `GET /api/workflows` + 前端过滤 |
| 3 | 「执行参数」区按 `input_config` 自动生成表单：按 `group` 分组、按 `order` 排序 | `GET /api/workflows/{id}` |
| 4 | 控件按 `field.type` 渲染：`string` 单行 / `textarea` 多行 / `number` 数字 / `boolean` 开关 / `date` 日期 / `select` 单选 / `multiselect` 多选；每字段下方渲染 `description`，必填打红星并提交前校验 | 渲染器支持上述 7 种 |
| 5 | 点「开始分析」→ 组装参数（无 `ticker` 时用 `analysis_target` 复用为 ticker，其余并入 `custom_params`）→ 提交 | `POST /api/workflows/{id}/execute` |
| 6 | 执行区显示任务ID（可复制）、分析目标、开始时间、已用时间、进度条与当前步骤 | 轮询 `GET /api/analysis/tasks/{id}/status` |
| 7 | 完成后「查看结果」跳**任务中心**；「历史结果」标签页可查历史（按流程/时间/关键字/状态筛选） | `/api/v2/tasks/general/list`（依赖后端打的 `task_category=general`） |

### 两处必须知道的前端细节

1. **模型字段是特例**：字段名恰为 `quick_analysis_model` / `deep_analysis_model` 时，选项**自动用系统已启用的 LLM 列表填充**，流程里不必写 `options`；其他 `select`/`multiselect` 才需要在 `input_config` 里显式给 `options` + `option_labels`。
2. **`textarea` 硬编码 500 字上限**（`GeneralAnalysis.vue:123`）。`analysis_target` 若用 `textarea`，超过 500 字会被截断——长文本场景需放宽该限制或改用 `string` 类型。

### 什么情况下才需要动前端

| 情况 | 做法 |
|---|---|
| 用到的字段类型不在上述 7 种内（file / 级联 / 富文本等） | 扩展 `GeneralAnalysis.vue` 的渲染分支 |
| 想让结果页呈现专属可视化（分层表、雷达图、图表），而不是只在任务中心看 Markdown | 写专属结果组件（当前页面本身不渲染结果） |
| 想让某条流程有独立入口 / 独立看板（如「盘面日报」做成 Dashboard 卡片一键直达） | 那就是新增页面/组件，而非借用通用研究页 |
| 想创建/编辑流程 | 社区版设计器被裁（`/workflow` 属 Pro 网关路由、`pro.routes.ts` 为空），当前只能经 API 或直接写库 |

### 社区版限制（影响样板落地方式）

- **定时分析页在社区版不存在**：`/settings/scheduled-analysis` 只出现在 Pro 路由网关名单（`router/index.ts:780`），前端无对应组件。后端侧 `app/worker/watchlist_analysis_task.py::_execute_general_workflow_slot` 已支持按时段执行 general 流程并透传 `workflow_id`/`analysis_target`，**只是没有 UI 入口**。因此"盘面日报定时自动跑"在社区版需自建入口或走 API。

---

## ✅ 行动清单

| # | 行动 | 触发条件 | 时间窗 |
|---|---|---|---|
| 1 | 先落库并跑通 2 条低难度样板（P1 盘面日报、M1 北向两融监控）作为端到端验证 | 本报告确认后 | 本周内 |
| 2 | 抽出**公共后处理组件库**：市场宽度双口径（breadth_all/breadth_core）、涨跌停族（涨跌停家数/连板高度/溢价）、拥挤度代理 | 首批开发启动前 | 3 日内 |
| 3 | 冻结「传导节点」数据结构（触发变量 / 承接环节 / 一阶·二阶·反向受损 / 时滞 / 验证指标 / 数据工具），宏观与产业链共用 | A2 与 I1 开发前 | 1 周内 |
| 4 | 评估补齐 5 处缺口数据源（基金重仓与超配、减持质押解禁、炸板率、基金申赎、分地区营收 + 一致预期） | 第二批排期前 | 2 周内 |
| 5 | 每条 general 流程补齐 `input_config`（含 `analysis_target` 必填）并加最小回归用例 | 每次落库时 | 持续 |

---

## 🚀 落地状态（首批 4 条已实现并验证）

落库脚本 `scripts/seed_general_workflows.py`（幂等 upsert，带 `--dry-run` / `--verify`），已写入 MongoDB `tradingagents.workflows`：

| 流程 id | 名称 | 节点/边 | is_default |
|---|---|---|---|
| `general_daily_market_review` | 每日市场复盘 · 盘面日报 | 10 / 12 | ✅ true |
| `general_market_emotion_cycle` | 市场情绪周期定位 · 仓位择时 | 14 / 19 | false |
| `general_capital_flow_monitor` | 北向 · 两融增量资金面监控 | 9 / 10 | false |
| `general_portfolio_health_check` | 持仓组合健康度体检 | 10 / 12 | false |

4 条文档的 `_id` 均写成与 `id` 相同的**字符串**（绕开 `app/routers/workflows.py:274` 用字符串查 ObjectId `_id` 必然 miss、导致 `workflow_type` 退化成 `stock_analysis` 的坑）。

**独立验证结论（实测，非推断）**
- 4 条流程 `WorkflowBuilder().build()` **4/4 编译成功**，**26 个 agent 节点 0 占位**。
- 反向对照有效：把估值节点改回不存在的 agent 会退化为 `placeholder_node`（`fn({})` 返回 `{"valuation_analyst_v2_report": "[...] 智能体未实现"}`）。
- `pytest tests/test_general_workflow_seed.py -v` → **10 passed, 0 skipped**（含新增守卫）。
- 新增守卫 `test_all_agent_ids_resolvable`：要求每个 `agent_id` 在**代码注册表**或 **DB `agent_configs` 门槛查询**中可解析，失败时列出全部 `(workflow_id, node_id, agent_id)` 三元组。⚠️ 该用例若因 Mongo 探活失败而 skip，**不算通过**。

**修掉的一个真实降级**：流程 4 的估值节点原写 `agent_id="valuation_analyst_v2"`，而该 agent 两个通道都不存在 → 会静默降级为占位文本，持仓体检缺整个估值维度。已改为 **`valuation_analyst_v1`**（依据：平台自有流程 `6ede3bc3-…`、`bd44e1fb-…` 在需要估值分析师时用的就是它；且它带 DCF / 历史估值分位 / 同业对比四个工具与 4230 字已审核提示词），并显式钉住 `config.output_field="valuation_report"`。

### ⚠️ 写这类流程时必须知道：节点 `config` 里多数键是**死配置**

`AgentConfig`（`core/agents/config.py:207-238`）未设 `extra="forbid"`，pydantic 默认静默丢弃未知键。全仓库**无读取点**的装饰性键：**`focus`**、`role`、`stance`、`description`、`rounds_ref`。真正被消费的只有 `output_field` / `temperature` / `timeout` / `max_tokens` / DEBATE 的 `participants`·`rounds` / MERGE 的 `strategy` / MANAGER 的 `delegated_agent_ids`。

⇒ 本批流程里写的 `config={"focus": "…"}` **只起注释作用，改不了 agent 职责**。

### 待办：流程 3 的「资金面解读」职责改写

需要落到 `prompt_templates` 集合（**流程级**作用域，先例：`install/community_bootstrap.json` 里 7 条 `workflow_id="v2_etf_analysis"` 的模板），即新增 `workflow_id="general_capital_flow_monitor"`、`agent_type="analysts_v2"`、`agent_name∈{index_analyst_v2, sector_analyst_v2, market_analyst_v2}`、`node_id=None`、`status="active"` 的模板。**节点级**（`node_id`）定制能力代码存在但标准流程运行时未接线，当前用不上。改 `agent_configs` 的提示词字段**不生效**。

---

## ⚠️ 风险提示 & 关键观察指标

**需要验证的**
- 首批 2 条低难度样板的实际端到端耗时与 Token 消耗，确认"日频高频使用"在经济上可承受。
- 双口径市场宽度（含/不含 ST 次新）对情绪判断的实际区分度——若两口径结论长期一致，可合并为一个。

**可能证伪的信号**
- 若后续接入**实时行情 / 基金持仓 / 龙虎榜 / 一致预期**任一数据源，本报告中"难度低"的结论与部分"不建议"的判定需要重估（尤其"涨停预测""目标价"两类）。
- 若"龙头/连板辨识"规则后处理无法稳定复现人工识别结果，P2 主线识别的价值需下调，应退回纯板块强度排名。

**已知硬约束（不可绕过）**
- 所有结论基于公开数据，日频/日更口径，不含实时与盘中数据。
- 「拥挤度」只能表述为代理指标，不得声称是真实基金拥挤度。
- A 股 T+1、涨跌停制度下，任何"日频信号"到"执行"之间必然存在时滞。

---

## 📚 数据来源 & 成员产出索引

- **代码侧核查**：`core/agents/config.py`（21 个 v2 角色元数据）、`app/services/workflow_registry.py`（task_type → 默认工作流映射）、工具清单（tool_agent_bindings 82 条绑定）、节点原语（`core/workflow/models.py`）
- **洪远见（宏观策略师）**：A1–A3 样板 + 传导节点字段口径
- **潘明哲（市场解读师）**：P1–P3 样板 + 情绪周期工程化规则
- **颜一深（个股研究员）**：S1–S3 样板 + 与平台默认单股研究流程的边界划分
- **贾不贵（估值定价师）**：V1–V3 样板 + 分红/现金流依赖说明
- **连拓远（产业链分析师）**：I1–I3 样板 + 传导节点空间结构（上中下游）与拥挤度代理口径
- **金向北（资金行为分析师）**：M1–M3 样板 + 双口径市场宽度公共节点、资金面职责归口
- **方有安（风险诊断师）**：R1–R3 样板 + 泡沫反身性与逐只健康度评级口径

---

> ⚠️ 以上内容由 AI 基于公开信息整理生成，仅供参考，不构成任何投资建议或个股推荐。投资有风险，决策需谨慎。
