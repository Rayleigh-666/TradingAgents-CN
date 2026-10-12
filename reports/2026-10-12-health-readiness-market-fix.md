# 修复：就绪检查按市场统计基础数据（第六轮 Review 遗留 P3）

- 日期：2026-10-12
- 对象：`app/routers/health.py::system_readiness`
- 来源：`reports/2026-10-12-cross-review-round6.md` 遗留观察第 1 条
- 性质：不阻断，但会误导首页引导卡片

## 问题

`/api/system/readiness` 两处（原 87 / 131 行）写死

```python
basics_count = await db["stock_basic_info"].count_documents({})
```

而基础信息按市场分集合存放（cn→`stock_basic_info` / hk→`stock_basic_info_hk` /
us→`stock_basic_info_us`）。**港股/美股专用部署**（只同步了 `_hk`/`_us`、A 股集合为空）下，
readiness 返回 `basics_synced=false`、`next_step=sync_data`，首页引导卡片永远停在第 3 步
「请同步股票基础数据」——与先前各轮修的是同一类「港美股被当成 A 股查」的问题。

## 口径decision

该接口是**全局系统就绪**语义（无市场上下文，前端 `Dashboard/index.vue` 只在未就绪时
展示引导卡片），不适合加 `market_type` 参数按市场分流——那会让「系统是否就绪」失去唯一答案。
故定口径：

> **任一市场（cn/hk/us）有基础数据即视为已同步；`basics_count` 为三市场合计。**

理由：单股分析前的**精确**门槛由 `/api/analysis/precheck` 负责（已按 `market_type` 分流，
上一提交 `7cb0ac6f` 修好）；readiness 只承担「这个部署是否已经导过数」的粗粒度判断，
任一市场有数据即说明数据链路已打通。

## 改动

| 文件 | 改动 |
|---|---|
| `app/routers/health.py` | 新增模块级 `_READINESS_MARKETS = ("cn","hk","us")` 与 `count_basics_by_market(db)`：逐市场统计，集合名走 `basic_info_collection()`（不硬编码）；单市场统计异常按 0 计并 `logger.warning`，不影响整体就绪检查。jdyun 分支与常规分支均改用它，`basics_count = sum(...)`，`basics_synced = basics_count > 0`；响应新增 `basics_by_market` 明细（初始结构同步补齐） |
| `tests/test_health_readiness_markets.py` | 新增 9 例 |

响应字段兼容性：`basics_count` 语义由「A 股条数」变为「三市场合计」，`basics_synced`
由「A 股 > 0」变为「合计 > 0」。唯一消费方 `Dashboard/index.vue` **只读 `basics_synced`**
（不读 `basics_count`），故无前端影响；`basics_by_market` 为新增字段。

## 验证

| 项 | 结果 |
|---|---|
| 新增 `tests/test_health_readiness_markets.py`（直接入口） | ✅ 9/9 |
| 回归：`test_analysis_precheck_market` / `test_hk_us_preheat_codes` / `test_hk_us_preheat_meta_throttle` / `test_akshare_batch_no_placeholder` | ✅ 9/9、24/24、10/10、4/4 |
| `py_compile` | ✅ health.py + 新测试 OK |
| **非空转证明** | ✅ 新测试拷入修复前 commit（`7cb0ac6f` worktree）→ 导入即 `ImportError`；同一行为探针在旧代码上返回 `basics_synced=False / next_step=sync_data / basics_count=0`，在新代码上返回 `True / ready / 674` |
| **真实库核对** | ✅ `basic_info_collection` 三集合名正确；实测 `{cn:11147, hk:674, us:517}`，合计 12338；模拟「仅 hk/us 有数据」→ 判定 synced |

覆盖用例：仅 `_hk` 有数据即 synced、仅 `_us` 同理、三市场皆空仍拦（`next_step=sync_data`）、
`basics_count` 为合计、集合名来自 `basic_info_collection`、单市场统计异常被隔离、
jdyun 分支同样按市场（含为空时仍拦）、数据源/LLM 前两步优先级不变。

## 生效

`app/routers/health.py` 属后端代码，uvicorn 未开 `--reload`，**需重启后端**才生效。
（前端 `Dashboard` 直接读接口返回，无需改动。）
