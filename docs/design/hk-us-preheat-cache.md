# 港美股行情预热缓存 设计文档

- 版本：**v3**（v2 需求已落地实施，含全量首跑实测与 3 处批量优化）
- 日期：2026-10-11
- 状态：**已实施并完成首次全量预热**
- 前置改动：`b4bb7299` 个股分析页恢复港股/美股入口（前端已改完，后端未动）

---

## 0. v2 变更摘要（相对 v1）

### 0.1 用户决策响应

| # | 用户决策                       | 采纳情况         | 关键约束                                    |
| - | -------------------------- | ------------ | --------------------------------------- |
| 1 | 港股加恒生科技 + 港股通；美股加标普500   | ✅ 采纳，但有重大约束  | ⚠️ Wind `search_stocks` **100 条上限**，港股通/标普会被截断 |
| 2 | 自选股逐步加入，预热跟着走              | ✅ 采纳，但发现断链   | 🐛 现有同步任务读的是**已废弃的旧集合**，必须一并修复           |
| 3 | 港美股财务数据一起预热                | ✅ 采纳         | 新增 `stock_financial_data_hk/us` 两个集合    |
| 4 | 小批量试探后汇报，询问是否继续            | ✅ 采纳         | 实施步骤加 **Gate-1 检查点**（见第 7 节）            |
| 5 | 本期不做实时行情；先搞清 Yahoo 怎么加入    | ✅ 采纳         | 🎯 **重大发现：Yahoo 已完整集成，且现在是港美股第一主力源**    |

### 0.2 对 v1 错误的更正（重要）

| v1 的说法                                | v2 更正（实测）                                                                            |
| ------------------------------------- | ------------------------------------------------------------------------------------ |
| 「港股实际只有 Wind 一个源在干活」                  | ❌ **完全错了**。实测 `_get_source_priority('HK')` → `['yfinance', 'akshare']`。**港股现在是 Yahoo Finance 在第一位供数** |
| 「`yahoo_finance` 是无适配器占位、永远轮不到」       | ❌ 在 `DataSourceManager`（K 线降级链）里确实如此，但**在港美股专属链路 `ForeignStockService` 里它是有效 handler** |
| 「数据库没有自选股集合」                          | ❌ 自选股存在 **`users.favorite_stocks`（内嵌数组）**，不是独立集合，所以按集合名没搜到                             |
| 标的范围「默认 185 只」                        | ⚠️ 大幅上调至约 **1100 只**（见 4.1）                                                          |
| 「财务数据二期再做」                            | ✅ 按决策 3，提至本期                                                                         |

---

## 1. 背景与问题

### 1.1 前端入口已打开，但后端没有缓存

`b4bb7299` 让「个股分析」页可以选港股/美股了。但港美股数据在后端**完全没有 MongoDB 缓存**：

| 项      | A 股                              | 港美股                                          |
| ------ | -------------------------------- | -------------------------------------------- |
| 定时同步任务 | 15 个（`app/main.py`）              | **0 个**（grep 无结果）                            |
| 日线集合   | `stock_daily_quotes` 1,062,774 条 | `stock_daily_quotes_hk` / `_us` **不存在**      |
| 基础信息集合 | `stock_basic_info` 11,146 条      | `stock_basic_info_hk` / `_us` **不存在**        |
| 财务集合   | `stock_financial_data` 4,960 条   | `stock_financial_data_hk` / `_us` **不存在**    |
| 写入方    | `historical_data_service` 等      | **没有任何写入方**                                  |

### 1.2 不预热的直接后果

1. **每次分析都实时打外部 API**。一次 3 级深度分析要跑市场/基本面/新闻/社交多个分析师，每个都要拉 K 线 + 财务 + 新闻，串行累计几十次调用。
2. **Wind 走 subprocess 调 CLI**（`wind_client.py`），单次开销远大于 HTTP API。
3. **数据检验必然失败**。分析页「数据检验」调 `data_validation_service`，按市场读专用集合（`:226-231/270-275/619-620`），集合不存在 → 永远报缺失。
4. **时差错位**。美股交易在北京时间 21:30–04:00，白天实时取拿到的往往不是最新收盘价。

---

## 2. 现状架构（关键事实，附行号）

### 2.1 K 线接口在市场检测后就分流了

`app/routers/stocks.py:963-985`：

```python
market, normalized_code = _detect_market_and_code(code)

if market in ['HK', 'US']:                       # ← 港美股在这里被分流走
    service = ForeignStockService(db=db)
    kline_data = await service.get_kline(market, normalized_code, period, limit, force_refresh)
    return ok(data={... 'source': 'cache_or_api'})

# ↓ 以下 local → MongoDB缓存 → 外部降级链 四级链路，只有 A 股才走
```

**港美股 K 线根本不读 `stock_daily_quotes`**，走的是 `ForeignStockService`。

> ⚠️ **修正 v1 的一个错误建议**：v1 建议"复用 `stock_daily_quotes` 集合"——不可行。港美股进不了那段代码；即使进了，`mongodb_cache_adapter.get_historical_data:188` 的 `zfill(6)` 会把港股 `00700` 补成 A 股 `000700`。**结论：必须用独立集合。**

### 2.2 🎯 港美股现在实际在用哪个源（v2 新增，实测）

`ForeignStockService` 的 `source_handlers` **只认固定几个 key**，Wind 不在此列：

| 市场 | `source_handlers`（`:213`/`:433`）      | 实测实际优先级                                   |
| -- | ----------------------------------- | ---------------------------------------- |
| HK | `yahoo_finance`, `akshare`          | `['yfinance', 'akshare']`                |
| US | `yahoo_finance`, `alpha_vantage`, `finnhub` | `['yfinance', 'alpha_vantage', 'finnhub']` |

**这意味着：**

1. **Yahoo Finance 已经是港美股第一主力源**，不是"待加入"，而是"已在服役"。
2. **Wind 的 `wind=4`（港股分组）在这个链路里不生效**——它只在 `DataSourceManager`（A 股 K 线降级链）里生效。想在港美股链路用 Wind，必须二期给 `source_handlers` 加 wind 分支。
3. 配置里有两条脏数据：`Alpha Vantage`（带空格）、`Yahoo Finance`（带空格），lower() 后是 `alpha vantage` / `yahoo finance`，**匹配不上 handler key，永久无效**，可清理。
4. 港股分组里 `AKShare` 被 `enabled=False`，所以 HK 实际只有一个 Yahoo 在跑，**没有兜底**。

### 2.3 Yahoo Finance 可用性实测（v2 新增）

| 项目       | 结果                                                    |
| -------- | ----------------------------------------------------- |
| 是否已集成    | ✅ 完整集成（报价/基础信息/K 线/新闻，港美股均有）                          |
| 依赖包      | `yfinance 1.7.0` 已安装                                  |
| 直连可用性    | ✅ **无需任何配置即可访问**                                      |
| 港股 0700.HK | Close **424.80**，耗时 1.9s                              |
| 美股 AAPL    | Close **336.64**，耗时 0.3s                              |
| 与 Wind 交叉验证 | ✅ **完全一致**（Wind 港股 424.8 / 美股 336.64）                 |
| 前置条件     | ⚠️ 依赖本机系统代理 `127.0.0.1:7897`（`.env` 里的 HTTP_PROXY 是注释状态，实际走 macOS 系统代理） |

> **风险**：Yahoo 走的是本机 Clash 系统代理。**代理一关，Yahoo 全部失效**，港美股将退回无数据状态。因此预热数据源必须能在 Yahoo 挂掉时自动切换（见 4.7 主备双源设计）。

### 2.4 数据检验的集合映射（决定了我们要写哪些集合）

`data_validation_service.py`：

| 用途                 | cn                     | hk                        | us                        |
| ------------------ | ---------------------- | ------------------------- | ------------------------- |
| 基础信息（`:226-231`）   | `stock_basic_info`     | `stock_basic_info_hk`     | `stock_basic_info_us`     |
| 日线（`:270-275`）     | `stock_daily_quotes`   | `stock_daily_quotes_hk`   | `stock_daily_quotes_us`   |
| 财务（`:619-620`）     | `stock_financial_data` | `stock_financial_data_hk` | `stock_financial_data_us` |
| 实时（`:662-663`）— 本期不做 | `market_quotes`        | `market_quotes_hk`        | `market_quotes_us`        |

查询用 `{"$or": [{"symbol": s}, {"code": s}]}`（`:234`、`:289`），symbol 或 code 任一匹配即可。

### 2.5 自选股存储结构（v2 新增，关键）

**不是独立集合**，是 users 文档的内嵌数组：

```
users.favorite_stocks: [
  { stock_code, stock_name, market, added_at, tags, notes, alert_price_high, alert_price_low }
]
```

- 写入方：`favorites_service.add_favorite`（`:378` `$push` 到 `users.favorite_stocks`）
- `market` 字段默认 `"A股"`，港美股会被记为 `"港股"` / `"美股"`
- 旧结构 `user_favorites` 集合已废弃（当前**不存在**）
- 当前实际数据：**0 条**

> 🐛 **发现的断链 Bug**：`favorites_sync_task.get_all_favorite_a_stocks()`（`:38-58`）读的是**旧的 `user_favorites` 集合**，且 `market == "A股"` + `zfill(6)`。
> **后果：现在你在前端加的任何自选股，A 股同步任务都抓不到**（它读的是一个不存在的集合）。这是需求 2 能否成立的前提，必须一并修复。

### 2.6 代码格式规范（必须严格对齐）

| 环节                                                    | 港股                | 美股                        |
| ----------------------------------------------------- | ----------------- | ------------------------- |
| 库内存储 / API 传入                                         | `00700`（5 位数字）    | `AAPL`（字母）                |
| `_detect_market_and_code` 输出（`stocks.py:373/377/381`） | `('HK', '00700')` | `('US', 'AAPL')`          |
| Wind 需要                                               | `00700.HK`        | `AAPL.O`（纳斯达克）/ `.N`（纽交所） |
| Yahoo 需要                                              | `0700.HK`（4 位）    | `AAPL`                    |
| Wind adapter `_to_windcode`（`wind_adapter.py:93-104`） | 5 位数字 → 补 `.HK`   | **字母原样透传**                |

⚠️ **实测坑**：Wind 传 `AAPL`（无后缀）**返回空**，必须带 `.O`/`.N`。
⚠️ **第二个坑**：标普500 里混有两种后缀 —— `AAPL.O`（纳斯达克）和 `ABT.N`、`A.N`（纽交所），不能一律拼 `.O`。

---

## 3. 目标与非目标

### 目标

1. 港美股日线 + 基础信息 + 财务预热进 MongoDB，K 线接口命中缓存即返回（零外部调用）
2. 分析页「数据检验」对港美股在三个维度（基础信息 + 日线 + 财务）均通过
3. **标的清单 = 指数成分 ∪ 自选股**，新增自选股后下一次同步自动纳入
4. 同步范围可控、幂等、可断点续跑，不冲击现有 A 股任务

### 非目标（本期不做）

- 分钟线
- **实时行情**（`market_quotes_hk/us` 本期不建，盘中仍走实时源）
- 全市场同步（港股 2600+、美股 10000+，量太大）
- 给 `ForeignStockService.source_handlers` 加 wind 分支（二期）

---

## 4. 方案设计

### 4.1 标的清单（已按你的决策扩大）

Wind `search_stocks` 直接返回**带正确后缀**的代码，实测结果：

| 清单                   | 返回条数 | 实际成分数 | 状态                    |
| -------------------- | ---- | ----- | --------------------- |
| 恒生指数 `HSI.HI`        | 95   | ~85   | ✅ 完整（可能含重复，需去重）       |
| 恒生科技 `HSTECH.HI`     | 30   | 30    | ✅ 完整                  |
| **港股通**              | 100  | 500+  | ❌ **被截断**，需分批         |
| 纳斯达克100 `NDX.GI`     | 100  | 101   | ✅ 基本完整                |
| **标普500 `SPX.GI`**   | 100  | 503   | ❌ **被截断**，需分批         |

> ⚠️ **硬约束：`search_stocks` 单次最多返回 100 条。** 加"不要省略""全部"等措辞**无效**，仍是 100 条。

#### 解决方案：按首字母分批（已实测可行）

```
"标普500成分股中代码以A开头的股票" → 51 条
"标普500成分股中代码以B开头的股票" → 22 条
"标普500成分股中代码以C开头的股票" → 52 条
... 实测 8 个字母去重后 205 只
```

遍历 A–Z（26 次调用）即可覆盖完整的 503 只标普成分股。港股通同理。

**设计**：配置项 `HK_US_PREHEAT_UNIVERSE`，清单定义如下：

| 市场 | 清单                    | 获取方式       | 约标的数   |
| -- | --------------------- | ---------- | ------ |
| 港股 | 恒生指数                  | 单次查询       | 85     |
| 港股 | 恒生科技                  | 单次查询       | 30     |
| 港股 | **港股通**               | **26 字母分批** | ~500   |
| 美股 | 纳斯达克100               | 单次查询       | 101    |
| 美股 | **标普500**             | **26 字母分批** | ~503   |
| 港美 | **自选股**（`users.favorite_stocks`） | 读库         | 动态     |

去重后估计：港股约 **550 只**，美股约 **550 只**，合计约 **1100 只**。

### 4.2 清单落库 + 自选股动态并集（需求 2）

新建 `hk_us_preheat_universe` 集合，作为唯一标的源：

```javascript
{
  symbol: "00002",        // 内部格式
  windcode: "0002.HK",    // Wind 格式（带后缀）
  yahoocode: "0002.HK",   // Yahoo 格式
  market: "HK",           // HK / US
  name: "中电控股",
  source: "index:HSI" | "index:HSTECH" | "index:港股通" | "index:NDX" | "index:SPX" | "favorite",
  enabled: true,
  added_at, updated_at
}
```

**每次同步前的 Universe 刷新流程**：

```
1. 读取 all users.favorite_stocks，筛 market in (港股, 美股)
   → upsert 进 universe，source="favorite"
2. 若某指数的 index:* 条目 数量==0 或 超过 7 天未刷新
   → 重新调 Wind search_stocks（按 4.1 的分批策略）→ upsert
3. 同步标的 = universe.find({enabled:true})
```

这样**新增自选股 → 下次同步前自动进入 universe → 自动预热**，满足需求 2。

> 注意：这一步**必须修 2.5 的断链 bug**，否则读不到自选股。修复方式：把 `get_all_favorite_a_stocks()` 改成读 `users.favorite_stocks`（新版），并新增一个港美股版本。

### 4.3 代码映射

```
港股：0002.HK → strip(".HK") → zfill(5) → "00002"   （与 _detect_market_and_code 一致）
美股：AAPL.O  → split(".")[0]           → "AAPL"
```

入库时同时写 `symbol` 和 `code`（值相同），因为数据检验用 `$or` 匹配。

### 4.4 集合与字段设计

**① 日线**（字段对齐现有 `stock_daily_quotes`）

```
stock_daily_quotes_hk / stock_daily_quotes_us
{
  symbol, code, full_symbol, market,      # market = "HK" / "US"
  trade_date,        # "2026-10-09" 字符串
  period,            # "daily"
  data_source,       # "wind" / "yahoo"
  open, high, low, close, pre_close,
  volume, amount, change, pct_chg,
  created_at, updated_at, version
}
```

索引（照抄现有）：

```javascript
db.stock_daily_quotes_hk.createIndex({symbol:1, trade_date:1, data_source:1, period:1}, {unique:true})
db.stock_daily_quotes_hk.createIndex({symbol:1})
db.stock_daily_quotes_hk.createIndex({trade_date:-1})
db.stock_daily_quotes_hk.createIndex({symbol:1, trade_date:-1})
```

**② 基础信息** `stock_basic_info_hk` / `stock_basic_info_us`
至少含数据检验读取字段（`:235`）：`symbol`、`code`、`name`、`list_date`。

**③ 财务数据**（需求 3，v2 新增）`stock_financial_data_hk` / `stock_financial_data_us`

Wind 已实测可用：港股 0700.HK 2024 年 ROE 21.78%、营收 6602.57 亿；美股 AAPL ROE 157.4%。字段结构待实施时对齐现有 `stock_financial_data` 的读取约定。

### 4.5 同步流程（幂等增量）

```
1. refresh_universe()                      # 指数清单 + 自选股 → universe 集合
2. for each symbol in universe(enabled):
     a. 日线
        latest = 集合里该 symbol 最大 trade_date
        if latest 已是最新交易日 → skip
        start = latest or (今天 - LOOKBACK_DAYS)      # 首次 365 天
        rows = 主源.get_kline(windcode/yahoocode, begin=start, end=今天)
        if 主源失败 → 备源重试
        upsert（唯一键 symbol+trade_date+data_source+period）
     b. 基础信息：仅当不存在或超过 N 天未更新时刷新
     c. 财务：按报告期 upsert（本期只取最近 8 个报告期）
     d. 单只失败记日志跳过，不中断整批；记录失败清单便于重跑
```

幂等保证：唯一索引 + upsert + 失败清单，重复跑不会重复插入，也不会丢数据。

### 4.6 K 线接口接入缓存（核心改动，约 25 行）

在 `stocks.py:966` 的 `if market in ['HK', 'US']:` 分支**内部、调用 ForeignStockService 之前**插入查询：

```python
if market in ['HK', 'US']:
    coll = db["stock_daily_quotes_hk" if market == 'HK' else "stock_daily_quotes_us"]
    rows = await coll.find({"symbol": normalized_code, "period": mongodb_period}) \
                     .sort("trade_date", 1).to_list(length=limit * 2)
    if rows:
        return ok(data={'code': normalized_code, 'period': period,
                        'items': [...], 'source': 'mongodb_preheat'})
    service = ForeignStockService(db=db)      # 未命中才走实时
    ...
```

命中时 `source` 标为 `mongodb_preheat`，便于在接口响应里肉眼确认走了哪条路。

### 4.7 主源选型建议 —— 需要你决策（⭐ v2 新增）

既然 Yahoo 已可用且与 Wind 数据一致，选谁做预热主源成为一个新问题：

|               | Wind（已集成）             | Yahoo（已集成 + 实测可用）        |
| ------------- | --------------------- | ------------------------ |
| 费用            | **额度未知**（skill 内置 key） | 免费无限                     |
| 单次耗时          | ~0.7s（subprocess 开销）  | 0.3–1.9s（直连 HTTP）        |
| 数据质量          | ⭐ 最高（专业级，含复权、板块、财务）   | 良好，**与 Wind 交叉验证一致**     |
| 依赖            | wind-mcp-skill 必须存在   | **依赖本机代理 127.0.0.1:7897** |
| 批量 1100 只可行性  | 受限流风险，需试探             | ✅ 无限流风险                  |
| 断网/代理关闭时      | 仍可用（走 skill）          | ❌ 失效                     |

**我的建议：Yahoo 主 + Wind 备**。
理由：1100 只标的量级下 Wind 的额度风险不可控，而 Yahoo 免费无限且数据已验证一致；Wind 留作兜底和后续质量校验。
**备选：Wind 主 + Yahoo 备**（数据质量优先）。

→ **这是待你确认的第 1 项，见第 8 节。**

### 4.8 调度

照抄 `main.py:647` 范式（`scheduler.add_job` + `CronTrigger.from_crontab`）：

| 任务                | cron            | 说明                                |
| ----------------- | --------------- | --------------------------------- |
| `hk_preheat_sync` | `30 17 * * 1-5` | 港股 16:00 收盘后                      |
| `us_preheat_sync` | `0 7 * * 1-5`   | 美股北京时间 04:00/05:00 收盘后，留 2h buffer |

新增配置项（`app/core/config.py`）：

```
HK_US_PREHEAT_ENABLED = True
HK_PREHEAT_CRON = "30 17 * * 1-5"
US_PREHEAT_CRON = "0 7 * * 1-5"
HK_US_PREHEAT_LOOKBACK_DAYS = 365
HK_US_PREHEAT_PRIMARY_SOURCE = "yahoo"     # yahoo | wind
HK_US_PREHEAT_ENABLE_FINANCIAL = True
```

沿用现有惯例：未启用时 `scheduler.pause_job(...)`（参考 `main.py:631`）。

---

## 5. 备选方案对比

| 方案                              | 优点                      | 缺点                                            | 结论   |
| ------------------------------- | ----------------------- | --------------------------------------------- | ---- |
| **A（推荐）** 独立 hk/us 集合 + 改 K 线接口 | 符合上游集合设计；检验三项全通过；改动可控   | 需改 K 线接口 ~25 行                                | ✅ 选它 |
| B 写进 `stock_daily_quotes`       | 不用改 K 线接口               | **不可行**：港美股进不了那段；`zfill(6)` 会把 00700 变 000700 | ❌    |
| C 只预热文件缓存                       | 零集合改动                   | 不在 MongoDB → 数据检验仍失败                          | ❌    |
| D 给 ForeignStockService 加 wind  | K 线质量提升                 | **不解决缓存问题**，每次仍实时调                            | 二期   |

---

## 6. 风险与对策

| 风险                                  | 影响            | 对策                                                     |
| ----------------------------------- | ------------- | ------------------------------------------------------ |
| **Yahoo 依赖本机代理**                    | 代理关闭则港美股全部断供  | 主备双源自动降级；universe/同步失败率超阈值时告警；文档写明"Yahoo 需代理"          |
| `search_stocks` 100 条上限             | 港股通/标普清单不全    | 按 A–Z 首字母分批（已实测）；清单落库，不每次重新反查                          |
| `search_stocks` 是自然语言工具，返回不稳定       | 标的清单漂移        | 清单落 `hk_us_preheat_universe`，仅在为空或超 7 天时刷新             |
| Wind 额度/限流未知                        | 批量同步受阻        | **Gate-1 小批量试探**（见第 7 节）；批间 sleep；失败清单重跑               |
| 自选股读的是废弃集合（现存 bug）                  | 需求 2 不成立      | 同步修复 `favorites_sync_task`，改为读 `users.favorite_stocks` |
| 标的数量扩大到 1100 只，首次全量耗时长              | 可能 40–60 分钟   | 错峰（港股 17:30、美股 07:00，与 A 股 18 点错开）；幂等可中断续跑            |
| 美股后缀 `.O`/`.N` 混用                   | 代码错导致空数据      | 直接用 `search_stocks` 返回值，不自己拼后缀                         |
| 缓存陈旧被静默使用                           | 分析用旧数据        | 复用 `_check_data_freshness`（落后 >2 个工作日判陈旧）              |

---

## 7. 实施步骤（含 Gate 检查点）

| #    | 步骤                                                        | 产出                             | 验证方式                                      |
| ---- | --------------------------------------------------------- | ------------------------------ | ----------------------------------------- |
| 1    | 新增配置项（`config.py`）+ 4 个集合索引初始化脚本                          | 配置 + index                     | 连库确认索引建立                                  |
| 2    | **修复自选股断链 bug**：`favorites_sync_task` 改读 `users.favorite_stocks` | 改动 + 新增港美股读取函数                 | 插入测试自选股，确认能被读出                            |
| 3    | 实现 Universe 构建：指数分批反查 + 自选股并集 → `hk_us_preheat_universe` | 新服务 + 落库清单                     | 查库确认各来源条数（预期 ~1100）                       |
| 4    | 实现日线同步（主备双源 + upsert + 失败清单）                              | `hk_us_preheat_service.py`      | 见 Gate-1                                  |
| —    | **🚦 Gate-1 检查点**                                         | **跑 5 只标的**（0700.HK / AAPL 等） | **向你汇报：试了几只、耗时、成功率、数据量、Wind/Yahoo 表现，等你确认是否放量** |
| 5    | Gate-1 通过后：~1100 只全量跑一次日线                                 | 缓存数据                           | 统计集合条数、耗时、失败率                             |
| 6    | 基础信息同步 → `stock_basic_info_hk/us`                         | 同上                             | 调数据检验接口，确认基础信息项通过                         |
| 7    | 财务同步 → `stock_financial_data_hk/us`（需求 3）                 | 同上                             | 数据检验财务项通过                                 |
| 8    | 改 `stocks.py:966` 分支加缓存查询                                 | K 线命中缓存                        | 打 K 线接口，确认 `source=mongodb_preheat`        |
| 9    | 注册定时任务 + 开关                                               | `main.py` 任务                   | 手动触发一次，看 `scheduler_executions`           |
| 10   | 自选股跟随验证：新增一只港股自选 → 手动触发同步 → 确认入库                          | 端到端验收                          | 查 `hk_us_preheat_universe` 与日线集合           |

每步单独提交，便于回滚。

---

## 8. 待你最终确认的 2 项

> ✅ 两项均已确认：**主源 = Yahoo 主 + Wind 备**；**范围 = 首次全量（~1100 只）**。

### ⚠️ 第 1 项：预热主源选 Wind 还是 Yahoo？

| 选项                | 说明                             | 适用情况             |
| ----------------- | ------------------------------ | ---------------- |
| **Yahoo 主 + Wind 备**（推荐） | 免费无限、无限流风险；数据与 Wind 实测一致       | 重视 1100 只量级的可完成性 |
| Wind 主 + Yahoo 备  | 数据质量最高，含专业级复权/板块/财务            | 重视数据质量，愿承担额度风险   |
| 只用 Wind           | 单一依赖                           | 不推荐（额度未知）        |

> 无论选哪个，**另一个都会作为自动降级备源**，不会单点失效。

### ⚠️ 第 2 项：标的范围确认 ~1100 只

| 市场 | 清单                     | 约标的数 |
| -- | ---------------------- | ---- |
| 港股 | 恒生指数 + 恒生科技 + **港股通**  | ~550 |
| 美股 | 纳斯达克100 + **标普500**    | ~550 |

首次全量（365 天）预计 **40–60 分钟**，增量日常只需几分钟。

**可选收窄**：如果嫌港股通过大，可先只做"恒生指数 + 恒生科技 + 纳指100 + 标普500"（约 720 只），港股通二期再加。

---

## 9. 你提出的第 5 条：Yahoo 怎么加入 —— 答案

**结论：Yahoo 不需要"加入"，它已经集成好了，而且现在是港美股的第一主力源。**

**现状**（实测）：

```
_get_source_priority('HK') → ['yfinance', 'akshare']
_get_source_priority('US') → ['yfinance', 'alpha_vantage', 'finnhub']
```

也就是说，你现在打开港股/美股页面看到的**每一条数据，绝大多数已经是 Yahoo 给的**。

**怎么控制它**（全程 Web，无需改码）：

「**市场分类管理**」→ 港股/美股分组 → 调整 `Yahoo Finance` 的优先级数字（数字越大越优先）+ 启用开关。

**当前存在的问题**：

1. 港股分组里 `AKShare` 被禁用、`Yahoo Finance(1)` 那条是**带空格的脏数据**（匹配不上 handler），所以 **港股实际只有 Yahoo 一个源，没有兜底**。建议启用 AKShare 或加 Wind（需二期给 handler 加 wind 分支）。
2. 配置里有 3 条脏数据：`Alpha Vantage`、`Yahoo Finance`（带空格）永远不会生效，建议清理。
3. **Yahoo 依赖本机代理**（`127.0.0.1:7897`）。代理一关，港股立刻没数据，且没有兜底。

---

## 10. 首次全量执行（v3 新增）：3 处批量优化与实测结果

### 10.1 为什么要优化

原实现全部是「逐只调用」：日线 1 次 HTTP/只、基础信息 1.3s/只、财务 1.8s/只。
按 1190 只外推，日线+基础+财务合计 **1.5–2 小时**，是 Gate-1 汇报时预估的最大风险点。
三处实测发现可批量化：

| 环节       | 原方式                        | 优化后                                          | 实测对比                       |
| -------- | -------------------------- | -------------------------------------------- | -------------------------- |
| 日线       | `yf.Ticker(c).history()` 逐只 | `yf.download([...])` 一次多只（默认 60 只/批）         | 1190 只 **83–112s**（原外推 ~50min） |
| 基础信息     | `get_stock_basicinfo` 逐只   | 同工具**一次传多个代码**（默认 40 只/批）                    | 20 只 2.5s（逐只 1.3s/只）       |
| 财务       | `get_stock_fundamentals` 逐只 | 同上                                           | 15 只 2.8s（逐只 1.8s/只）       |

新增配置：`HK_US_PREHEAT_BATCH_SIZE`（日线批大小，默认 60）、
`HK_US_PREHEAT_WIND_BATCH_SIZE`（Wind 批大小，默认 40）、`HK_US_PREHEAT_USE_BATCH`（总开关）。

**日线批量化不影响幂等语义**：批量下载后仍逐只按「> 库内最新 `trade_date`」过滤再 upsert，
所以「无新增则 skip」的行为与逐只模式完全一致；主源非 yahoo 或关闭开关时自动回退逐只。

**`--force-full` 重建模式**：忽略增量窗口、按完整 lookback 重取全历史，并从本批第一根 K 线
重置 `pre_close/change/pct_chg` 起算点（`_upsert_daily(from_scratch=True)`）。
用于修正「首次只跑了短窗口」（如 Gate-1 的 30/90 天）导致的深度不足 —— 实测
`00001`(长和) 27 → 251 根、`MSFT` 27 → 257 根。仍是幂等 upsert，不产生重复。

**薄覆盖自动换源**：某只标的 Yahoo 返回行数 < `_MIN_YAHOO_ROWS`(5) 时，按完整 lookback
试取 Wind，仅在「Wind 行数 > Yahoo 行数 **且** > 库内现有行数」时切换，保证已存历史永不缩短。
切换走 `_write_single_source()`：先删除该 symbol 其它来源的旧行再写 —— 因为唯一键含
`data_source`，若同一标的混存 Yahoo/Wind，K 线接口会返回重复交易日。
实测修好 `AVB`(Yahoo 仅 1 根 → Wind 240 根) 与 5 只港股次新股。

### 10.2 Universe 实测（`refresh_universe --force`）

| 清单           | 返回条数 | 说明                          |
| ------------ | ---- | --------------------------- |
| 恒生指数         | 95   | 单次查询                        |
| 恒生科技         | 30   | 单次查询                        |
| **港股通**      | **667** | 数字前缀递归分批（0→00→000，实测 111 次） |
| 纳斯达克100      | 101  | A–Z 分批                      |
| **标普500**    | **502** | A–Z 分批                      |
| 自选股          | 0    | 当前无港美股自选                    |
| **去重后合计**    | **1190** | HK 673 + US 517             |

耗时约 2 分 15 秒（Wind 约 170 次调用）。

### 10.3 首次全量结果

| 阶段            | 成功        | 失败 | 写入        | 耗时          |
| ------------- | --------- | -- | --------- | ----------- |
| 日线（首批）        | 1181/1190 | 0  | 284,680 行 | 1m52s       |
| 日线（`--force-full` 重建） | 1190/1190 | 0  | 286,963 行 | 1m23s       |
| 基础信息          | 1190/1190 | 0  | 1190 条    | ≈1m40s      |
| 财务            | 1181/1190 | 9  | 1181 条    | ≈1m45s      |
| **合计（含全部重跑与校验）** | —         | —  | —         | **≈25 分钟**（含反复验证） |

- 日线 `data_source`：yahoo **1188** 只、wind **2** 只（Yahoo 无数据的自动降级生效）
  另加薄覆盖换源 6 只（`AVB` + 5 只港股次新股）
- **幂等复跑**：`{'ok': 0, 'skipped': 1190, 'rows': 0, 'failed': 0}`，零写入
- **无重复键、无多来源混写**（同 symbol+日期 唯一，唯一键含 data_source）

### 10.3.1 集合终态

| 集合                        | 条数      | 覆盖           |
| ------------------------- | ------- | ------------ |
| `hk_us_preheat_universe`  | 1,190   | HK 673 / US 517 |
| `stock_daily_quotes_hk`   | 155,675 | 673 只        |
| `stock_daily_quotes_us`   | 132,255 | 517 只        |
| `stock_basic_info_hk`     | 673     | 673/673      |
| `stock_basic_info_us`     | 517     | 517/517      |
| `stock_financial_data_hk` | 673     | 673/673      |
| `stock_financial_data_us` | 508     | 508/517（9 只见 10.4） |

> 日线历史深度：HK 平均 230 根、US 平均 255 根（回看 365 自然日）。
> 仅 6 只 HK + 1 只 US 少于 30 根，均为 2026 年下半年新上市次新股（如安克创新、
> 广和通、江波龙、云迹），属客观事实而非漏拉。

### 10.4 ⚠️ 踩坑：Wind 代码解析器会「串号」到退市实体（重要）

标普500 里有若干**代码被复用**的标的，Wind 自然语言解析器会把它们匹配回**已退市的旧实体**：

| 请求代码     | Wind 返回     | 返回名称                    | 后果            |
| -------- | ----------- | ----------------------- | ------------- |
| `AMZN.O` | `AMAZ.OO`   | AMAZING ENE OIL & GAS(退市) | 亚马逊基础信息被写成一家油气公司 |
| `COR.N`  | `COR!.N`    | CORESITE房产(退市)           | Cencora 被写成 Coresite |
| `DD.N`   | `DD!.N`     | DU PONT (EI)(退市)         | DuPont 被写成 1915 年的老杜邦 |
| `DOW.N`  | `DOW!.N`    | 陶氏化学(退市)                | Dow Inc 被写成陶氏化学 |
| `FOX.O`  | `TFCF.O`    | 21世纪福克斯-B(退市)            | Fox Corp 被写成 21 世纪福克斯 |
| `FOXA.O` | `TFCFA.O`   | 21世纪福克斯-A(退市)            | 同上            |

`!` 是 Wind 的**退市标记**。若不校验，会把另一家公司的财务/基础信息静默写进当前 symbol。

**对策**：新增 `windcode_matches()` 校验（代码大小写/后缀/港股前导零归一化后比对，
并直接拒收含 `!` 的退市代码），**所有** Wind 返回（批量与逐只、基础信息与财务）都必须过校验：

- 命中不匹配 → 该条 Wind 数据**弃用**（宁缺勿错），且**不留历史错配数据**
- 基础信息：自动回退 **Yahoo** 兜底（`longName` / `firstTradeDateMilliseconds` / `industry` / `exchange`），
  AMZN / COR / DD / DOW / FOX / FOXA / IR / Q / WTW 等因此拿到了**正确**的公司名与上市日期
- 财务：本期无第二来源 → 计入**失败清单**，由数据检验如实反映缺失，绝不用错数据冒充

实际触发弃用的 10 只：`AMZN`(←AMAZ.OO)、`GOOG`(←GOOGL.O)、`COR`(←COR!.N)、
`DD`(←DD!.N)、`DOW`(←DOW!.N)、`FOX`(←TFCF.O)、`FOXA`(←TFCFA.O)、
`IR`(←TT.N)、`Q`(←IQV.N)、`WTW`(←WW.O)。

> **为什么不建「同公司别名表」放行 GOOG←GOOGL / WTW←WW？**
> 实测 `WTW` 取回的 `WW.O` 行数据为营收 3 亿 / 净利 −38 亿，与 Willis Towers Watson
> （营收 ~$99 亿、净利 ~$10 亿）明显不符 —— 说明「返回了别的代码」本身就意味着
> 解析器不确定，放行会把脏数据引入。故**统一弃用**，不做例外。

### 10.5 已知数据质量注意点

| 项                       | 说明                                                                 |
| ----------------------- | ------------------------------------------------------------------ |
| 财务单位                    | HK 为**亿港元/亿元**、US 为**亿美元**（Wind 按标的记账币种返回），统一 ×1e8 存入 |
| 财务期间语义                  | 「最新报告期」对 HK 多为半年报、对 US 多为季报累计；已把 Wind 的 `日期` 列存入 `report_period_label`（如 `Q3 FY2026`）便于溯源 |
| US 财务个别口径可疑             | 抽样发现 `GOOGL` 净利率高达 76%（营收 2297 亿 / 净利 1748 亿），与真实 ~30% 不符，属 Wind 上游口径问题；HK 抽样（腾讯净利率 28%、汇丰、建行）均正常 |
| US 银行营收缺失               | `JPM`/`BAC` 等银行 `最新营业收入` 上游返回空（净利/ROE 正常），非本项目问题                   |
| 覆盖度                     | 日线/基础信息 1190/1190；财务 1181/1190（缺 9 只，见 10.4，属有意取舍）                 |

## 11. 交叉 Review 修正记录（v4）

来源：`reports/2026-10-11-cross-review-hkus-preheat.md`。P0–P3 全部处理，逐项列明改动与验证方式。

### 11.1 P0 工作区设计文档被旧版覆盖 —— 已恢复

工作区版本是实施前的 v2（474 行），把已推送的 v3（593 行，含 10.x 全量实测）覆盖掉了。
已 `git checkout` 恢复推送版本，本文件即恢复后的 v3（并追加本节）。

### 11.2 P1 跨数据源切换产生同一交易日重复记录 —— 已修

**问题**：日线唯一键含 `data_source`，而 K 线接口只按 `{symbol, period}` 过滤。
路径 A（`HK_US_PREHEAT_PRIMARY_SOURCE=wind` 或 `USE_BATCH=False`）的 `_upsert_daily`
只做 upsert、不清异源旧行，且写入的是整个增量窗口（含 ≤ 库内最新日期的缓冲行），
于是同一交易日会留下 Yahoo + Wind 两行 → 接口返回重复交易日。

**改法**：`_upsert_daily` 写入前调用 `_purge_conflicting_dates()`，按**本次写入的
`trade_date` 精确删除其它来源的旧行**。

> 为什么不直接「删除该 symbol 全部异源行」（review 建议的原方案）：
> 路径 A 的增量窗口可能只有 6 天，若直接删光异源行再写 4 根，会把该标的
> 250 根历史砍成 4 根。按日期精确清理既能消除重复，又保证历史永不缩短。

**验证**：构造 `yahoo` 3 行（含 1 个本次不覆盖的旧日期）+ 以 `wind` 写同窗口 3 行，
结果为 4 行、**0 个重复交易日**、未被覆盖的历史行保留。

### 11.3 P2 美股自选股 windcode 缺交易所后缀 —— 已修

**问题**：`_merge_favorites` 美股分支直接 `windcode = raw_code`（裸 ticker），
而 Wind 接口要求带后缀 → 财务永远失败、基础信息每次白打一次无效调用、Wind 备源失效。

**改法**：新增 `_resolve_us_windcodes()`（`refresh_universe` 收尾调用），
按 `WIND_BATCH_SIZE` 反查 Wind 代码并**过 `windcode_matches` 校验**；
批量未覆盖的逐个兜底重试。补全成功写回 `windcode/yahoocode/full_symbol`；
Wind 无法确认的（如会被解析成伦敦 `BRK.L` 的 `BRK.B`）打标 `yahoo_only=True`，
`_load_targets` 透出该标记，日线/基础信息/财务三条取数链路全部跳过 Wind。

**验证**：`TSM`→`TSM.N`、`MSFT`→`MSFT.O`、`BRK.B`→`BRK_B.N` 均补全成功；
现网 517 只美股中已无缺后缀标的。

### 11.4 P2 点号类股票代码（BRK.B）—— 已修

**实测更正**（与 review 描述略有出入）：Wind 用**下划线**表示股份类别
（`BRK_B.N`，不是 `BRK.B.N`），且 `BRK`/`AAPL` 等裸 ticker 在**基础信息**接口可正常解析。
真正的坑是 `to_internal_symbol("BRK.B.N")` → 归并出 `BRK`（撞 A 类）、
`to_yahoo_ticker` 给出 `BRK`（Yahoo 的 `BRK` 是 A 类），**会把 A 类行情写进 B 类 symbol**。

**改法**（四处 + 一次数据迁移）：

| 位置 | 改动 |
| --- | --- |
| 预热服务 `to_internal_symbol` | 美股内部 symbol 统一为点号形式（`BRK_B.N` → `BRK.B`），只剥离已知交易所后缀 |
| 预热服务 `to_yahoo_ticker` | 点号 → 连字符（`BRK_B.N` → `BRK-B`；`CBOE.BAT` → `CBOE`） |
| 预热服务 `windcode_matches` | 改为**带股份类别**比对（`BRK.B` ≠ `BRK`，也 ≠ 仅共享前缀的 `BRK.L`） |
| `stocks.py:_detect_market_and_code` | 放行 `^[A-Z]{1,5}[.\-][A-Z]$`，`BRK-B` 归一为 `BRK.B`；放在「默认 A 股」之前 |
| `stock_utils.identify_stock_market` | 同样放行点号/连字符形式 |
| 数据迁移 | 已把现网 `BRK_B`/`BF_B` → `BRK.B`/`BF.B`（universe + 日线 + 基础信息 + 财务，各 267 行日线） |

**踩到并已修的连带问题**：
- 交易所后缀白名单漏了 `BAT`（Cboe BZX，`CBOE.BAT`）→ 会把 `yahoocode` 写成 `CBOE-BAT`。
  已按现网 517 只美股实测分布（`O` 172 / `N` 344 / `BAT` 1）补全白名单；
  **刻意不收录单字母 `A/B/C`**——那正是股份类别写法。
- 后缀白名单若只按「含点号」判断待补全，会漏掉 `BRK.B` 这类既有类股票点号、又没交易所后缀的输入。
  已改为按「末段不是已知交易所后缀」判断。

**验证**：`BRK.B`/`BRK-B` → US `BRK.B`；`windcode_matches("BRK_B.N","BRK.L")=False`；
Yahoo 侧 `BRK-B` 最新收盘 515.62 与库内 Wind 值一致。

### 11.5 P3 五项

| 项 | 改动 |
| --- | --- |
| P3-1 批量日线内存驻留 | `_yahoo_batch`（返回整市场 map）拆为 `_yahoo_download_chunk`（单批）；`_sync_daily_batch` 改为**下载一批 → 立即写库 → 释放**，峰值内存只与 `BATCH_SIZE` 相关 |
| P3-2 薄覆盖判定口径 | 原按「Yahoo 原始行数」触发，已是最新的标的每次仍会白打一次完整 lookback 的 Wind。改为仅在**库内同样覆盖不足**（`existing < 5`）或 `force_full` 时才试 Wind |
| P3-3 前端误判静默化 | 新增 `looksLikeTruncatedCNCode()`：`^[15]\d{4}$` 不再被静默当成港股（`51005` → 提示疑似缺位而非切港股）；`SingleAnalysis.vue` 的市场切换由「A股/ETF → 港美股」改为**双向联动**（港股/美股下输 `600519`/`AAPL` 同样自动切换） |
| P3-4 universe 无移除路径 | 新增 `_prune_source()`：指数重查后摘掉已剔除成分的标签，无其它来源则 `enabled=False`；自选股删除同样摘标签。`stats[source_tag]` 统一为「写入后 enabled 计数」（原先「新鲜跳过」与「刚刷新」口径不一致） |
| P3-5 LocalAdapter 索引 | `create_database_indexes` 补 `market_quotes.{data_source:1}` 索引，避免 `find({"data_source":"local"})` 集合扫描 |

### 11.6 回归验证

| 验证项 | 结果 |
| --- | --- |
| 代码转换/守卫单测 | `to_internal_symbol` / `to_yahoo_ticker` / `windcode_matches` 共 27 个用例全通过 |
| P1 重复交易日 | 构造用例：0 个重复交易日、未覆盖历史保留 |
| universe 清理与后缀补全 | `_prune_source` 摘标签+按需禁用 2/2 正确；`TSM`/`MSFT`/`BRK.B` 补全 3/3 正确 |
| 全量增量日线 | `{'total':1190,'ok':0,'skipped':1190,'rows':0,'failed':0}`，耗时 90.4s（与改前同量级） |
| 基础信息 / 财务增量 | 见执行日志（幂等，无回归） |
| 前端 | `eslint src/utils/stockValidator.ts src/views/Analysis/SingleAnalysis.vue` 零告警 |

## 附：本次勘察的代码索引

| 位置                                                | 内容                                       |
| ------------------------------------------------- | ---------------------------------------- |
| `app/routers/stocks.py:356-388`                   | `_detect_market_and_code` 市场识别与代码标准化     |
| `app/routers/stocks.py:963-985`                   | K 线接口港美股分流点                              |
| `app/services/foreign_stock_service.py:213/433`   | 港股/美股 **有效 handler 只有 yahoo/akshare 等，无 wind** |
| `app/services/foreign_stock_service.py:269`       | `_get_source_priority` 读 `datasource_groupings` |
| `app/services/favorites_service.py:378`           | 自选股实际写入点 `users.favorite_stocks`         |
| `app/worker/favorites_sync_task.py:38-58`         | 🐛 读已废弃的 `user_favorites`，断链 bug          |
| `app/services/data_validation_service.py:226-231` | 按市场映射的集合名（基础信息/日线/财务/实时）                 |
| `app/services/data_sources/wind_adapter.py:93-104` | `_to_windcode` 转换规则                      |
| `app/main.py:647` / `:631`                        | 定时任务注册范式 / 暂停范式                          |
