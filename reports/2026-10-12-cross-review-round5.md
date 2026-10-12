# 交叉 Review 第五轮：港股代码识别错误 Bug 修复核验（工作区未提交改动）

- 日期：2026-10-12 20:56
- Review 范围：**工作区未提交 diff**（10 文件，+471/-19），即「港股代码识别错误」bug 的修复
- Review 方式：通读全部 diff + 读写两端口径逐点核对（universe / basic_info / daily / market_quotes 四类集合的键与格式约定）+ 前后端全链路走查 + 实跑单测（23/23）+ 边界样例脚本 + `py_compile` + `vue-tsc --noEmit`

## 总体结论

**修复正确且完整，主链路无 P0/P1/P2 问题，可以提交。** 剩余 3 条 P3 观察（1 条建议随手修，2 条记录备查），均不阻断提交。

## 修复内容确认

| 文件 | 改动 | 核验 |
|---|---|---|
| `app/core/market_codes.py` | 新增 `resolve_market` / `normalize_hk_symbol` / `normalize_cn_symbol` / `normalize_us_symbol` / `normalize_symbol` / `internal_to_windcode`（+101 行，零依赖纯函数） | ✅ 单一事实来源，cn→6 位 / hk→5 位 / us→裸 ticker；港股兼容 4 位、带 `.HK` 后缀及旧 bug 脏值 `003669→03669` |
| `app/services/data_validation_service.py` | `str(symbol).zfill(6)` → `resolve_market` + `normalize_symbol`，空值显式返回「代码格式错误」 | ✅ 误报「基础信息不存在」的根因已除；`_check_basic_info` 的 `$or:[symbol,code]` 查询与 `sync_basic_info` 落库（symbol+code 双写，hk_us_preheat_service.py:1502-1517）口径一致 |
| `app/routers/stock_sync.py` | `SingleStockSyncRequest` 增 `market_type`；`FOREIGN_MARKETS` 提前分流；新增 `_run_foreign_stock_sync` / `_sync_latest_foreign_quote` / `_interpret_preheat_stats` | ✅ 复用预热服务（不另造 schema）；`_request_to_dict`→payload→任务重建，`market_type` 全程可达 |
| `app/worker/hk_us_preheat_service.py` | 新增 `ensure_symbols`（on-demand 入池，`ON_DEMAND_SOURCE`）；`_us_ticker` 委托 `normalize_us_symbol` | ✅ 与 `_merge_favorites` 同构；`_load_targets`（enabled=True / market 大写 / symbol `$in`）可命中；US 走 `_resolve_us_windcodes` 补后缀 |
| 前端 4 文件 | `toApiMarketType` 工具 + 3 个页面传 `market_type` + 接口类型 | ✅ `港股/美股/A股/HK/US/hk` 等写法全覆盖；显式避开 `normalizeMarketForAnalysis`（仅 A 股时代助手） |
| 测试 | `test_normalize_symbol_by_market` / `test_preheat_stats_skipped_is_success` | ✅ 含旧脏值回归；skipped=成功语义固化 |

## 逐项深核（均通过）

1. **写入口径一致**：`market_quotes_hk/us` 既有写方（hk_sync_service.py:411 `code=lstrip('0').zfill(5)`、us_sync_service.py:334 `code=upper()`）均以 `code` 为 upsert 键；新 `_sync_latest_foreign_quote` 同样按 `{"code": symbol}` 写、5 位/裸 ticker 格式相同——不会产生重复文档，favorites_service 的 `{"code": {"$in": [...]}}` 查询能命中。
2. **universe 注册闭环**：`internal_to_windcode("03669","HK")="3669.HK"` → `to_yahoo_ticker` 原样（Yahoo 4 位+.HK）→ `to_full_symbol="03669.HK"`；`ensure_symbols` 的 `$set: enabled=True` 满足 `_load_targets` 过滤。
3. **按需标的不会被误清理**：`_prune_source` 仅以 `"favorite"` 与指数标签调用（:402/:418），`"ondemand"` 标签从不参与 prune；双来源标的被摘掉 favorite 后 `remaining` 非空、保持 enabled。
4. **skipped 语义**：`_interpret_preheat_stats` 把 `skipped>0 且 failed==0` 判成功，与 `sync_daily` 统计键（total/ok/skipped/failed/rows，:810）匹配；二次同步不再误报失败。
5. **前端市场值实测映射**：Detail.vue 的 `d.market` 为 `'HK'/'US'`（foreign_stock_service.py:1100/1123）→ `toApiMarketType` → hk/us ✓；Favorites 的 `row.market` 为 `'港股'/'美股'/'A股'`（favorites_service.py:358 默认 `'A股'`）→ ✓；分析页 `handleValidateData` 本就传 `market_type`（'港股'→'hk'），`handleSyncMissingData` 本次补齐 ✓。
6. **边界样例脚本**：`3669/03669/003669/0700.HK/700 → 03669/00700`；`BRK-B.N→BRK.B`、`AAPL.O→AAPL`；`resolve_market(None/'')→cn`；`normalize_symbol('abc','hk')=''`（触发格式错误分支）——全部符合设计。
7. **实跑验证**：
   - `tests/test_hk_us_preheat_codes.py`：**23/23 通过**（含 2 个新增用例）
   - `vue-tsc --noEmit`：**0 错误**（全量，exit 0）
   - `py_compile` 5 个改动 Python 文件：**OK**

## P3 观察（不阻断提交）

| # | 位置 | 说明 | 建议 |
|---|---|---|---|
| 1 | `market_codes.internal_to_windcode` | 对 >5 位数字输入静默取末 4 位（`zfill(5)[-4:]`）：`"1234567"` → `"4567.HK"`，会把错误长码注册成**另一只股票**的 universe 记录。`normalize_hk_symbol` 本身不截断（DB 匹配侧安全），风险仅在入池注册这一步 | 一行 guard：`normalize_hk_symbol` 对 >5 位直接返回 `""`（无法识别），从源头堵死。**建议随手修** |
| 2 | `unified_analysis_service._resolve_stock_name` / `task_analysis_service._resolve_stock_name` / `simple_analysis_service._get_cached_security_name` | 三处名称解析仍先 `zfill(6)` 再只查 A 股 `stock_basic_info`，港股 5 位码被补成 6 位查 A 股库 → 必失配，退回「股票03669」字样。**不阻断分析**（仅报告/任务展示名退化），属同一类 bug 的剩余残渣 | 后续按 `resolve_market` 分流查 `stock_basic_info_hk/us` |
| 3 | `stocks.py::_detect_market_and_code` | 6 位纯数字（含旧脏值 `003669`）落入 `^\d{6}$` A 股分支。但合法 A 股 `000xxx`（深市）与脏值无法从码形区分，当前表现为优雅失败（not-found）而非错数据，**非回归** | 记录备查，无需行动 |
| 4 | universe 增长 | `ondemand` 标签只增不删（无 TTL/上限），universe 随临时分析行为单调增长 | 设计已文档化（池=指数∪自选∪按需），记录备查 |
| 5 | Favorites 单股同步弹窗 | 默认仅勾选 realtime；若该标的从未同步过日线，`_sync_latest_foreign_quote` 无数据可回填 → 必失败 | UX 观察：默认补勾 historical，或失败时提示「请先同步历史数据」 |

## 遗留提醒（沿用前几轮）

- 改动生效需**重启后端进程**（market_codes 新函数、路由层分流）。
- 报告/代码提交前跑一次 `git status`（该仓库 format-on-save 坑过旧 buffer）。

## 结论

Bug 修复主链路（识别 → 校验 → 按需同步 → 行情回填 → 前端分流）全部闭环，读写口径与既有服务一致，测试/类型检查/编译全绿。**建议合入**；P3-1 的一行 guard 建议随本提交一并处理，P3-2/3/4/5 记录备查。
