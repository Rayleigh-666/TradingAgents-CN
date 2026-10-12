# 第五轮交叉 Review · P3 问题修复记录

- 日期：2026-10-12 21:10
- 上游：[`reports/2026-10-12-cross-review-round5.md`](./2026-10-12-cross-review-round5.md)
- 范围：仅处理该报告「P3 观察」表中的可行动项；P3-3 / P3-4 属记录备查，本次未改（见文末）

## 逐条处置

| # | 原问题 | 处置 | 落点 |
|---|---|---|---|
| 1 | `internal_to_windcode` 对 >5 位数字静默取末 4 位（`1234567` → `4567.HK`），会把错误长码注册成**另一只股票**的 universe 记录 | ✅ 已修 | `app/core/market_codes.py` |
| 2 | `_resolve_stock_name`（×2）/ `_get_cached_security_name` 先 `zfill(6)` 再只查 A 股 `stock_basic_info`，港股 5 位码必失配，退回「股票03669」 | ✅ 已修 | `simple_analysis_service.py` / `unified_analysis_service.py` / `task_analysis_service.py` |
| 3 | `stocks.py::_detect_market_and_code` 6 位纯数字（含旧脏值）落 A 股分支 | ⏸ 记录备查（无合法 A 股 `000xxx` 与脏值的可区分特征） | — |
| 4 | universe 的 `ondemand` 来源只增不删 | ⏸ 记录备查（池＝指数∪自选∪按需，设计已文档化） | — |
| 5 | Favorites 单股同步弹窗默认只勾 realtime，标的若从未同步日线则回填必失败 | ✅ 已修 | `frontend/src/views/Favorites/index.vue` + `app/routers/stock_sync.py` |

## P3-1：港股无效长码在源头拒绝

原实现 `f"{digits.zfill(5)[-4:]}.HK"` 只按位数截断，长度异常时把「错误长码」成功注册成另一只股票。
现改为**先归一化、再取末 4 位**，并在 `normalize_hk_symbol` 加 guard：

```python
significant = digits.lstrip("0") or "0"
if len(significant) > 5:      # 港股有效位数最多 5 位
    return ""
```

- 关键点：**判据是「去前导零后的有效位数」而非「输入位数」**——否则会把需要修复的历史脏值
  `003669`（6 位输入、4 位有效）一并拒掉，与既有回归用例冲突。
- 效果：`123456` / `1234567` / `999999` → `""`（无法识别）；`003669` → 仍修回 `03669`。

## P3-2：名称解析按码形分流

三处名称解析拿不到 `market` 参数，故新增 `guess_market(code)` 按码形推断市场，并用
`basic_info_collection(market)` 取集合名（避免各处硬编码），查询键改用 `normalize_symbol(raw, market)`：

| 输入 | 市场 | 集合 | 查询键 |
|---|---|---|---|
| `03669` / `700` / `0700.HK` | hk | `stock_basic_info_hk` | `03669` / `00700` |
| `000001` / `600519.SH` | cn | `stock_basic_info` | `000001` / `600519` |
| `AAPL` / `AAPL.O` / `BRK.B` | us | `stock_basic_info_us` | `AAPL` / `BRK.B` |

- 规则：显式后缀（`.HK`→hk、`.SH/.SZ/.SS/.BJ`→cn）优先于形态；含字母→us；纯数字 ≤5 位→hk、≥6 位→cn。
- 6 位纯数字（含脏值 `003669`）仍按 A 股处理，与 `stocks.py::_detect_market_and_code` 口径一致（即 P3-3）。
- ETF 分支仅在 cn 下生效。

## P3-5：实时行情回填空转

- 前端 `singleSyncForm.syncTypes` 默认改为 `['realtime', 'historical']`（实时回填依赖当日日线，
  从未同步过历史的标的只勾 realtime 会必然失败）。
- 后端失败文案改为「无日线数据可回填（请先勾选「历史行情数据」同步日线）」，给出可执行指引。

## 验证

| 项 | 结果 |
|---|---|
| `py_compile` 6 个改动 Python 文件 | OK |
| `tests/test_hk_us_preheat_codes.py` | **24/24 通过**（新增 `test_guess_market_and_collection_routing`；`test_normalize_symbol_by_market` 补无效长码断言） |
| 边界样例（真实调用） | `123456/1234567/999999 → ""`；`003669 → 03669`；`01369`… 见脚本输出 |
| 端到端（真实 MongoDB） | `_get_cached_security_name("03669")` → **永达汽车**（旧逻辑 → `股票003669`）；`00700→腾讯控股`、`NVDA→英伟达(NVIDIA)`、`BRK.B→伯克希尔(BERKSHIRE)-B`、`000001→平安银行`、`600519→贵州茅台` |
| `vue-tsc --noEmit` | **0 错误**（全量，exit 0） |

## 追加处置：占位脏行清理 + 源头阻断（同日 21:20）

原报告遗留项「本轮新发现的残留」已按用户要求处理完毕（删除 + 不再产生）。

### 1. 删除残留行

- 目标：`stock_basic_info` 中 `_id=6accc17284a48cf3cc3fd4e6`（`code/symbol=003669`、`name=股票003669`）
- 安全断言：仅当 `name == "股票" + code` 成立才删（真股票名不可能等于该形态），脚本拒绝其他情况
- 备份：`.runtime/backup/stock_basic_info_placeholder_003669_20261012_211559.json`
- 结果：`deleted_count=1`，`stock_basic_info` 11147 → **11146**；全库再扫 `name == "股票<code>"` 为 **0** 条
  （`stock_screening_view` 是视图，随基础文档一并消失）

### 2. 源头阻断：两处写库守卫

占位行的真正成因不在 `_build_basic_info_from_list`，而在**写库前没有校验数据是否有实际内容**：

- **单股同步**（`app/routers/stock_sync.py`，实际产生本行脏数据的路径）
  旧逻辑把 provider 的**降级结果**（`sync_status="degraded"`，name 恒为占位 `股票<code>`）
  照常 `upsert` 进库，**之后**才把结果标成失败——即「报失败却写了脏数据」。
  现在改为：`degraded` 或 name 仍为占位 → **跳过写库**，返回
  `{success: False, degraded, skipped_write: True, error, detail_symbol}`。
  对外仍为失败，与旧行为一致，只是不再落库。
- **批量同步**（`app/worker/akshare_sync_service.py`）
  ① `_build_basic_info_from_list` 不再伪造占位名（`name or f"股票{code6}"` → 留空）；
  ② `_process_basic_info_batch` 增守卫：name 为空/占位或 `sync_status="degraded"` → 计
  `error_count` 并写 `context="placeholder_guard"`，**不写库**。

### 3. 验证

| 项 | 结果 |
|---|---|
| `tests/test_akshare_batch_no_placeholder.py`（新增 4 例） | 4/4 —— 缺名不写、provider 占位名不写、真实数据照写、builder 不伪造名 |
| `tests/test_stock_sync_single_akshare_basic.py`（重写 3 例） | 3/3 —— 成功路径照写且行业映射优先；`degraded` 不写；无 degraded 标记但占位名也不写 |
| `tests/test_akshare_provider_basic_info.py` | 2/2（未改 provider 契约） |
| `tests/test_hk_us_preheat_codes.py` / `test_hk_us_preheat_meta_throttle.py` | 24/24、10/10 |
| `py_compile` | OK |
| 真实 MongoDB 复查 | `003669` 查无此档；全库占位行 0 条 |

> ⚠️ 顺带修好一条**既有失效用例**：`test_stock_sync_single_akshare_basic.py` 仍按已移除的
> `sync_single_stock(request, BackgroundTasks(), current_user=...)` 签名调用，抛
> `TypeError: got multiple values for argument 'current_user'`。该端点现为「提交后台任务」的薄封装，
> 同步逻辑在 `run_single_stock_sync`，用例已改为直接调用后者。

## 原：本轮新发现的残留（已在上节处理）

`stock_basic_info` 存在一条 **akshare 占位脏行**：

```
{_id: 6accc17284a48cf3cc3fd4e6, code/symbol: "003669", name: "股票003669",
 source: "akshare", sync_status: "degraded", full_symbol: "003669.SZ",
 last_sync: 2026-10-12 11:17:49}
```

成因：`app/worker/akshare_sync_service.py:194` 在数据源无返回时仍会写入，
名称回落为 `f"股票{code}"`、`sync_status="degraded"`。即「旧脏值 `003669` 走 A 股同步链路」的产物，
也是 P3-3「表现为优雅失败而非错数据」的一个反例：**它会落库并让不存在的 A 股代码查得到"基础信息"**。

建议二选一（本次未执行，避免在用户库上做未授权的删除）：
1. 删掉该行（`stock_screening_view` 为视图，会自动跟随）；
2. 让 `akshare_sync_service` 在「无名称且非 degraded 兜底」时**不写库**，从源头不再产生占位行。
