# 第六轮遗留 P3 闭环核验：就绪检查按市场统计（71e8ce6d）

- 日期：2026-10-12
- 对象：`71e8ce6d fix(health): 就绪检查按市场统计基础数据（第六轮 Review 遗留 P3）`
- 范围：2 文件，+259/-4（`app/routers/health.py`、`tests/test_health_readiness_markets.py` 新增）

## 结论

**P3 已闭环，口径决策合理，无新引入问题。可以提交（已是 commit `71e8ce6d`）。**

## 口径评审（本条的核心是决策，不只是代码）

第六轮报告要求「先定口径再修」。提交采用：**任一市场（cn/hk/us）有基础数据即视为已同步；`basics_count` 为三市场合计；新增 `basics_by_market` 明细**。

评审意见：**采纳正确**。
- readiness 是首页引导卡片的**全局系统就绪**语义，没有市场上下文。若按 `market_type` 分流，「系统是否就绪」将失去唯一答案，且引导卡片无从选择市场——比现状更糟。
- 精确到单股的门槛已由 `/api/analysis/precheck`（7cb0ac6f，按 `market_type` 分流）承担；readiness 只回答「这个部署是否已导过数」。两层职责分离清晰。
- 保留拦截：三市场皆空仍 `basics_synced=false / next_step=sync_data`，未因改动放宽（有用例固化）。

## 逐点核验

| 检查点 | 结果 |
|---|---|
| 集合名来源 | ✅ 走 `basic_info_collection()`（函数内 import），无硬编码；`_READINESS_MARKETS` 单一常量 |
| 两处分支共用 | ✅ jdyun 特化分支（原 87 行）与常规分支（原 131 行）均改用 `count_basics_by_market`，无第三处遗漏 |
| 单市场异常隔离 | ✅ 逐市场 try/except 按 0 计并 `logger.warning` 留痕，不拖垮整体（有用例） |
| 前两步优先级 | ✅ 数据源 → LLM → 基础数据的次序未动（有用例：缓存型数据源仍拦 `configure_datasource`） |
| 兼容性 | ✅ 唯一消费方 `Dashboard/index.vue` 只读 `basics_synced`/`next_step`（已 grep 证实不读 `basics_count`）；`basics_by_market` 为纯新增字段；后端无其他调用方。`basics_count` 语义由「A 股数」变为「三市场合计」对现存消费方零影响 |
| 依赖完备性 | ✅ `Dict`（typing 已在 6 行导入）、`logger`（9 行）、`is_jdyun_mode`（同模块 12 行定义，测试 patch 目标正确） |

## 实跑验证

| 项 | 结果 |
|---|---|
| 新测试（直接 + pytest） | ✅ 9/9 |
| 相关套件回归 | ✅ **62 passed**（health 9 + precheck 9 + preheat codes/meta throttle + akshare 占位守卫/单股同步/provider/manual mode） |
| **真实库端到端** | ✅ `count_basics_by_market` = `{cn:11146, hk:674, us:517}` 合计 12337（cn 为 11146 系此前清理占位行后口径）；`system_readiness()` 返回 `basics_synced=True / overall_ready=True / next_step=ready`，`basics_by_market` 明细正确 |
| **非空转证明** | ✅ ① 行为探针：旧代码（7cb0ac6f worktree）+ 仅 `_hk` 有数据的假库 → `basics_synced=False / next_step=sync_data`，旧 bug 复现；② 新测试拷入旧 worktree 运行 → **collection 阶段即 ImportError**（`count_basics_by_market` 不存在），9 例全失败 |
| `py_compile` | ✅ health.py + 新测试 OK |

## 遗留观察（1 条，不阻断）

1. **全库故障时的可诊断性略有变化**：旧实现 DB 整体不可用时 `count_documents` 抛错 → 外层 handler 返回 `next_step=error` 并带原始错误；新实现逐市场捕获后按 0 计，三市场全失败时表现为 `next_step=sync_data`「请同步股票基础数据」，而非直白的 DB 错误。仍是** fail-closed**（不会误报就绪），且每个市场的失败都有 `logger.warning` 留痕。属「隔离单点故障」与「整体故障可诊断性」之间的固有取舍，可接受；如在意，可在 `count_basics_by_market` 返回后加一句「三市场全部统计失败则抛给外层」的判断，一行的事，但触发条件（全库故障）本身罕见，建议不动。

## 收尾状态

「港美股被当成 A 股查」一类问题在已知入口（数据校验 / 名称解析 / 分析预检 / 系统就绪）均已按市场分流闭环。本轮无新增待办。
