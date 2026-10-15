# 第八轮交叉 Review 报告（2026-10-15）：第二批修复 + 组合分析师 v2

- **对象**：第七轮 Review 之后的第二批改动（round7 P1/P2/P3 修正收口 + 流程 4 组合级改造）
- **基线**：HEAD = `24ce32d6`，全部未提交
- **方式**：本人逐段核对核心 diff + 2 路并行 Review（工具层 / 组合分析师）+ 全量回归实测 + 关键声明复跑验证
- **测试**：新增 8 文件 **115 passed**、历史契约 8 文件 **87 passed**、前端 vitest **13 passed**、`vue-tsc` exit 0 —— 测试面全绿
- **结论**：**P0 = 0；P1 = 2（均在 portfolio_analyst_v2.py，合入前必修）；修改方案整体合理，round 7 的三大虚假判断来源已被结构性消除，但本批新引入了一条「用真实数据算出误导性报告」的路径**

---

## 一、关键声明的亲自复验（不是纸面信任）

| 声明 | 复验结果 |
|---|---|
| `resolve_instrument` 行为（13 组输入） | ✅ 与文档逐字一致：`700→('hk','00700')`、`0700.HK→('hk','00700')`、`AAPL.O→('us','AAPL')`、`BRK-B→('us','BRK.B')`；`MARKET/__GENERAL__/自由文本/''→('','')` |
| 修正报告称原哨兵 `"MARKET"` 不命中美股正则（我第七轮 P2-1 归因错误） | ✅ **修正报告是对的**：`identify_stock_market("MARKET")=UNKNOWN`（6 个字母），`guess_market("MARKET")='us'`——真因是「含字母→us」规则，不是 `^[A-Z]{1,5}$` |
| 我第七轮 P1-5 建议「UNKNOWN 时补 guess_market」会引入回归 | ✅ **我的建议确实错了**：实测 `guess_market('A股市场当日盘面复盘')='us'`，照做会把流程 1 新闻链打成美股。已实施的「先形态判断（looks_like_instrument_code）再定市场」是正确方案 |
| `random.uniform()` 假数据已清除 | ✅ 两个 provider 的 `_generate_fallback_data` 均改为「⛔ 本维度数据不可用 + 请勿推断」，`import random` 删除，全仓零残留 |
| favorites mock 方法删除无断裂 | ✅ grep 零调用方 |
| e2e `DEGRADED_MARKERS` 未被新文案误触 | ✅ 标志列表仍是 5 个真实失败串；组合报告里诚实的「不可用」不会假阳性 |

---

## 二、P1（合入前必修，均在 `core/agents/adapters/portfolio_analyst_v2.py`）

### P1-1 `parse_holdings` 兜底会把畸形输入解析成「幻影证券」（本人实测复现）
`resolve_instrument` 接受 1-5 位字母/数字为合法代码形态，而 parse 兜底对弱形态 token 无约束：

| 输入 | 实际解析 |
|---|---|
| `not json at all` | → 美股 `NOT`，name=`json at all`，weight=None |
| `30, 1500`（备注短数字） | 1500 越界整行丢弃，但 `30` 这类行会 → 港股 `00030` |
| `aa,600519,30` | `aa` 抢占代码位，真代码 600519 被当权重 → 整行以「权重非法」丢弃，归因失真 |

用户粘贴的备注行、带短英文标签的行都会命中，**凭空造出持仓标的**。
**修法**：两轮定位——第一轮只认强形态（6 位数字、带点号/交易所后缀、显式 `.HK`）；第二轮兜底对弱形态 token 要求「剩余 token 能解释为权重/成本」或「本地库查得到」，否则 warning 跳过。

### P1-2 单行误解析一票否决整包有效前沿，且归因事实错误（本人实测复现）
`markets` 集合含任一幻影 us/hk 标的 → `non_cn=True` → 整包跳过有效前沿，报告写「检测到非 A 股标的」。实测 `600519,30,1500` + 一行 `not json at all` → EF 消失，而用户实际全是 A 股。有效前沿是本报告核心价值，一行垃圾输入就整体消失，还把用户引向错误排查方向。
**修法**：区分「真实非 A 股持仓」与「弱形态兜底命中的可疑标的」——仅前者触发 cn_only 文案；后者剔除该行 + warning，其余 A 股照常算 EF。

---

## 三、P2

| # | 位置 | 问题（本人实测/复核确认） | 建议 |
|---|---|---|---|
| P2-1 | `stock_sentiment.py:424,451` + `agent_utils.py:1478` | `_build_us_sentiment` 过了形态闸门却把**原始 ticker** 传 Reddit：`AAPL.O`/`BRK-B` 是 `looks_like_instrument_code` 明确接受的合法形态，数据层只认裸 ticker → KeyError「个股新闻获取失败」。实测 `AAPL.O` 失败、`AAPL` 正常 | 传归一化后的 `_symbol`，一行改动 |
| P2-2 | `agent_utils.py:1478` | legacy 情绪工具与 core 工具**双实现平行存在**，门控/文案两套会持续漂移（news 工具已桥接，情绪没有） | 情绪工具同样桥接后删平行实现 |
| P2-3 | `stock_news.py:2078` | 旧版 `get_stock_news_unified` 未迁移 `resolve_instrument`：`"700"`（腾讯）落 UNKNOWN → 被报「无新闻」；同一 ticker 两个新闻工具行为不一致 | 旧工具也改走 `_build_stock_news_bundle` 或合并下线 |
| P2-4 | `portfolio_analyst_v2.py:276` | 重复代码 `weights_used` 字典塌缩：实测 `600519,30`+`600519,20` → `weights_used={'600519': 40.0}`，而 HHI=0.52 隐含 60/40，展示值自相矛盾 | 按 code 求和并注明「多行已合并」 |
| P2-5 | `portfolio_analyst_v2.py:461` + `skill_runtime/data_access.py:427` | cn_only 时仍对港股代码查 A 股集合（`00700`→`zfill(6)`→`000700` 查 A 股日线），必然 0 行，报告展示「00700：0 根」会被误读为「数据缺失」，实际是「市场不支持」 | cn_only 分支跳过非 cn 标的计数或标注「未查询（市场不支持）」 |
| P2-6 | `portfolio_analyst_v2.py:53` | 负权重被静默吸收成名称：实测 `600519,-30` → name=`-30`、weight=None、**无 warning**（正数越界有 warning） | `_NUM_RE` 支持负号，负权重走 warning+跳过 |
| P2-7 | `portfolio_analyst_v2.py:301` | 报告头部「分析日期」回显用户输入，但数据窗口永远是 `datetime.now()`，历史日期报告会「标注 2026-01-01、数据截至今天」 | 至少明示「数据窗口截至今日」 |
| P2-8 | `portfolio_analyst_v2.py:132-143` | 港股代码不在行首就整行丢失：实测 `腾讯控股,00700,30` → 「无法识别为证券代码」 | 兜底遍历全部 token |

## P3（摘要）

- `portfolio_analyst_v2.py:327`：`_extract_excluded` 用 `code in str(w)` 子串匹配，短代码可能误配归因。
- `portfolio_analyst_v2.py:170`：行内第 3 个及以后的数字 token 静默忽略（`600519,30,1500,1600` 丢 1600）。
- `engine.py:375`：`total_position_pct=0` 经 `keep_non_empty` 被丢弃，「真的是 0」与「未提供」不分（报告显示 0.0% 而非「未提供」）。
- `seed_general_workflows.py:469`：工作流 description 仍写「输出仓位区间与调仓建议」，适配器明确不产出建议，过度承诺。
- `stock_sentiment.py:306`：未识别市场兜底文案缺「不代表中性」免责句（港股分支有，中文兜底没有）。
- `stock_sentiment.py:57`：`market_type="us"` + A 股代码 `600519` → 显式市场胜出 → Reddit KeyError，冲突不告警（workflows 主链路有 warning，这里没有）。
- `agent_utils.check_local_reddit_cache` 调用时取 `get_data_dir()`，与数据路径的模块级 `DATA_DIR` 时点不一致，可能误判可用性。
- `get_market_snapshot_for_review` 仍挂 4 处 agent 工具清单，恒失败白耗轮次。
- `test_portfolio_analyst_v2.py` 缺口：P1 两种幻影输入、「一行垃圾整包降级」、负权重、重复代码、EF 失败三路径、`_count_trading_days` 导入源（local_data 日期 bug 防线）均无测试。

---

## 四、修改方案分项结论（回应「方案是否合理」）

| 方案 | 结论 | 核验 |
|---|---|---|
| market_codes 唯一入口化（形态白名单优先） | **合理，是本批最重要的基础设施** | 13 组输入实测一致；「先形态后市场」从结构上消除自由文本误判美股 |
| custom_params 保留键过滤 + None 丢弃 | **合理** | 保留键不可注入、None 不可覆盖默认值，契约由结构保证 |
| ticker 兜底门控 task_category=general | **合理** | 非 general 缺 ticker 照常报错，不再被掩盖 |
| `_generate_fallback_data` 删 random 假数据 | **合理且必要** | 「带标签的假数字守不住就是数据造假」；改为显式不可用 + 禁止推断 |
| 删单股下游、`start → a_pf → end` 单节点 DAG | **合理** | 从图结构上消灭伪组合报告路径；下游报告名契约错位一并消除 |
| 适配器直读 `state["holdings"]` + 纯代码计算不调 LLM | **合理且相容** | 双通道声明（registry inputs + engine 白名单）实测 IN SCHEMA，start→analyst 无丢失；先例（technical_analyst_v2）成立 |
| 数据链路选 skill_runtime 版本 | **正确** | 避开了 local_data 字典序丢数据 bug，且与 EF 内部同源同窗口，根数证据与重叠交易日不会矛盾 |
| parse_holdings 弱形态兜底 | **有缺陷（P1）** | 幻影标的 + 一行垃圾整体降级 + 错误归因，需修后再合 |

---

## 五、「能否避免虚假判断」总评

**round 7 遗留的三大虚假判断来源已被结构性消除**（本人实测确认）：
1. 自由文本当股票代码 → 形态白名单 `resolve_instrument`，`("","")` 契约强制调用方显式处理；
2. A 股逻辑误套港美股 → `market != "cn"` 提前 return 的**结构性**保证（非注释约定），akshare tripwire 测试实证零接触；
3. `random.uniform()` 编行情 → 显式「不可用 + 请勿推断」。

**但本批新引入一条**：`parse_holdings` 用**真实数据**算出**误导性报告**（幻影标的 / 一行垃圾让有效前沿整体消失并错因归因）——不造假数字，但会让用户基于错误事实决策。这正是 P1-1/P1-2，修完即可放行。

**总体判断**：方案方向正确、不变量的兑现是真实的（115+87+13 测试实证 + 本人复跑关键声明）；修掉 2 条 P1（改动局限在 `portfolio_analyst_v2.py` 内部，不波及引擎与 DAG）后建议合入；P2-1（一行改动）建议同批修。
