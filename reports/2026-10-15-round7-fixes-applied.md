# 第七轮 Review 修正收口报告（2026-10-15）

对应输入：`reports/2026-10-15-cross-review-round7.md`（P0=0，P1=6，P2=8，P3 若干）

## 0. 结论摘要

| 项 | 结果 |
|---|---|
| P1 | **6/6 已修** |
| P2 | **8/8 已处置**（P2-1 报告建议无效，按正确方案修；P2-5 采用「更正注释」方案） |
| P3 | **11/11 已修** |
| Review 报告本身的错误 | **2 处已纠正**（P1-5 / P2-1 的建议若照字面执行会引入新回归） |
| 报告未覆盖、额外发现的缺陷 | **2 处已修**（`sector_tools.py` 大盘判据 / `watchlist_analysis_task.py` 写 symbol） |
| 新增公共基础设施 | `app/core/market_codes.py`：`resolve_instrument` / `looks_like_instrument_code` / `try_resolve_market` |
| E2E | 流程 1 **20/20**（70.6s）、流程 4 **17/17**（46.4s） |
| 回归 | **102 passed / 0 failed** |

> ⚠️ 与报告的分歧：本报告**没有照单全收**。有两条建议经实测验证会引入新回归，已按下述「正确方案」实施并在代码注释中写明归因。

---

## 1. 新增的公共基础设施（所有修复的地基）

`app/core/market_codes.py` 新增三个纯函数，把「一个字符串到底是不是证券代码、属于哪个市场」收敛到唯一入口：

| 函数 | 语义 |
|---|---|
| `looks_like_instrument_code(text)` | 形态判断。非 ASCII/空白 → False；cn `\d{6}` 或带 `.SH/.SZ/.SS/.BJ/.XSHG/.XSHE`；hk `\d{1,5}\.HK` 或**纯 `\d{1,5}`**；us `[A-Z]{1,5}` / `[A-Z]{1,5}[.\-][A-Z]` / `[A-Z]{1,5}[.\-][A-Z]{2,3}` |
| `resolve_instrument(text) -> (market, symbol)` | **代码解析唯一推荐入口**。先形态判断 → `guess_market` 定市场 → `normalize_symbol` 归一化；不像代码返回 `("","")` |
| `try_resolve_market(market) -> str` | 与 `resolve_market` 同源，但**未知返回 `""`**（`resolve_market` 未知兜底 `"cn"`）。供「不知道就明说不知道」的调用方使用 |

同时把 `_MARKET_ALIASES` 合并为全项目唯一别名表（补入 `a` / `a_share` / `a_shares` / `a-share` / `ashare` / `china` / `etf` / `hongkong` / `hong_kong` / `hong kong`）。

实测输出：

```
'600519'    → ('cn', '600519')      '600519.SS' → ('cn', '600519')
'0700.HK'   → ('hk', '00700')       '700'       → ('hk', '00700')
'AAPL'      → ('us', 'AAPL')        'aapl'      → ('us', 'AAPL')
'BRK-B'     → ('us', 'BRK.B')       'AAPL.O'    → ('us', 'AAPL')
'MARKET' / '__GENERAL__' / 'A股市场当日盘面复盘' / '以下持仓组合的健康度体检' / '' → ('', '')
```

⚠️ 与前端 `market.ts::looksLikeInstrumentCode` 存在**有意差异**：后端接受 `\d{1,5}`（港股内部口径铁律），前端只接受 `\d{4,5}`（宁缺勿滥，避免把 `"2024"` 这种年份误判成港股）。两侧注释已互相引用说明。

---

## 2. P1（6/6 已修）

| 编号 | 问题 | 处置 |
|---|---|---|
| P1-1 ⭐两路复现 | custom_params 无过滤合并，回盖归一化 `market_type`，可注入 `symbol`/`stock_code`/`task_category` 架空契约 | `workflows.py` 新增 `_RESERVED_TASK_PARAM_KEYS = frozenset({"market_type","symbol","stock_code","task_category"})` + 纯函数 `_sanitize_custom_params(custom_params) -> (dict, list[str])`，合并前先剔除保留键；被剔除的键回报 warning |
| P1-2 | `symbol = ticker` 写库前未走 `normalize_symbol` | `_resolve_market_and_symbol` 改走 `resolve_instrument()`，归一化后才落库；同时**删掉死参数 `workflow_type`**（P3 同条） |
| P1-3 | 舆情工具手工 `.replace` 剥后缀，`0700.HK`→`0700` 查库必 miss | `stock_sentiment.py` 删两处手工后缀表 → `normalize_symbol(ticker, market)`；归一化为空时**不查库**（不再用脏 key 打库） |
| P1-4 | 港股舆情备用源误用 A 股 `stock_news_em` 接口，且违反「非 A 股不主动拉数据」 | 分派拆成 `cn` / `hk` / `us` / 未识别四路；`_build_chinese_sentiment()` 增加 `market` 参数并**提前 return**（用结构保证非 cn 不会落进 A 股备用源，而非靠条件判断） |
| P1-5 | 市场识别绕开统一口径，1-3 位港股代码分发错误 | 新增 `_resolve_market(market_type, ticker) -> (market_key, market_name, is_china, is_hk, is_us)`：`try_resolve_market` 优先 → `resolve_instrument` 回落 → 全空给 `""`；**不再调 `StockUtils.get_market_info`**。`stock_news.py::_build_stock_news_bundle` 同样改走 `resolve_instrument` |
| P1-6 | `test_status_analysts_none.py` 虚报覆盖、`__main__` 直接运行必崩 | 实现 `__main__` 引用的 8 个原本未定义的函数；docstring 与实际用例一一对应（删除虚报的 E/F/G 覆盖声明） |

### 2.1 ⚠️ P1-5 的报告建议本身是错的（未照单全收）

报告建议：「UNKNOWN 时补一层 `guess_market(ticker)`」。

**实测反证**：

```python
guess_market("A股市场当日盘面复盘") == "us"   # 注意自由文本里的字母 A
```

`guess_market` 的规则是「含拉丁字母 → us」。流程 1「每日市场复盘」的分析目标正是这类自由文本，**照报告建议修会直接把现在正常工作的流程 1 新闻链路打成美股**。

**正确方案**：先做形态判断（`looks_like_instrument_code`），只有确认「像代码」才去 `guess_market` 定市场——即 `resolve_instrument`。这也是本轮所有工具层修复的统一落点。

---

## 3. P2（8/8 已处置）

| 编号 | 问题 | 处置 |
|---|---|---|
| P2-1 | 哨兵 `GENERAL_TICKER_SENTINEL = "MARKET"` 命中美股正则 | 见 §3.1 —— 报告的归因**是错的**。值已改为 `"__GENERAL__"`，但真实归因写进代码注释，价值落在解析侧 |
| P2-2 | 显式 `market_type` 与识别市场冲突时静默错配（`market_type="us"` + `ticker="600519"` → 跨市场落库） | `_resolve_market_and_symbol` 冲突时**以识别结果为准** + 打 warning；补对应测试用例 |
| P2-3 | `scripts/e2e_general_workflow_check.py` 硬编码 JWT_SECRET 默认值 | 删默认值，新增纯函数 `_load_jwt_secret(env=, env_file=)`（env → `.env` → `raise SystemExit(1)`）+ `_parse_env_file_for_key`；脚本不会回退到任何内置密钥 |
| P2-4 | `simple_analysis_service.py:1685/1755` 同家族 `selected_analysts=None` 未设防 | 新增模块级纯函数 `_resolve_selected_analysts(parameters)`，两处改用它。口径：**None 与空列表都兜底**成 `["market","fundamentals"]`（依据同文件 `:1250`、`:946` 既有口径）。已纳入 `TRAP_FILES` |
| P2-5 | `stock_news.py:1058-1061` 注释声称 provider 大盘分支「不可用」，与本轮已修复的事实相反 | 采用报告的备选方案「更正注释」：改写 `:1058` 起与 `:1178` 起两处 docstring，删除与当前代码相反的「provider 不可用」说法，如实说明工具层直连 `ak.news_cctv` 的原因 |
| P2-6 | `market_snapshot.py` 全量注册但恒失败，description 仍在宣传「返回 K 线序列、技术指标」 | description 首句改为 `【暂不可用】交易复盘市场快照：底层统一K线数据接口尚未实现，当前恒返回 success=False；请勿依赖本工具获取K线/技术指标数据`（`core/tools/config.py:856` 同步改） |
| P2-7 | `stock_sentiment.py:253-255` except 块引用可能未绑定的局部变量，真实错误被 `NameError` 二次掩盖 | `clean_ticker=""` / `start_time=None` / `end_time=None` 三个变量初始化**提到 `try` 之前** |
| P2-8 | `stock_sentiment.py` 本地 `_CN/_HK/_US_MARKET_ALIASES` 与 `market_codes` 重复且不一致 | 删三张本地别名表 → `try_resolve_market` |

### 3.1 ⚠️ P2-1 的报告建议无效（未照单全收）

报告称：哨兵 `"MARKET"` 命中美股代码正则 `^[A-Z]{1,5}$`。

**这是错的**——`MARKET` 有 **6** 个字母，不匹配 `{1,5}`。实测：

```python
StockUtils.identify_stock_market("MARKET") == "UNKNOWN"
```

真因是 `guess_market` 的「含字母 → us」。因此报告建议的替身 `"__GENERAL__"` **同样含字母、同样被判 us，换了等于没换**（含下划线的写法虽不匹配 `^[A-Z]{1,5}$`，但 `guess_market` 走的是「含字母」分支，不看正则）。

本轮仍改了值（`"MARKET"` → `"__GENERAL__"`，语义更自解释），但把**真实归因写进代码注释**，并把真正的修复放在「解析侧一律走 `resolve_instrument`」——即无论哨兵取什么值，下游解析都不会把它当成证券代码。

---

## 4. P3（11/11 已修）

| 位置 | 问题 | 处置 |
|---|---|---|
| `workflows.py:49` | `_resolve_market_and_symbol` 的 `workflow_type` 死参数 | 已删 |
| `unified_analysis_engine.py:44-56` | ticker 兜底对**非 general** 任务也生效，可能掩盖真缺参错误 | 兜底整体改为 `task_category == "general"` 门控；非 general 直接返回 `None`，不写任何键 |
| 契约测试缺口 | 未覆盖 P1-1 别名回盖 / 注入 symbol / `700→00700` 归一化 | `test_general_workflow_param_contract.py` 补齐（含 1 条反向对照）；`test_market_symbol_resolution.py` 13 例含 `700→00700` |
| `stock_news.py:1094` | 7 天窗口 vs `MAX_DATE_FETCHES=3`，前 4 天永远取不到 | `MARKET_NEWS_MAX_DATE_FETCHES` 由 `3` 改为 `= A_SHARE_NEWS_LOOKBACK_DAYS`（7） |
| `stock_news.py:1114` | `news_df.iterrows()` 缺 `isinstance(pd.DataFrame)` 校验 | 已补显式 `isinstance`。（注：报告说「缺校验」**部分不属实**——该处**已有**鸭子类型守卫 `news_df is not None and not getattr(news_df, "empty", True)`，只是不能保证 `.iterrows` 存在。已在注释中说明） |
| `stock_news.py:1137` | 大盘新闻硬编码 `importance:"medium"` 又被相关性评分加 7 分，自造字段自消费、评分虚高 | 已删硬编码 |
| `stock_sentiment.py:350` | 直接 `os.getenv("REDDIT_CLIENT_ID")` 绕过统一 config | 删 `os.getenv("REDDIT_*")`，门控改 `_reddit_cache_unavailable_reason()`（与数据源同源探测 `<DATA_DIR>/reddit_data/*.jsonl`） |
| `stock_sentiment.py:369` | 自由文本 + `market_type="us"` 会拿中文文本查 Reddit | `_build_us_sentiment` 增加 `resolve_instrument` 形态闸门 |
| `test_sentiment_market_dispatch.py:55-91` | 行为测试无打桩，真实连 Mongo/AKShare/Reddit | 改为**全离线**：`fake_mongo` / 假 akshare 模块 / Reddit 调用哨兵；AST 守卫保留。7 → 12 例 |
| `GeneralAnalysis.vue:1036/1044` | `const result: any` 废掉类型契约；失败分支丢 `result?.message` | `:1036` 改 `const result`（类型自洽，无需改 `workflow.ts`）；`:1044` 改 `result?.error \|\| result?.message \|\| '未获取到任务ID，请检查后端日志'`；`catch (e: any)` → `catch (e)` + `e instanceof Error` |
| `market.ts:96-100` | 已知误报边界（`"2024"`→港股、`"GOLD"`→美股）未标注 | 补注释点明 + 跨端一致性说明；新增 3 条用例锁死边界 |
| `agent_utils.py:1408-1422` | Reddit 凭证门控与数据源真实可用性不匹配 | 门控抽成纯函数 `check_local_reddit_cache(data_dir=None) -> (available, reason)`，路径与 `interface.py` 同源；删 `is_valid_api_key` 判断 |

### 4.1 一处必要的语义变更（测试断言的删除）

`test_sentiment_market_dispatch.py` 原有一条 `assert "未配置有效凭证" in out`。该断言锁定的**正是 P3 判定为错误的行为**（用凭证门控代替「本地缓存是否存在」的真实判据）。因此必须删除，替换为 `非证券代码形态` 断言。保留语义内核：工具不崩 + 显式说明原因 + 输出中仍带 `不代表情绪中性`。

---

## 5. 报告未覆盖、额外发现并修复的 2 处

这两处不在任何修复清单内，是我在核实 worker 上报时发现的漏网之鱼。

### 5.1 `core/tools/sector_tools.py::analyze_sector` —— 大盘判据失效

```python
# 旧
is_market_analysis = (ticker == "MARKET" or not ticker)
# 新
from app.core.market_codes import resolve_instrument
_market, _symbol = resolve_instrument(ticker or "")
is_market_analysis = not _symbol
```

**为什么要改**：哨兵值改名后，字面量判据对 `"__GENERAL__"` 失效；更关键的是 general 工作流的 `analysis_target` 常是自由文本（如「新能源板块」），字面量判据会让这类输入**错误落进「个股板块分析」分支**，拿自由文本去查成分股，产出错误报告。

### 5.2 `app/worker/watchlist_analysis_task.py:727` —— 定时调度路径违反契约

```python
# 旧
task_params = {
    "symbol": analysis_target,
    "stock_code": analysis_target,
    ...
}
# 新：只保留 analysis_target
task_params = {
    "analysis_target": analysis_target,
    ...
}
```

**为什么要改**：该路径（定时自选股调度）把自由文本 `analysis_target` 直接写进 `symbol`/`stock_code`，与「通用研究工作流绝不写 symbol」的契约**直接相悖**（前两轮我刚修好主链路，这里是并行的第二条写入口）。「识别到代码才写 symbol」统一由 `workflows.py::_resolve_market_and_symbol` 负责。

---

## 6. 收口验证

### 6.1 环境

服务在会话重启后**全部停止**，按以下顺序恢复：

```bash
bash .runtime/bin/start-services.sh   # mongod 27017 + redis 6379
bash .runtime/bin/start-app.sh        # 后端 8001 + 前端 3000
```

新代码加载判据：`GET /api/system/readiness` 返回含 `basics_by_market` → `{'cn': 11149, 'hk': 674, 'us': 517}`。

### 6.2 E2E（`scripts/e2e_general_workflow_check.py`）

**流程 1「每日市场复盘 · 盘面日报」— 20/20 通过，70.6s**

- `task_params` 只有 `market_type=cn` + `task_category=general`，`symbol=None`、`stock_code=None`
- 36 次状态轮询**全程 200**
- 合成 running 任务（`selected_analysts=None`）状态接口返回 200 且 `estimated_total_time=180.0` —— 证明**真的走进了 tracker 分支**（旧代码此处 500）
- reports 六维：`news_report`(3436) / `sentiment_report`(2242) / `index_report` / `sector_report` / `trader_investment_plan` / `research_team_decision`
- 全部 reports **无降级/缺参标志**，结果未写占位 symbol

**流程 4「持仓组合健康度体检」— 17/17 通过，46.4s**

- reports 七维：`market_report` / `fundamentals_report` / `sector_report` / `trader_investment_plan` / `risk_assessment` / **`valuation_report`(718)** / `risk_management_decision`
- `valuation_report` 在位且**非降级**（第一轮报告的估值节点降级问题确认修复）

### 6.3 回归

```
102 passed, 47 warnings in 2.52s
```

| 测试文件 | 用例数 |
|---|---|
| `test_market_symbol_resolution.py` | 13 |
| `test_general_workflow_param_contract.py` | 20 |
| `test_general_workflow_seed.py` | 10 |
| `test_status_analysts_none.py` | 13 |
| `test_sentiment_market_dispatch.py` | 12 |
| `test_news_market_mode.py` | 18 |
| `test_engine_input_defaults.py` | 16 |
| **合计** | **102** |

前端：vitest 13 passed + `vue-tsc --noEmit` exit 0。

### 6.4 ⚠️ 存量基线失败（非本轮引入）

`tests/unit/agents/test_sector_analyst.py` **9 个用例失败**，根因是**测试夹具的 Mock 不支持 `await`**：

```
❌ 板块轮动分析失败: object Mock can't be used in 'await' expression
```

涉及 `test_get_sector_rotation` / `test_get_sector_performance_*` / `test_get_peer_comparison_*`。**已用 `git stash push -- core/tools/sector_tools.py` 在该文件被修改前复现同样的失败**，证明与 `analyze_sector` 的改动无关。修法是给夹具补 `AsyncMock`，属独立技术债。

---

## 7. 剩余待办（本轮未动，需决策）

1. **流程 4 `holdings` 字段送不到任何 agent** —— 它确实进了 `task_params.holdings`，但 workflow 输入只有 `ticker`（及各 report 变量）会进 prompt 模板，`unified_analysis_engine` / `builder` / `agents/base.py` 里**没有任何 holdings 注入**。属模板级缺陷，需产品决策（是补注入，还是把流程 4 换成组合级分析师）。
2. **流程 3「资金面解读」职责改写** —— 需 `prompt_templates` 流程级模板支持。
3. `GeneralAnalysis.vue` textarea 硬编码 `maxlength="500"`，限制不了 `holdings` 长文本。
4. **「未知市场」返回 `random.uniform()` 随机假数据** —— `tradingagents/dataflows/optimized_china_data.py:2135` 与 `providers/us/optimized.py:617` 的 `_generate_fallback_data()` 用随机数编价格，只靠 `## 📊 模拟数据（仅供演示）` 这行标签兜着。建议遇到未知标的直接返回「不可用」。
5. `tests/unit/agents/test_sector_analyst.py` 夹具 `AsyncMock` 技术债。
6. ⚠️ **所有改动仍未 commit** —— 本轮结束时工作区有 22 个 modified 文件 + 若干 untracked（含本报告与 `scripts/`、新增测试文件）。
