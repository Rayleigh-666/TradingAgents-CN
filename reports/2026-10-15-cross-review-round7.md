# 第七轮交叉 Review 报告（2026-10-15）

- **基线**：HEAD = `24ce32d6`（未提交改动，17 文件修改 + 6 个新测试文件 + 2 个新脚本）
- **范围**：5 处更新——① general 工作流参数契约（workflows / registry / engine / analysis / seed）；② 新闻工具市场模式（stock_news）；③ 舆情工具市场分发（stock_sentiment）；④ 市场代码解析前后端（market.ts / workflow.ts / GeneralAnalysis.vue / agent_utils / akshare）；⑤ `selected_analysts=None` 状态 500 修复（tracker / task_analysis_service）
- **方式**：3 路并行交叉 Review（工作流契约 / 新闻舆情工具 / 前端与杂项）+ 全量回归测试
- **测试结论**：后端 136 通过 / 0 失败（1 个环境性跳过），前端 vitest 10 通过 / 0 失败 —— **测试面可放行**
- **Review 结论**：P0 = 0，P1 = 6（2 条为两路独立复现），**建议修完 P1 再合入**

---

## P1（合入前必须修复）

### P1-1 custom_params 无过滤合并，回盖归一化 market_type 且可注入保留字段 ⭐两路复现
`app/routers/workflows.py:402-405`
`_resolve_market_and_symbol` 写入归一化结果后，`task_params.update({k: v for k, v in data.custom_params.items() if v is not None})` 把 custom_params 里的**原始值**盖回：
- `market_type="a_share"`/`"HONGKONG"`/`"xxx"` → 归一化结果被原值回盖，落库 task_params 出现非法 market_type；
- `custom_params={"symbol": "随便文本"}` 绕过「真识别才写 symbol」守卫，自由文本污染下游；
- 非 general 流程下 `custom_params={"task_category": "general"}` 可命中 registry 豁免判据，普通股票分析缺 symbol 也能过校验。
**修法**：合并时剔除保留键（`market_type`/`symbol`/`stock_code`/`task_category`），或归一化写入挪到合并之后；`task_category` 只允许按 workflow_type 由服务端写入。补端点级测试断言最终 `task_params["market_type"] ∈ {cn,hk,us}`。

### P1-2 `symbol = ticker` 写库前未走 normalize_symbol
`app/routers/workflows.py:83`
识别用大写副本、写库用原始串：`"700"`/`"0700.HK"` 原样落库而非 `00700`；`"600519.SS"` 带后缀落库；`"aapl"` 小写落库。下游按内部 symbol 查库必 miss。
**修法**：`symbol = normalize_symbol(ticker, recognized[market])`，返回空串按未识别处理（symbol=None）。

### P1-3 舆情工具手工剥后缀未归一化，港股查库必 miss
`core/tools/implementations/social/stock_sentiment.py:95-97, 270-272`
`for suffix in [...]: clean_ticker = clean_ticker.replace(suffix, '')`：`"0700.HK"`→`"0700"`（4 位），库内口径是 5 位 `"00700"`，查 `social_media_messages` 永远 miss；`.replace` 是子串替换会误剥代码中段；空 symbol 未处理。
**修法**：按 `_resolve_market` 结果调 `normalize_symbol(ticker, market)`，删掉手工 suffix 表。

### P1-4 港股舆情备用源误用 A 股接口，且违反「非 A 股不主动拉数据」
`core/tools/implementations/social/stock_sentiment.py:266-278, 434-436`
`is_hk` 落入 `_build_chinese_sentiment`，备用源 `AKShareProvider.get_stock_news_sync` 底层 `stock_news_em` 只认 A 股 6 位码；且对港股发起实时 akshare 调用。本轮重写新增了 `market_type="hk"` 显式入口（:62-63），触发面扩大，不能再算历史沿袭。
**修法**：is_hk 分支只查 Mongo 预热数据，无数据时明确返回「港股舆情暂无数据源」。

### P1-5 市场识别绕开 guess_market，1-3 位港股代码分发错误
`core/tools/implementations/news/stock_news.py:1811`、`core/tools/implementations/social/stock_sentiment.py:75`
两处用 `StockUtils.get_market_info`，其 `identify_stock_market` 只认 `\d{4,5}` 为港股；而铁律口径是「纯数字 ≤5 位→hk」。后果：`"700"`（腾讯最常见写法）在新闻工具落 UNKNOWN→全市场兜底（拿到大盘新闻而非港股新闻），在舆情工具落 UNKNOWN→中文兜底→A 股接口。
**修法**：UNKNOWN 时补一层 `guess_market(ticker)`，或直接以 `guess_market` 为准。

### P1-6 状态 None 修复的测试文件虚报覆盖、直接运行必崩
`tests/test_status_analysts_none.py:172-179`
`__main__` 块引用 8 个**从未定义**的测试函数（`python tests/test_status_analysts_none.py` 直接 NameError）；docstring 声称覆盖的 E（custom_params 丢 None）/ F（engine setdefault）/ G（builder participants）三层在 pytest 下零用例——`unified_analysis_engine.py` 和 `builder.py` 的改动实际无测试保护。
**修法**：补齐 8 个测试，或删 `__main__` 引用并修正 docstring。

---

## P2

| # | 位置 | 问题 | 建议 |
|---|---|---|---|
| P2-1 | `unified_analysis_engine.py:22` | 哨兵 `GENERAL_TICKER_SENTINEL = "MARKET"` 命中美股代码正则 `^[A-Z]{1,5}$`，下游 dispatch 会把它当美股 "MARKET" 查询 | 换成 `__GENERAL__` 等不可能命中代码正则的占位 |
| P2-2 | `workflows.py:76-86` | 显式 market_type 与识别市场冲突时静默错配（`market_type="us"` + `ticker="600519"` → 跨市场落库） | 冲突时 warning 并以识别市场为准，补测试 |
| P2-3 | `scripts/e2e_general_workflow_check.py:35-38` | 硬编码 JWT_SECRET 默认值 + 自助铸造 admin token | 删默认值，缺 env 即退出 |
| P2-4 | `simple_analysis_service.py:1685, 1755` | 同一 None 家族未设防：`parameters` 存在但 `selected_analysts=None` 时 `enumerate(None)` 在线程里抛 TypeError | `... or ["market", "fundamentals"]`，并纳入 TRAP_FILES |
| P2-5 | `stock_news.py:1058-1061` | 注释声称 provider 大盘分支「不可用」与本轮已修复的事实相反，且工具层绕过 provider 直连 `ak.news_cctv` 重复实现 | 改调 `provider.get_stock_news(symbol=None, market_date=...)`，或至少更正注释 |
| P2-6 | `market_snapshot.py:14-25, 51-55` | 工具仍全量注册但恒返回失败，description 仍在向 LLM 宣传「返回 K 线序列、技术指标」 | 下线注册或在 description 首句声明暂不可用 |
| P2-7 | `stock_sentiment.py:253-255` | except 块引用可能未绑定的 `clean_ticker`/`start_time`/`end_time`，真实错误被 NameError 二次掩盖 | 三个变量初始化移到 try 之前 |
| P2-8 | `stock_sentiment.py:22-38` | 本地 `_CN/_HK/_US_MARKET_ALIASES` 与 `market_codes.resolve_market` 重复且不一致，新漂移点 | 删本地别名表，复用 `resolve_market` |

## P3（摘要）

- `workflows.py:49`：`_resolve_market_and_symbol` 的 `workflow_type` 死参数（两路复现）。
- `unified_analysis_engine.py:44-56`：ticker 兜底对非 general 任务也生效，可能掩盖真缺参错误。
- 契约测试缺口：未覆盖 P1-1 别名回盖 / 注入 symbol / `700→00700` 归一化。
- `stock_news.py:1094`：7 天窗口 vs `MAX_DATE_FETCHES=3`，前 4 天永远取不到。
- `stock_news.py:1114`：`news_df.iterrows()` 缺 `isinstance(pd.DataFrame)` 校验。
- `stock_news.py:1137`：大盘新闻硬编码 `importance:"medium"` 又被相关性评分加 7 分，自造字段自消费、评分虚高。
- `stock_sentiment.py:350`：直接 `os.getenv("REDDIT_CLIENT_ID")` 绕过统一 config 体系；`:369` 自由文本 + `market_type="us"` 会拿中文文本查 Reddit。
- `test_sentiment_market_dispatch.py:55-91`：行为测试无打桩，真实连 Mongo/AKShare/Reddit，CI 下慢且可能挂起。
- `GeneralAnalysis.vue:1036`：`const result: any` 废掉刚修好的 `ExecutionResult` 类型契约；`:1044` 失败分支丢了 `result?.message`。
- `market.ts:96-100`：`looksLikeInstrumentCode` 已知误报边界（`"2024"`→港股、`"GOLD"`→美股）建议注释点明。
- `agent_utils.py:1408-1422`：Reddit 凭证门控与数据源真实可用性不匹配（实际只读本地缓存目录）。

---

## 做得对的点（三路汇总）

- **registry 豁免判据正确**：严格用 `task_category=="general"`（非「没有 symbol」），限定 STOCK_ANALYSIS 载体，测试含反向对照（普通股票分析仍要 symbol、ETF 打 general 标记也不豁免）。
- **ticker 兜底只进不出**：`apply_general_ticker_fallback` 只写 `workflow_inputs["ticker"]`，不回写 symbol/stock_code，有专项测试锁定。
- **UNKNOWN→空串**：`analysis.py` 兜底改动注释清晰，与 e2e 第 6 项检查闭环。
- **大盘新闻兜底不编造数据**：`reachable`/`matched_count` 状态机显式区分「数据源不可达」与「窗口内无新闻」；provider 拒绝静默回落写死的 `20240424`，宁可报错。
- **UNKNOWN 标的不再落美股 Reddit 死路**；Reddit 凭证缺失明确输出「本维度不可用、不代表情绪中性」，避免下游误读。
- **akshare.py 未触碰占位写入铁律**；`OptimizedUSProvider`→`OptimizedUSDataProvider` 修正属实。
- **前端透传链完整**：seed select（cn/hk/us）→ GeneralAnalysis 动态表单 → custom_params → 后端解析；`toApiMarketType` 语义零改动，未回退 `normalizeMarketForAnalysis`；ticker 复用加代码识别闸门，自由文本不再污染 symbol；轮询补 `cancelled` 终止分支。
- **None 修复主体齐备**：tracker 归一化、analysis.py 两处 `or []`、builder participants `or []`、engine 弃 setdefault，TRAP_FILES 源码守卫防回流（实测通过）。
- **测试质量**：news 测试全离线打桩；AST 级 stale-import 静态检查防再引入；e2e 用合成 running 任务确定性复现旧 500，finally 清理探针。

## 回归测试明细

- 本轮新测试 6 文件：56 通过 / 0 失败 / 1 跳过（`test_all_agent_ids_resolvable`，环境性跳过）。
- 历史契约回归 7 文件：80 通过 / 0 失败（precheck / readiness / api_key 守卫 / 降级摘要 / akshare 占位 / 报告导出 16 例 / 港美预热 24 例）。
- 前端 vitest：market.test.ts 8 通过、workflow.test.ts 2 通过。
- 环境备注：直接跑 pytest 会被 WorkBuddy shim 拦 `mkdir` 报 EEXIST，需 `env -u PYTHONPATH` 卸载 shim，与项目代码无关。

## 处置建议

1. **先修 6 条 P1**（P1-1/P1-2 在 workflows.py 同一处合并逻辑，可一次改完；P1-3/P1-4/P1-5 在工具层，统一走 normalize_symbol + guess_market）。
2. P2-1（哨兵改名）、P2-4（simple_analysis_service None 防御）建议同轮顺手修，成本低。
3. 修完后为 P1-1/P1-2/P1-3 各补一个回归用例，再跑一轮本报告的测试清单即可合入。
