# 交叉 Review 第四轮：第三轮反馈落实核验（3f24bd51）

- 日期：2026-10-12 18:50
- Review 范围：`e5159fe1..3f24bd51`（单提交，8 文件，+534/-97），已推送到 origin/main
- Review 方式：通读全部 diff + 现状代码逐点核对 + 实跑两套单测（21/21、10/10）+ 调用点/导入链/常量核查 + 工作区状态检查

## 总体结论

**第三轮的三项反馈全部落实，且主项（锁存）的修法比我建议的更好——可以收了。**

## 逐项核验

| 项 | 第三轮反馈 | 核验结果 |
|---|---|---|
| P3 锁存（主项） | 比例当完整性代理 + 跳过不写库 → 永久锁存 | ✅ 已重做。`_fetch_recursive` 深度耗尽仍满页 → `state["truncated"]=True`；`_search_members` 异常 → `state["failed"]` 记录；`_fetch_index_members` 返回 `(members, state)`；`refresh_universe` 按事实分流——不完整则**增量补写 + 跳过清理**（补写推进 `updated_at`，锁存根除），完整则照常清理、合法缩水只告警不拦截。`_INDEX_SHRINK_GUARD`/`_MIN_INDEX_MEMBERS_FOR_PRUNE` 降级为日志分级，不再参与清理决策 |
| 观察 2 分析侧后缀 | `CBOE.BAT → UNKNOWN` | ✅ 已修。补 `^[A-Z]{1,5}[.\-]([A-Z]{2,3})$` + 共享集合判定；顺带修了 `700.HK → UNKNOWN`（放宽为 `^\d{1,5}\.HK$`，裸数字分支仍 `^\d{4,5}$` 不变，A 股三位新股不会被误判） |
| 观察 3 后缀漂移 | 四处字面量副本 | ✅ 已收敛到 `app/core/market_codes.py`（零依赖 frozenset），preheat/stocks/paper/分析侧四处 `import` 同一对象；`app/__init__.py` 与 `app/core/__init__.py` 均为空壳，无重导入链问题；反向依赖先例属实（`trading_graph.py:41` import `app.core.jdyun`） |

## 实现细节抽查（均通过）

- **锁存真修复**：不完整路径的 `bulk_write(ops)` 里 `$set` 含 `updated_at: now`（每轮必变），`_index_fresh` 因此必然前进——测试 `test_incomplete_fetch_advances_freshness_and_does_not_hammer` 实跑验证：首轮 fetch=1、`_index_fresh` 转 True、同周期二轮 fetch=0。
- **合法缩水被接受**：`test_complete_shrink_is_accepted`（30→23）断言清理执行、7 只摘标签且 `enabled=False`、stats/total=23。
- **签名变更无外溢**：`_fetch_index_members` 全仓库仅 `refresh_universe`（:335）与测试调用；`Tuple` 已在 typing 导入中。
- **截断判定保守性**：满页（100）且 depth≥3 才标 truncated。S&P500 不存在同 3 字母前缀 ≥100 只；港股通约 600 只，3 数字前缀 ≥100 的可能性极低；即使误判，代价仅「跳过一个清理周期 + 告警」，下周期自愈。
- **已文档化的取舍**：完整拉取下的静默丢行按调仓接受，误摘下周期可自愈（`_prune_source` 只摘标签/置 enabled=False，恢复正常会重新写入启用）。这是合理取舍——与「完全无出口的锁存」二选一，选对了。
- **测试质量**：21 例非空转——`_prune_source` 改为「套壳记录 + 真实执行」，断言看库内状态而非调用计数；`_FakeColl` 补了 `$addToSet`/`$pull`。
- **工作区**：`git status` 干净，HEAD==origin/main；设计文档第 14 节修正记录与实现相符（含「真实库预演未执行」的诚实标注）。

## 实跑验证

- `tests/test_hk_us_preheat_codes.py`：**21/21 通过**
- `tests/test_hk_us_preheat_meta_throttle.py`：**10/10 通过**（无回归）

## 结论

第三轮反馈全部闭环，无遗留、无新问题。`3f24bd51` 已在 origin/main，本轮无需任何行动。唯一提醒仍是老两条：后端进程需重启才能让新的 universe 逻辑与缓存命中生效；该设计文档的 format-on-save 已被旧 buffer 坑过两次，提交前跑 `git status`。
