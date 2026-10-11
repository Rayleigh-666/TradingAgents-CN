# 港美股预热缓存 · 今日提交交叉 Review（新模型）

- 日期：2026-10-11
- Review 对象：main 分支今日 5 个提交（GitHub API 与本地 reflog 双向核实，无第 6 个含新提交的推送）
- Review 方式：通读全部 diff + 关联文件上下文（stocks.py / analysis.py / stock_utils.py / wind_adapter / wind_client / favorites_service / quotes_ingestion_service / stock_data.py）+ 接口签名与导入核对

## 提交清单

| 提交         | 时间    | 内容                                  | 结论                          |
| ---------- | ----- | ----------------------------------- | --------------------------- |
| `73585f41` | 13:31 | LocalAdapter 仅读 data_source='local' | ✅ 通过（前提已验证属实）               |
| `b4bb7299` | 17:04 | 前端恢复港股/美股入口                         | 🟡 基本通过，2 个衍生问题             |
| `69bf9688` | 19:35 | 港美股预热服务主体                           | 🟡 主体扎实，1 个 P1 + 2 个 P2     |
| `3c1a670b` | 20:23 | 批量化 + Wind 串号防护                     | 🟡 优化有效，串号防护设计好；P1 未覆盖全路径   |
| `fcc61a8c` | 20:23 | 设计文档 v3                             | 🔴 文档质量高，但工作区文件已被旧版覆盖（见 P0） |

---

## P0 — 工作区设计文档被旧版覆盖（流程问题，建议立即处理）

`git status` 显示 `docs/design/hk-us-preheat-cache.md` 有**未提交修改**，且工作区版本是**实施前的 v2**（474 行），把已推送的 **v3**（593 行，含 10.x 全量实测记录、Wind 串号踩坑表、已知数据质量注意点）覆盖掉了。

- 风险：此刻若执行 `git commit -a` 或再次提交该文件，会把 v3 的实测记录整体冲掉。
- 处理：确认本地无意保留旧版后，`git checkout -- docs/design/hk-us-preheat-cache.md` 恢复推送版本。

## P1 — 逐只模式下跨数据源切换会产生同一交易日重复记录

位置：`app/worker/hk_us_preheat_service.py` 路径 A `_sync_daily_per_symbol` → `_upsert_daily`

- `_upsert_daily` 只做 upsert，**不清除该 symbol 其它 `data_source` 的旧行**；且路径 A 写入的是整个增量窗口的全部 bar（含 ≤ 库内最新日期的 5 天缓冲行），不做 `> last` 过滤。
- 触发场景（均为受支持配置）：`HK_US_PREHEAT_PRIMARY_SOURCE=wind` 或 `HK_US_PREHEAT_USE_BATCH=False` 时，某 symbol 曾由 Yahoo 兜底写入 → Wind 恢复后同 `trade_date` 出现两条记录（唯一键含 `data_source`，Mongo 不报冲突）→ K 线接口 `find({"symbol", "period": "daily"})` **不过滤 data_source** → 接口返回重复交易日，下游指标/分析被污染。
- `3c1a670b` 提交信息宣称"修复唯一键含 data_source 导致重复日期"，但 `_write_single_source()`（先删异源再写）只用在批量路径的 Wind 兜底与薄覆盖换源两处；**批量路径安全（Yahoo 侧有 `> last` 过滤、Wind 侧走 `_write_single_source`），路径 A 未覆盖**。
- 建议修复：把 `_upsert_daily` 统一改为"先 `delete_many({symbol, data_source: {$ne: source}})` 再 bulk upsert"（每 symbol 一次 delete，成本可忽略）；或路径 A 也改走 `_write_single_source` 语义。

## P2 — 美股自选股的 windcode 缺交易所后缀，Wind 侧全部静默失效

位置：`_merge_favorites`（美股分支 `windcode = raw_code`）

- 设计文档自证约束：「Wind 传无后缀 `AAPL` 返回空，必须带后缀」。
- 后果：美股自选股 ① 财务同步永远失败（财务无 Yahoo 兜底，计入失败清单）；② 基础信息每次多打一次无效 Wind 调用后才走 Yahoo 兜底；③ Wind 备源日线对该批标的失效。
- 港股自选做了 4 位补全（`0700.HK`），美股没有对应处理。
- 建议：universe 落库时对无后缀美股代码做一次 Wind 反查补 `.O/.N`，或在 universe 文档打标 `yahoo_only`，取数链路跳过 Wind。

## P2 — 带点号的美股代码（BRK.B 类）前后端均不识别，且预热会写入错标的数据

- 前端 `b4bb7299` 明确支持 `BRK.B`（`validateUSStock` 的 docstring 示例即 BRK.B）；
- 后端两处识别都不含点号：`stocks.py:356 _detect_market_and_code`（纯字母才判 US，`BRK.B` 落入默认 → CN）、`tradingagents/utils/stock_utils.py:60 identify_stock_market`（`^[A-Z]{1,5}$` → UNKNOWN → 默认 cn）。用户从新入口输入 BRK.B，分析与 K 线都会走 A 股链路 → 无数据。
- 预热侧：Wind 返回 `BRK.B.N` → `to_internal_symbol` 归并出 `BRK`（与 BRK.A 撞号）、`to_yahoo_ticker` 切成 `BRK`（Yahoo 的 BRK 是伯克希尔 **A** 类，B 类在 Yahoo 是 `BRK-B`）→ **把 A 类行情写进 B 类 symbol**。标普500 成分含 BRK.B。
- 建议：三处正则放行 `^[A-Z]{1,5}(\.[A-Z])?$`；`to_yahoo_ticker` 对美股把点号映射为 Yahoo 的连字符（`BRK.B.N → BRK-B`）；`windcode_matches` 增加点号前后段分别比对。

## P3 — 小问题 / 改进建议

1. **批量日线内存驻留**：`_sync_daily_batch` 中 `data_map` 一次性持有整个市场分组、统一窗口（组内最大需求）的全部行——约 1190 只 × ~250 行 ≈ 30 万条 dict（百 MB 级）。功能正确，建议按 chunk 边解析边释放或流式写库。
2. **薄覆盖判定口径**：`_MIN_YAHOO_ROWS` 按「Yahoo 原始返回行数」而非「新增行数」触发。已是最新的标的若 Yahoo 偏薄（如次新股），每次同步都会多打一次完整 lookback 的 Wind（`max(p["limit"], lookback)`），结果又被 `existing` 行数否决——纯浪费。
3. **前端误判静默化**：5 位数字（如 `51005`，ETF `510050` 笔误）现在会被识别为港股并自动切换市场，不再报错；且反向不联动（港股/美股市场下手输 `AAPL`/`00700` 只报错不切换）。
4. **universe 无移除路径**：`enabled` 恒为 True，指数清单调整后旧成分不会被清理；`stats[source_tag]` 在「新鲜跳过」与「刚刷新」两种路径下口径不一致（前者 enabled 计数、后者 members 数）。
5. **LocalAdapter 查询性能**：`find({"data_source": "local"})` 若 `market_quotes` 无该字段索引则为集合扫描（当前量级可接受，建议补 `{data_source: 1}` 索引）。

---

## 已验证通过的关键点

- `73585f41` 前提属实：`quotes_ingestion_service._bulk_upsert` 确实不写 `data_source`；手工导入接口 `stock_data.py:727` 确实置 `'local'`（`:366` 的 tushare 只是响应示例，非入库）。
- `stocks.py` 预热缓存读取：`date` 已导入；`period='daily'` 过滤与写入一致；15 天陈旧回退实时源；symbol 格式（HK 5 位 / US 裸 ticker）与预热写入、`_detect_market_and_code` 三方一致；`get_mongo_db()` 同步返回用法与文件内既有用法一致。
- `main.py`：`add_job` 后 `pause_job`，且 `scheduler.start()`（:1024）在其后执行，暂停语义正确。
- 外部接口匹配：`WindAdapter.get_kline(code, period, limit)` 返回 `[{time,open,high,low,close,volume,amount}]` 与 `_wind_kline` 解析一致；`WindMCPClient.call_table` 返回 DataFrame。
- `favorites_sync_task` 双读：字段名 `stock_code/stock_name/market` 与 `favorites_service.add_favorite` 写入一致；`Set` 已导入；A 股市场白名单较原逻辑只宽不严。
- `windcode_matches()` 串号防护（拒收 `!` 退市标记、后缀/前导零归一、批量与逐只双路校验）设计扎实，实测弃用 10 只误配的取舍（宁缺勿错、不建别名表）合理。
- 幂等性：唯一索引 + upsert + `$setOnInsert`；文档实测复跑 `{'ok':0,'skipped':1190,'rows':0}`。
- 前端 `handleValidateData` 的 `marketTypeMap` 补 `hk/us` 映射是真实 bug 修复（此前港美股数据检验回退到 A 股集合）。

## 关于「6 个推送」

今日实际为 **5 个提交**（本地 reflog 与 GitHub commits API 双向核实；`v3.0` 分支今日无更新）。第 6 次推送应为无新提交的空推/重推，无可 review 内容。如指其它内容请指明。
