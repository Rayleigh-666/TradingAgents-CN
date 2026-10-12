# 第六轮交叉 Review：分析预检按市场分流（7cb0ac6f）

- 日期：2026-10-12
- 对象：`7cb0ac6f fix(analysis): 预检按市场查基础信息集合，修复港股/美股「数据未同步」误拦`
- 范围：4 文件，+255/-12（`app/routers/analysis.py`、`frontend/src/api/analysis.ts`、`frontend/src/views/Analysis/SingleAnalysis.vue`、`tests/test_analysis_precheck_market.py` 新增）
- 背景：上一轮 P3 闭环后，用户实机发现港股（03669 永达汽车，已同步成功）点「开始分析」仍被「基础数据未同步」拦截——与已修的各处同源：港美股被当成 A 股查。本提交修 `/api/analysis/precheck`。

## 结论

**修复正确、完整、无新引入问题，无 P0/P1/P2。可以提交（已提交）。**

## 逐点核验

### 1. 后端逻辑（analysis.py::precheck_analysis）

| 检查点 | 结果 |
|---|---|
| 市场归一化入口 | ✅ `resolve_market(market_type)`，复用 `market_codes` 单一事实来源，未在业务侧另造口径 |
| 集合选择 | ✅ `basic_info_collection(market)`：cn→`stock_basic_info` / hk→`_hk` / us→`_us`；第 2 步 `basics_count` 与第 3 步目标查询同集合，口径一致 |
| 代码归一化 | ✅ `normalize_symbol(symbol, market)` 后再查；脏值 `003669→03669`、`0700.HK→00700`、`aapl→AAPL`、`BRK-B.N→BRK.B` 均正确 |
| 不跨市场回退 | ✅ 归一化为空（无效长码）时 `target_exists=False` 直接判 `stock_not_found`，**不发查询**；查询只发往 `basics_coll`，绝无 A 股集合回退 |
| 参数默认值 | ✅ `market_type: str = Query("cn")` 向后兼容（现存唯一调用方已显式传参） |
| 失败响应 | ✅ 拦截时 `details` 带 `market_type`/`basics_collection`，message 展示归一化后的代码，便于排查 |
| 异常降级 | ✅ 预检本身异常 → `can_submit=True`（precheck_error），worker 兜底校验——既有设计，未动 |

### 2. 前端链路

| 检查点 | 结果 |
|---|---|
| 调用方收敛 | ✅ 全前端仅 1 处 `precheck(` 调用（SingleAnalysis.vue:1599），已传 `toApiMarketType(analysisForm.market)` |
| 导入 | ✅ `toApiMarketType` 已在 858 行导入（非新增引用） |
| 取值域闭合 | ✅ `MarketType = 'A股'\|'ETF'\|'港股'\|'美股'`；`toApiMarketType` 映射 A股/ETF→cn、港股→hk、美股→us，**永不为 undefined**——`market_type` 参数不存在静默漏传 |
| 参数名对齐 | ✅ 前端 `params.market_type` ↔ 后端 `Query("market_type")` |
| 提交前其他门槛 | ✅ `validateStockCode`（前端格式校验，按市场）、`fetchFactorPreview`（仅 A 股且 catch 吞错）均不拦港美股；后端 `/single` 建任务无基础信息门槛，worker 校验在前几轮已按市场修 |

### 3. 测试（新增 9 例）

- 假 DB 断言「实际查询的集合名 + 归一化键」，不依赖真实 MongoDB
- 覆盖：港股放行且只查 `_hk`、`003669→03669`、A 股集合有 03669 也不跨市场放行、美股小写归一化、A 股行为不变、无效长码拦截且不发查询、按市场 `basics_count`（仅 `_hk` 有数据不误判 no_basics）、无 symbol 各市场写法容错、数据源检查不受影响
- 双入口均通过：`python tests/test_analysis_precheck_market.py` → 9/9；`pytest` → 9 passed

### 4. 实跑验证

| 项 | 结果 |
|---|---|
| 新测试（直接 + pytest） | ✅ 9/9 |
| 相关套件回归 | ✅ 53 passed（preheat codes / meta throttle / akshare 占位守卫 / 单股同步 / provider / manual mode） |
| **非空转证明** | ✅ 新测试拷入修复前 commit（6cb8d458）worktree 运行 → **9/9 全部失败**（旧签名无 `market_type`，测试确实在验新代码路径） |
| **真实库端到端** | ✅ **15/15**：`3669/03669/003669/0700.HK/00700`→hk 放行；`aapl/NVDA/BRK-B.N`→us 放行；`000001/600519/1`→cn 放行；`99999/1234567`(hk)、`999999`(us)、`03669`(cn) → 各自市场 `stock_not_found`。提交信息中的真实库声明逐条属实 |
| `py_compile` | ✅ analysis.py + 新测试 OK |
| `vue-tsc --noEmit` | ✅ 0 错误（exit 0） |

## 遗留观察（均不阻断，P3）

1. ~~**`app/routers/health.py` 就绪检查仍只数 `stock_basic_info`**（87/131 行）~~ → **已修复**
   （见 `reports/2026-10-12-health-readiness-market-fix.md`）。原问题：同类问题的另一处——港股/美股
   专用部署下，readiness 接口会报 `basics_synced=false`「基础数据未同步」。因其为**全局系统就绪**
   语义（无市场上下文），已定口径为「**任一市场（cn/hk/us）有基础数据即视为已同步**」，
   `basics_count` 改为三市场合计并新增 `basics_by_market` 明细；两处分支（jdyun 特化 + 常规）
   共用 `count_basics_by_market()`，集合名走 `basic_info_collection()`。
   新增 `tests/test_health_readiness_markets.py`（9 例）。
2. 预检异常降级为放行（`can_submit=True/precheck_error`）：若 MongoDB 绑定未初始化，预检形同虚设——既有设计（worker 有兜底），生产环境启动时绑定已建立，仅备忘。
3. `details.symbol` 在未传 symbol 时为 `None`（改动前后一致，无害）。

## 环境副作用（同前两轮）

- 本地 MongoDB（27017）为实测保持运行；`.venv` 内 `pytest`/`pytest-asyncio` 为此前装入（requirements-ci.txt 已声明）。
