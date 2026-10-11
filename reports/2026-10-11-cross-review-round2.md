# 交叉 Review 第二轮：P0~P3 修复验证（c4fec099）

- 日期：2026-10-11 22:40
- Review 范围：`fcc61a8c..c4fec099`（用户指定的修复提交）；另注意到 HEAD 已前移至 `7b682d25`（节流优化，范围外，末尾附简要结论）
- Review 方式：通读全部 diff + 现状代码核对（_merge_favorites / _load_targets / _write_single_source / paper.py 平行实现）+ 实跑提交内的单测 + 工作区状态检查

## 总体结论

**修复全部落实且方法多数优于原建议**（尤其 P1 按日期精确清理替代删光异源、P2-b 的三态代码转换）。逐项验证 + 一个新 P1 + 一个复发的 P0。

## 逐项核验结果

| 项 | 结论 | 核验要点 |
|---|---|---|
| P0 文档恢复 | ✅ 提交内已完成（恢复 v3 + 第 11 节修正记录，内容与实现相符） | 但**工作区二次复发**，见下 |
| P1 重复交易日 | ✅ 修法正确 | `_purge_conflicting_dates` 在 `_upsert_daily` 内按本次 dates 删异源，路径 A 覆盖；`_write_single_source` 仍是整源切换（先删异源再写全量 lookback），两者语义互补不冲突 |
| P2-a 美股后缀补全 | ✅ 设计合理 | `$not` 正则查询、批量反查 + 逐只兜底、`windcode_matches` 校验、`yahoo_only` 三链路跳过均正确；`_base_ticker` 对 `BRK_B`/`BRK_B.N` 均归一 `BRK.B`；`BRK.B→BRK.L` 会被拒 |
| P2-b 点号类股票 | ✅ 主体修得好 | `to_yahoo_ticker("CBOE.BAT")→CBOE` ✓；白名单不收单字母 ✓；`windcode_matches("AAPL.O","AAPL.N")=True`、`("BRK_B.N","BRK.L")=False` ✓；两处后端识别放行且置于默认 A 股之前 ✓ |
| P3-1 内存 | ✅ | 拆 `_yahoo_download_chunk` 单批 + 即时写库释放；附带处理 yahoocode 缺失/撞号退化 ✓ |
| P3-2 薄覆盖口径 | ✅ | 改为 `existing < _MIN_YAHOO_ROWS or force_full`，AVB 不再白打 ✓ |
| P3-3 前端 | ✅ | `looksLikeTruncatedCNCode`（5 位 1/5 开头）合理；双向联动正确（港股/美股下输 600519/AAPL 会切）；ETF 作为当前市场仍会切走——与改前一致，无回归 |
| P3-4 universe 清理 | ✅ | `_prune_source` 只摘该来源标签、多来源保留、无来源才 `enabled=False`；成员/自选的 UpdateOne 均带 `enabled: True`，被摘标的重新入池会恢复 ✓；stats 口径统一 ✓ |
| P3-5 索引 | ✅ | `market_quotes.{data_source:1}` 已加 ✓ |

## 🔴 P0（复发，第二次）：工作区设计文档再次被旧内容覆盖

`git status` 显示 `docs/design/hk-us-preheat-cache.md` 又有**未提交修改**，且工作区版本是 **c4fec099 时期的内容变体**（表格被整体重排，疑似编辑器 format-on-save），**缺少 `7b682d25` 追加的第 12 节（元数据节流 v5）**。

- 与上次的区别：上次是实施前 v2（474 行）；本次是带第 11 节但缺第 12 节、且全文件表格重排（685 行 vs HEAD 749 行，whitespace-insensitive diff 仍 100+ 行）。
- 风险：此刻 `git commit -a` 会丢第 12 节 + 产生全文件噪声 diff。
- 处理：`git restore docs/design/hk-us-preheat-cache.md`（确认无需保留本地格式）；如确想要重排版式，基于 HEAD 版本另做一次纯格式化提交。
- 根因建议：编辑器对该文件 format-on-save + 旧 buffer 已两次闯祸，建议关掉该文件自动格式化，或提交前必跑 `git status`。

## 🔴 P1（新发现，超出本次修复范围）：分支 (3) Wind 兜底仍会砍掉历史

位置：`_write_daily_target` 分支 (3)（批量路径）与 `_sync_daily_per_symbol` 分支 (b)（路径 A）。

```python
elif allow_wind:
    # (3) Yahoo 完全没数据 → Wind 单只兜底
    w_items = await asyncio.to_thread(self._wind_kline, t["windcode"], p["limit"])  # ← 增量小窗
    ...
    rows_written = await self._write_single_source(coll, t, w_items, "wind", ...)   # ← 先删光全部 Yahoo 行
```

- `p["limit"]` 是增量窗口（gap+5 天，可能只有几天），而 `_write_single_source` 会 **先 `delete_many` 该 symbol 全部异源行**再写 → 250 根 Yahoo 历史被砍成几根。
- 触发条件不苛刻：① 整批 Yahoo 下载失败（一批 60 只全部中招，`got={}` → 每只都走分支 3）；② 单只 Yahoo 临时无数据/被限流；③ Yahoo 下架某 ticker。
- 这与本次修复坚持的「已存历史永不缩短」不变式直接相悖；属提交前既存路径，但恰在刚刚硬化的同一区域，且批量化后整批失败会放大成 60 只一起砍。
- 建议（二选一或都做）：
  1. 分支 (3) 的 Wind 取数改用 `max(p["limit"], p["lookback"])`（与分支 (1) 一致），保证切换来源时历史深度不缩水；
  2. `_write_single_source` 增加保险：`len(items) < 现有异源行数` 且非 `force_full` 时拒绝切换（保历史优先，记 failed/skip）。

## 🟡 P2（遗留）：`paper.py` 第三份市场识别未同步更新

`app/routers/paper.py:38` 有一份平行的 `_detect_market_and_code`，仍是 `^[A-Z]+$`（纯字母才判 US）。显式传 `market="US"` 时不受影响（`_resolve_market_and_code` 短路），但走自动检测时 `BRK.B` 仍会落入默认 CN。本次修复更新了 stocks.py 与 stock_utils.py 两份，漏了这份。建议同步修改，或趁机把三份收敛成一个公共函数（`tradingagents/utils/stock_utils.py` 已是现成位置）。

## 🟢 P3（遗留/建议）

1. **`_merge_favorites` 美股分支不归一连字符**：自选股若存 `BRK-B`，`_us_ticker` 不含 dash 处理 → symbol 为 `BRK-B`，与指数侧 `BRK.B` 成为两条 universe 记录；且 K 线接口把输入 `BRK-B` 归一为 `BRK.B`，该条目数据永远命中不了缓存。建议 `_us_ticker`（或自选股分支）把 `-` 也归一为 `.`。
2. **`stocks.py` 新类别分支会吞掉交易所后缀形式**：`^([A-Z]{1,5})[.\-]([A-Z])$` 对 `AAPL.O` 也命中，原样返回 `AAPL.O`（不剥后缀）→ 缓存查询失配。用户不会这样输入，但建议把已知交易所后缀（O/N/BAT…）排除在类别字母之外。
3. **`_prune_source` 对部分拉取敏感**：某字母桶瞬时失败但其它桶成功时，缺失成分会被误摘/误禁用（下次成功刷新自愈，期间少预热一批）。可在 members 数相对历史暴跌时跳过清理。
4. **测试未落库**：提交信息称 27 例全过，但仓库中只提交了 `7b682d25` 的 10 例节流测试（已实跑，全过）；c4fec099 的 P1/P2 验证（4 行 0 重复构造、代码转换 27 例）没有固化。建议补 `tests/test_hk_us_preheat_codes.py`——`_us_ticker`/`windcode_matches`/`_purge_conflicting_dates` 正是最易回归处。
5. `_upsert_daily` 新增的 close 过滤会丢弃无收盘价行：若无效行夹在有效行中间，其后行的 `prev_close` 链会跳过一格（影响极小，无效行本就不该入库，记录备查）。

## 范围外：`7b682d25`（节流优化）简评

HEAD 已前移到该提交（不在指定 review 范围）。快速核对：`_as_naive_dt`/`_is_fresh`/`_split_fresh` 实现正确（naive/aware/ISO/非法值均处理）；与 `yahoo_only`、`_resolve_us_windcodes` 交互无冲突（新入池标的无 basic/financial 记录 → stale → 会刷新）；10 例单测实跑全过。如需细审可说一声。

## 遗留事项确认（与用户反馈一致）

后端进程仍为旧代码，K 线缓存命中与两个 cron 需重启生效——已记录，不属代码问题。
