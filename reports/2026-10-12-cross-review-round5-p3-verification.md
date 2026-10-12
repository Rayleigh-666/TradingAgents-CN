# 第五轮交叉 Review · P3 修复闭环核验（2026-10-12 21:35）

- 日期：2026-10-12 21:35
- 核验对象：`reports/2026-10-12-cross-review-round5-p3-fixes.md` 声称的 P3 修复（工作区未提交 diff，15 文件，+768/-75）
- 核验方式：逐条比对 diff + 目标测试实跑（pytest 9.1.1）+ **真实 MongoDB 实测** + 干净 HEAD worktree 反向对照（验证测试非空转）+ vue-tsc + py_compile

## 总体结论

**P3 可行动项（1/2/5）全部闭环，追加的占位脏行清理与源头阻断也属实；无新引入问题。可以提交。**

## 逐条核验

| # | 修复声明 | 核验结果 |
|---|---|---|
| P3-1 | `normalize_hk_symbol` 对「去前导零后 >5 位」返回 `""`；`internal_to_windcode` 先归一化再取末 4 位 | ✅ 实测 `123456/1234567/999999 → ""`、windcode `""`；关键判据用**有效位数**（`lstrip("0")` 后）而非输入位数，旧脏值 `003669 → 03669`、windcode `3669.HK` 均保留，回归用例无冲突 |
| P3-2 | 三处名称解析改 `guess_market` + `basic_info_collection` + `normalize_symbol` | ✅ 实测路由全对（`03669→hk/stock_basic_info_hk/03669`、`0700.HK→hk`、`600519.SH→cn`、`AAPL.O→us/AAPL`、`BRK.B→us/BRK.B`）；ETF 分支已加 `market=="cn"` 前置；**真实库端到端**：`03669→永达汽车`、`00700→腾讯控股`、`NVDA→英伟达(NVIDIA)`、`BRK.B→伯克希尔(BERKSHIRE)-B`、`000001→平安银行`、`600519→贵州茅台`、`003669→None`（不再伪造名） |
| P3-3 | 6 位脏值按 A 股处理（记录备查） | ✅ 未改，`guess_market("003669")→cn`，与 `stocks.py` 口径一致；属可接受的优雅失败（not-found，非错数据） |
| P3-4 | ondemand 只增不删（记录备查） | ✅ 未改，设计已文档化 |
| P3-5 | Favorites 默认勾选加 historical + 后端失败文案给指引 | ✅ `singleSyncForm.syncTypes` 默认 `['realtime','historical']`；`_run_foreign_stock_sync` realtime 失败文案为「无日线数据可回填（请先勾选「历史行情数据」同步日线）」 |
| 追加-1 | 删除占位脏行（`_id=6accc17284a48cf3cc3fd4e6`） | ✅ **真实库复核**：`003669` 档不存在；全库扫描 `name == "股票<code>"` 形态 **0 条**；`stock_basic_info` 11147→**11146**；备份文件 `.runtime/backup/...003669...json` 在场且内容与被删行一致 |
| 追加-2 | 单股同步写库守卫（degraded/占位名不落库） | ✅ `stock_sync.py:904-917`：`degraded_sync or not has_real_name` → `skipped_write=True` 直接返回失败，不 upsert；既有「库内真名回填」逻辑（:877）保留在前、先替换再判真名，顺序正确 |
| 追加-3 | 批量同步守卫 + builder 不伪造名 | ✅ `akshare_sync_service.py:278-291` 守卫位于写库前、复用既有 `batch_stats["errors"]/error_count` 键；`_build_basic_info_from_list` 改 `str(name or "").strip()`；全仓库仅批量循环一个调用方，无其他消费者依赖占位名 |

## 测试非空转证明（干净 HEAD worktree 反向对照）

把新测试拷入 `git worktree`（HEAD=f49e3dd5，无任何本轮修复）运行：

- **8 failed / 2 passed**——失败的正是全部 P3 相关断言（guard、guess_market、skipped 语义、两处写库守卫）
- 通过的 2 个是**对照组**（真实数据照常写库、成功路径行业映射优先），在新旧代码下都应通过

即：新测试确实能抓出旧 bug，不是空转。

## 实跑验证汇总

| 项 | 结果 |
|---|---|
| `tests/test_hk_us_preheat_codes.py` | **24/24**（含 P3-1 guard 断言与新增 `test_guess_market_and_collection_routing`） |
| `tests/test_hk_us_preheat_meta_throttle.py` | **10/10**（无回归） |
| `tests/test_akshare_batch_no_placeholder.py` | **4/4** |
| `tests/test_stock_sync_single_akshare_basic.py` | **3/3** |
| `tests/test_akshare_provider_basic_info.py` | **2/2**（pytest 风格无 runner，内联驱动执行） |
| `tests/test_akshare_manual_mode.py` / `test_akshare_sync_service.py` / `test_symbol_field_fix.py` | **1/1、3/3、5/5**（排查批量守卫回归） |
| pytest 目标子集（7 文件） | **44 passed** |
| `vue-tsc --noEmit` | **0 错误**（全量） |
| `py_compile`（11 个改动文件） | **OK** |
| 真实 MongoDB | 见上表（占位行 0、名称解析六市场样例全对） |

## 环境备注（本轮为验证所为的变更）

- 经 `uv pip install` 向 `.venv` 装入了 `pytest` / `pytest-asyncio`（`requirements-ci.txt` 已声明的 CI 依赖，非新增依赖）
- 为实测数据库启动了本地 mongod（`.runtime/mongodb/bin/mongod --dbpath .runtime/data/mongo`），**验证后保持运行**（项目本地依赖的常态）

## 遗留（均不阻断提交）

1. `tests/test_akshare_historical_error_handling.py` 是**既有失效测试**：import `stock_sync._build_single_historical_sync_result`，该函数在 HEAD 也不存在（diff 未触碰），与本轮无关。建议后续删除或改名。
2. `guess_market` 对 5 位纯数字一律归港股——若有人手工输入缺前导零的 A 股码（如 `00001`），名称解析不再补零查 A 股库。前端分析流程传出的都是 6 位规范化码，实际不可达；且与项目既定口径（≤5 位→hk）一致，记录备查。
3. 全量 `pytest tests/` 收集即中断：`tests/qmt/test_qmt_download.py`、`test_mongodb_check.py` 等历史脚本在模块级 `sys.exit(1)`，属既有问题，与本次改动无关。

## 结论

P3-1 / P3-2 / P3-5 及追加的占位行清理、两道写库守卫**全部闭环且实测属实**；P3-3 / P3-4 按记录备查处理，判断合理。测试在新旧代码上的对照证明回归有效。**建议合入**（15 个改动文件 + 2 份报告一并提交）。
