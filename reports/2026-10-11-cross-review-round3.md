# 交叉 Review 第三轮：修复反馈核验（e5159fe1）

- 日期：2026-10-11 22:45
- Review 范围：`7b682d25..e5159fe1`（单提交，6 文件，+676/-13）
- Review 方式：通读全部 diff + 现状代码逐点核对 + 实跑两套单测（13/13、10/10）+ 三处后缀集合运行时比对 + 工作区状态检查

## 总体结论

**同意反馈全部内容。** 8 项处理全部落实，3 处对第二轮报告的更正全部属实（我接受）。另发现 1 个 P3 级残留（暴跌保护会锁存）和 2 个微小观察，均不阻塞合入。

## 逐项核验结果

| 项 | 反馈结论 | 核验结果 |
|---|---|---|
| P0 文档二次覆盖 | ✅ 属实，已恢复 | `git restore` 生效：HEAD 811 行 == 工作区 811 行，`git status` 干净；`/tmp` 备份 685 行与反馈一致；第 11/12/13 节俱全。根因（format-on-save 旧 buffer）与第二轮判断一致 |
| P1 Wind 兜底砍历史 | ✅ 属实（范围更小），已修 | 分支 (3) 按 `has_history` 分流：有历史 → `_upsert_daily(from_scratch=False)` 增量 + 按日期清异源；无历史 / `force_full` → `max(limit, lookback)` 取数 + `_write_single_source(allow_shrink=force_full)`。`_write_single_source` 新增 `allow_shrink` 闸：`other_rows > len(items)` 时拒绝切换返回 0（实跑日志可见「20 根历史拒绝 1 行切换」warning）。`p["last"]` 来自 `_last_trade_date`（库内最大交易日），`has_history` 判定可靠 |
| P2 paper.py 第三份识别 | ✅ 属实，已对齐 | 逐行比对两份 `_detect_market_and_code`：同正则、同后缀优先、同单字母类别、同 fallthrough；仅注释与 `_zfill_code`/`zfill(6)` 等价差异。测试固化两实现一致 |
| P3-1 自选股 BRK-B 不归一 | ✅ 属实，已修 | 端到端通：自选股 `BRK-B` → `to_internal_symbol` → `_us_ticker` → `BRK.B`；`to_yahoo_ticker` → `BRK-B`（Yahoo 侧正确）。不会再与指数侧分裂成两条 universe 记录 |
| P3-2 AAPL.O 被当类别 | ✅ 属实，已修 | 后缀优先生效：`AAPL.O→AAPL`、`CBOE.BAT→CBOE`、`GOOGL.OQ→GOOGL`、`BRK.B`/`BRK-B→BRK.B` 两处一致；多字母未知尾段（如 `BRK.XY`）落默认 CN——合理 |
| P3-3 部分拉取敏感 | ✅ 属实，已修 | 80% 阈值 + 存量 ≥20 + 跳过清理 + 告警 + `stats` 记 `existing_n`，位置正确（在 members 非空检查之后） |
| P3-4 测试未落库 | ✅ 属实，已补 | `tests/test_hk_us_preheat_codes.py` 13 例已提交，实跑 13/13 通过；节流测试 10/10 无回归。覆盖：代码转换、purge 按日期、allow_shrink 双向、upsert 保历史、暴跌保护三态、两实现一致性 |
| P3-5 prev_close 跳格 | ❌ 误报 | 确认误报：`_build_daily_ops` 的 `continue` 不触碰 `prev_close`（仅在 append 成功后赋值），下一有效行衔接到上一**有效**行收盘，链不断。第二轮「跳格」说法有误，接受更正 |

## 对报告三处更正的核验

1. **P1 范围夸大 —— 属实。** `_write_single_source` 全仓库仅 2 个调用点（`hk_us_preheat_service.py:877`、`:913`），均在 `_write_daily_target` 内；`_sync_daily_per_symbol` 走 `_upsert_daily`。第二轮把路径 A 分支 (b) 列入受影响范围是我搞错了，该路径按日期精确清异源、本来就安全。
2. **P1 修法更优 —— 属实。** `max(limit, lookback)` 只保深度、本质仍是整源替换；语义分流 + `allow_shrink` 闸同时保住深度与来源构成。且分支 (1) 前置判断 `len(w_items) > max(len(raw), existing)` 使闸在该路径数学上不可能触发（纯保险），无行为回归风险。
3. **P3-5 误报 —— 属实。** 见上表。

## 新观察（均不阻塞，按需处理）

1. **P3｜暴跌保护会「锁存」（latch）**：合法 >20% 缩股时（如 HSTECH 一次调仓摘 7 只、30→23），跳过路径不写库 → `updated_at` 不前进 → `_index_fresh` 永远 False → 下次刷新同值再比再跳 → **直到 `force=True` 才自愈**。文档 13.4 写的「保留旧清单下次自愈」只对「上游瞬时失败后恢复」成立。后果轻（多预热几只老成分 + 每次刷新告警，无数据正确性问题），但建议二选一：① 两击规则——连续两次缩水才接受新清单；② 让 `_fetch_recursive` 上报「是否被截断」，按截断事实而非比例触发。
2. **微小｜`identify_stock_market`（`tradingagents/utils/stock_utils.py`，分析流）仍不认多字母交易所后缀**：`CBOE.BAT → UNKNOWN`（单字母 `AAPL.O` 可判 US）。用户基本不会在分析入口输后缀，影响小；如要彻底，可把三处 `_US_EXCH_SUFFIXES` 收敛到该文件统一导出。
3. **微小｜后缀集合防漂移**：三处集合当前实跑比对一致（11 个后缀全等），但测试只固化了 stocks↔paper 两份；建议加一行断言把 service 的 `_WIND_US_EXCH_SUFFIXES` 也圈进测试。

## 结论

反馈全部属实且处理到位，`e5159fe1` 具备合入条件。剩余三项为可选加固，不阻塞。
