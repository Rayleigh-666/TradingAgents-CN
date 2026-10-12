#!/usr/bin/env python3
"""
港美股预热：代码转换 / 历史不变式 / 指数拉取完整性 / 市场识别一致性 —— 单元测试

覆盖最易回归的四处：

A. 代码转换（`_us_ticker` / `to_internal_symbol` / `to_yahoo_ticker` / `windcode_matches`）
   —— 含第二轮 Review 新修的「连字符归一」（自选股存 `BRK-B` 时不致与 `BRK.B` 分裂）
B. 日线写入不变式「已存历史永不缩短」
   —— `_purge_conflicting_dates` 只清同日异源、`_write_single_source` 拒绝缩短、
      `_build_daily_ops` 前收链（顺带证伪「无效行导致 prev_close 跳格」）
B2. 指数成分拉取的完整性判定（第三轮 Review 的 P3 残留）
   —— 拉取不完整（截断 / 子查询失败）→ 只增量补写、跳过清理；
      拉取完整 → 照常清理（含 HSTECH 30→23 这类合法调仓缩水）；
      且补写必须推进 `updated_at`，否则「跳过即不写」会永久锁存
C. 市场识别各处一致性
   —— stocks.py / paper.py 的 normalized_code 必须一致；
      `US_EXCH_SUFFIXES` 四处（含分析链路 stock_utils）必须引用同一集合

可直接运行：`python tests/test_hk_us_preheat_codes.py`
"""

import asyncio
import os
import sys
from datetime import datetime, timedelta
from unittest.mock import patch

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

# —— 容忍 no-op mkdir ——
# `tradingagents.config` 在 import 时会执行 Path("config").mkdir(exist_ok=True)，
# 而 `config/` 在本仓库中已存在、本应直接 no-op；但当前执行环境的文件操作代理会把
# 这次调用拦成 PermissionError(EEXIST)，导致 app.routers.paper 无法导入。
# 这里只短路「目标已是目录 + exist_ok=True」的情形，不改变任何真实创建行为。
import pathlib  # noqa: E402

_mkdir_orig = pathlib.Path.mkdir


def _mkdir_tolerant(self, mode=0o777, parents=False, exist_ok=False):
    if exist_ok:
        try:
            if self.is_dir():
                return
        except OSError:
            pass
    return _mkdir_orig(self, mode, parents, exist_ok)


pathlib.Path.mkdir = _mkdir_tolerant

from app.worker.hk_us_preheat_service import (  # noqa: E402
    HKUSPreheatService,
    _base_ticker,
    _us_ticker,
    to_full_symbol,
    to_internal_symbol,
    to_yahoo_ticker,
    windcode_matches,
)


# ==================== 轻量假 MongoDB ====================

def _match(doc, f):
    for k, v in f.items():
        if isinstance(v, dict):
            if "$ne" in v and doc.get(k) == v["$ne"]:
                return False
            if "$in" in v and doc.get(k) not in v["$in"]:
                return False
        elif isinstance(doc.get(k), list) and not isinstance(v, (list, dict)):
            # MongoDB 语义：{"sources": "x"} 匹配「数组含 x 的文档」
            if v not in doc[k]:
                return False
        elif doc.get(k) != v:
            return False
    return True


class _BulkResult:
    def __init__(self, upserted=0, modified=0):
        self.upserted_count = upserted
        self.modified_count = modified


class _FakeColl:
    def __init__(self, docs=None):
        self.docs = [dict(d) for d in (docs or [])]

    async def count_documents(self, filter_=None, *a, **kw):
        return len([d for d in self.docs if _match(d, filter_ or {})])

    def find(self, filter_=None, *a, **kw):
        return _FakeCursor([d for d in self.docs if _match(d, filter_ or {})])

    async def delete_many(self, filter_, *a, **kw):
        before = len(self.docs)
        self.docs = [d for d in self.docs if not _match(d, filter_)]
        return type("R", (), {"deleted_count": before - len(self.docs)})()

    async def find_one(self, filter_=None, *a, **kw):
        hits = [d for d in self.docs if _match(d, filter_ or {})]
        if not hits:
            return None
        sort = kw.get("sort")
        if sort:
            key, direction = sort[0]
            hits.sort(key=lambda d: d.get(key) or "", reverse=(direction < 0))
        return hits[0]

    async def bulk_write(self, ops, ordered=False):
        upserted = modified = 0
        for op in ops:
            filt = getattr(op, "_filter", None) or {}
            doc = getattr(op, "_doc", None) or {}
            set_ = dict(doc.get("$set") or {})
            add_to_set = doc.get("$addToSet") or {}
            pull = doc.get("$pull") or {}
            existing = [d for d in self.docs if _match(d, filt)]
            if existing:
                target = existing[0]
                target.update(set_)
                self._apply_array_ops(target, add_to_set, pull)
                modified += 1
            else:
                new_doc = dict(filt)
                new_doc.update(set_)
                new_doc.update(doc.get("$setOnInsert") or {})
                self._apply_array_ops(new_doc, add_to_set, pull)
                new_doc.setdefault("_id", f"auto-{len(self.docs)}")
                self.docs.append(new_doc)
                upserted += 1
        return _BulkResult(upserted, modified)

    @staticmethod
    def _apply_array_ops(target, add_to_set, pull):
        """支持 $addToSet（去重追加）与 $pull（移除元素），_prune_source 会用到"""
        for k, v in add_to_set.items():
            arr = list(target.get(k) or [])
            if v not in arr:
                arr.append(v)
            target[k] = arr
        for k, v in pull.items():
            if isinstance(target.get(k), list):
                target[k] = [x for x in target[k] if x != v]


class _FakeCursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def __aiter__(self):
        self._it = iter(self._docs)
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration


class _FakeDB:
    def __init__(self, colls=None):
        self._colls = dict(colls or {})

    def __getitem__(self, name):
        return self._colls.setdefault(name, _FakeColl())


DB_PATCH = "app.worker.hk_us_preheat_service.get_mongo_db"


def _us_target(symbol="AAPL", windcode="AAPL.O"):
    return {"market": "US", "symbol": symbol, "windcode": windcode,
            "yahoocode": _us_ticker(windcode).replace(".", "-")}


# ==================== A. 代码转换 ====================

def test_us_ticker_normalizes_separators():
    """下划线（Wind）/ 连字符（Yahoo）统一为点号；交易所后缀被剥掉"""
    assert _us_ticker("AAPL.O") == "AAPL"
    assert _us_ticker("BRK_B.N") == "BRK.B"
    assert _us_ticker("BRK.B") == "BRK.B"
    assert _us_ticker("BRK-B") == "BRK.B"          # ← 第二轮 Review 新修
    assert _us_ticker("CBOE.BAT") == "CBOE"
    assert _us_ticker("GOOGL.OQ") == "GOOGL"
    assert _us_ticker("BF-B") == "BF.B"
    print("  ✓ _us_ticker 分隔符归一 + 剥交易所后缀")


def test_internal_and_yahoo_symbol():
    """内部 symbol 用点号；Yahoo ticker 用连字符（BRK.B → BRK-B）"""
    assert to_internal_symbol("BRK_B.N", "US") == "BRK.B"
    assert to_internal_symbol("BRK-B", "US") == "BRK.B"
    assert to_internal_symbol("AAPL.O", "US") == "AAPL"
    assert to_internal_symbol("0002.HK", "HK") == "00002"
    assert to_yahoo_ticker("BRK_B.N", "US") == "BRK-B"
    assert to_yahoo_ticker("BRK-B", "US") == "BRK-B"     # 幂等
    assert to_yahoo_ticker("CBOE.BAT", "US") == "CBOE"
    assert to_full_symbol("0002.HK", "HK") == "00002.HK"
    print("  ✓ 内部 symbol / Yahoo ticker 转换")


def test_base_ticker_and_windcode_matches():
    """基码归一后可比；Wind 串号（退市实体 / 不同 ticker）必须被拒"""
    assert _base_ticker("BRK_B.N") == _base_ticker("BRK.B") == _base_ticker("BRK-B")
    assert _base_ticker("0002.HK") == _base_ticker("00002.HK") == "2"

    assert windcode_matches("AAPL.O", "AAPL.N") is True      # 同标的换交易所
    assert windcode_matches("BRK_B.N", "BRK.L") is False     # 股份类别不同
    assert windcode_matches("COR.N", "COR!.N") is False      # 退市标记 !
    assert windcode_matches("AMZN.O", "AMAZ.OO") is False    # 串号到退市实体
    print("  ✓ _base_ticker / windcode_matches（拒串号与退市）")


# ==================== B. 日线写入不变式 ====================

def test_purge_conflicting_dates_only_same_dates():
    """只清「本次写入日期」上的异源行，未覆盖历史必须保留"""
    svc = HKUSPreheatService()
    coll = _FakeColl([
        {"symbol": "AAPL", "data_source": "yahoo", "trade_date": "2026-01-01", "period": "daily"},
        {"symbol": "AAPL", "data_source": "yahoo", "trade_date": "2026-01-02", "period": "daily"},
        {"symbol": "AAPL", "data_source": "yahoo", "trade_date": "2026-01-03", "period": "daily"},
        {"symbol": "AAPL", "data_source": "wind", "trade_date": "2026-01-02", "period": "daily"},
    ])
    asyncio.run(svc._purge_conflicting_dates(coll, "AAPL", "wind", [{"date": "2026-01-02"}]))

    assert len(coll.docs) == 3, coll.docs
    assert not any(d["data_source"] == "yahoo" and d["trade_date"] == "2026-01-02"
                   for d in coll.docs), "同日异源行应被清除"
    assert any(d["data_source"] == "yahoo" and d["trade_date"] == "2026-01-01"
               for d in coll.docs), "未覆盖历史必须保留"
    print("  ✓ _purge_conflicting_dates 按日期精确清理")


def test_write_single_source_refuses_to_shrink_history():
    """
    核心不变式：整源切换不得用少量新数据覆盖整段历史
    （第二轮 Review 的 P1：Yahoo 挂了走 Wind 兜底，250 根被砍成几根）
    """
    svc = HKUSPreheatService()
    history = [{"symbol": "AAPL", "data_source": "yahoo", "period": "daily",
                "trade_date": f"2026-01-{i:02d}", "close": 100.0 + i}
               for i in range(1, 21)]
    coll = _FakeColl(history)
    t = _us_target("AAPL")
    tiny = [{"date": "2026-02-01", "close": 200.0}]      # 只有 1 行

    n = asyncio.run(svc._write_single_source(coll, t, tiny, "wind"))
    assert n == 0, "应拒绝写入"
    assert len(coll.docs) == 20, "历史必须原样保留"
    assert all(d["data_source"] == "yahoo" for d in coll.docs), "不应清掉异源行"
    print("  ✓ _write_single_source 拒绝缩短历史（默认）")


def test_write_single_source_allows_shrink_only_when_asked():
    """force_full 重建时显式放行，才允许切换"""
    svc = HKUSPreheatService()
    history = [{"symbol": "AAPL", "data_source": "yahoo", "period": "daily",
                "trade_date": f"2026-01-{i:02d}", "close": 100.0 + i}
               for i in range(1, 21)]
    coll = _FakeColl(history)
    t = _us_target("AAPL")
    tiny = [{"date": "2026-02-01", "close": 200.0}]

    n = asyncio.run(svc._write_single_source(coll, t, tiny, "wind", allow_shrink=True))
    assert n == 1
    assert len(coll.docs) == 1
    assert coll.docs[0]["data_source"] == "wind"
    print("  ✓ _write_single_source allow_shrink=True 放行（force_full 场景）")


def test_upsert_daily_keeps_history_and_dedupes_cross_source():
    """
    Yahoo 完全失败时用 Wind 补增量的正解：走 _upsert_daily
    → 历史保留、同交易日不重复
    """
    svc = HKUSPreheatService()
    history = [{"symbol": "AAPL", "data_source": "yahoo", "period": "daily",
                "trade_date": f"2026-01-{i:02d}", "close": 100.0 + i}
               for i in range(1, 21)]
    coll = _FakeColl(history)
    t = _us_target("AAPL")
    incr = [{"date": "2026-01-20", "close": 999.0}]     # 与库内最后一根同日

    asyncio.run(svc._upsert_daily(coll, incr, t, "wind"))

    assert len(coll.docs) == 20, f"历史行数不应变化: {len(coll.docs)}"
    same_date = [d for d in coll.docs if d["trade_date"] == "2026-01-20"]
    assert len(same_date) == 1, "同一交易日只能有一行"
    assert same_date[0]["data_source"] == "wind"
    assert same_date[0]["close"] == 999.0
    print("  ✓ _upsert_daily 保留历史 + 同交易日跨源不重复")


def test_build_daily_ops_prev_close_chain_skips_invalid_rows():
    """
    证伪 Review P3-5：无效行被跳过后，其后首行的 pre_close 取的是**上一有效行**的
    close，链不会断（跳格说法不成立）
    """
    items = [
        {"date": "2026-01-01", "close": 100.0},
        {"date": "2026-01-02", "close": None},        # 无效（无收盘价）
        {"date": "2026-01-03", "close": 110.0},
    ]
    ops = HKUSPreheatService._build_daily_ops(items, _us_target("AAPL"), "yahoo",
                                              None, datetime.now())
    assert len(ops) == 2, "无效行不应产出 op"
    first, second = (op._doc["$set"] for op in ops)
    assert first["close"] == 100.0 and first["pre_close"] is None
    assert second["close"] == 110.0
    assert second["pre_close"] == 100.0, "前收应衔接上一有效行"
    assert second["change"] == 10.0
    print("  ✓ _build_daily_ops 前收链跳过无效行（P3-5 不成立）")


# ==================== B2. 指数成分拉取的完整性判定 ====================

class _FakeSettings:
    HK_US_PREHEAT_INDEXES = "恒生指数"
    HK_US_PREHEAT_UNIVERSE_REFRESH_DAYS = 7


_INDEX_TAG = "index:恒生指数"


def _run_universe_refresh(svc, db, member_count, existing_count,
                          truncated=False, failed=0, force=True, stale=False,
                          coll=None):
    """
    跑一次 refresh_universe，返回 (stats, prune 来源标签列表, 集合, 调用计数)

    `truncated` / `failed` 模拟 `_fetch_index_members` 上报的完整性事实：
    前者对应「细分到最大深度仍满页」，后者对应「N 个子查询抛异常」。
    `stale=True` 时预置的旧清单 updated_at 是 30 天前（用于验证新鲜度推进）。
    `coll` 传入可复用同一集合连续跑多轮（验证「不重复重打」）。

    注意这里**不替换 `_prune_source`**，只在外面套一层记录调用，
    清理效果因此是真实发生的（断言可以看库内状态，而非只看有没有被调用）。
    """
    if coll is None:
        age = timedelta(days=30) if stale else timedelta(0)
        coll = _FakeColl([
            {"_id": f"u{i}", "market": "HK", "symbol": f"{i:05d}",
             "sources": [_INDEX_TAG], "enabled": True,
             "updated_at": datetime.now() - age}
            for i in range(existing_count)
        ])
    db._colls["hk_us_preheat_universe"] = coll
    calls = {"prune": [], "fetch": 0}

    async def _fake_merge(c):
        return 0, []

    real_prune = svc._prune_source

    async def _spy_prune(c, tag, keep):
        calls["prune"].append(tag)
        return await real_prune(c, tag, keep)

    def _fake_members(name, market):
        calls["fetch"] += 1
        members = [{"windcode": f"{i:05d}.HK", "name": "X"}
                   for i in range(member_count)]
        return members, {"truncated": truncated, "failed": ["q"] * failed}

    with patch.object(svc, "_merge_favorites", _fake_merge), \
         patch.object(svc, "_prune_source", _spy_prune), \
         patch.object(svc, "_fetch_index_members", _fake_members), \
         patch(DB_PATCH, lambda: db), \
         patch("app.worker.hk_us_preheat_service.settings", _FakeSettings):
        stats = asyncio.run(svc.refresh_universe(force=force))
    # 注意：_prune_source 还会被自选股分支调用一次（tag="favorite"），
    # 故断言只看指数来源那一项
    return stats, calls["prune"], coll, calls


def _tagged_enabled(coll):
    """库内仍带指数来源标签且 enabled 的标的数"""
    return len([d for d in coll.docs
                if _INDEX_TAG in (d.get("sources") or []) and d.get("enabled")])


def test_incomplete_fetch_skips_prune():
    """
    上游拉取不完整（细分截断）时必须跳过清理：
    否则缺失桶里的成分会被误摘标签 / 误禁用，少预热一批
    """
    svc = HKUSPreheatService()
    db = _FakeDB({})
    stats, prune_tags, coll, _ = _run_universe_refresh(
        svc, db, member_count=5, existing_count=100, truncated=True)
    assert _INDEX_TAG not in prune_tags, f"拉取不完整时应跳过清理，实际 {prune_tags}"
    assert stats[_INDEX_TAG] == 100, stats
    assert _tagged_enabled(coll) == 100, "旧清单必须原样保留"
    assert all(d.get("enabled") for d in coll.docs), "不应有标的被误禁用"
    print("  ✓ 拉取被截断 → 跳过清理且旧清单原样保留")


def test_failed_bucket_skips_prune():
    """子查询抛异常同样属于「不完整」，必须跳过清理"""
    svc = HKUSPreheatService()
    db = _FakeDB({})
    stats, prune_tags, coll, _ = _run_universe_refresh(
        svc, db, member_count=5, existing_count=100, failed=2)
    assert _INDEX_TAG not in prune_tags, f"有子查询失败时应跳过清理，实际 {prune_tags}"
    assert stats[_INDEX_TAG] == 100, stats
    assert _tagged_enabled(coll) == 100, "旧清单必须原样保留"
    print("  ✓ 子查询失败 → 跳过清理")


def test_complete_fetch_runs_prune():
    """拉取完整且成分数正常 → 照常清理，保证调仓能被跟随"""
    svc = HKUSPreheatService()
    db = _FakeDB({})
    stats, prune_tags, coll, _ = _run_universe_refresh(
        svc, db, member_count=100, existing_count=100)
    assert _INDEX_TAG in prune_tags, f"正常情况应执行清理，实际 {prune_tags}"
    assert stats[_INDEX_TAG] == 100, stats
    print("  ✓ 拉取完整且无缩水 → 照常清理")


def test_first_build_runs_prune():
    """首次建立（库内为空）没有旧清单可保护，正常写入 + 清理"""
    svc = HKUSPreheatService()
    db = _FakeDB({})
    stats, prune_tags, coll, _ = _run_universe_refresh(
        svc, db, member_count=5, existing_count=0)
    assert _INDEX_TAG in prune_tags, f"首次建立应正常走清理，实际 {prune_tags}"
    assert stats[_INDEX_TAG] == 5, stats
    assert _tagged_enabled(coll) == 5
    print("  ✓ 首次建立正常写入 + 清理")


def test_complete_shrink_is_accepted():
    """
    第三轮 Review 的 P3 残留核心：**合法调仓缩水必须被接受**。
    HSTECH 一次调仓 30→23（不足 80%），旧实现按比例「锁存」跳过清理，
    于是新清单永远进不了库、每轮还重复打 Wind，只有 force 才能自愈。
    """
    svc = HKUSPreheatService()
    db = _FakeDB({})
    stats, prune_tags, coll, _ = _run_universe_refresh(
        svc, db, member_count=23, existing_count=30)
    assert _INDEX_TAG in prune_tags, f"合法调仓缩水应照常清理，实际 {prune_tags}"
    assert stats[_INDEX_TAG] == 23, stats
    assert _tagged_enabled(coll) == 23, "被剔除的 7 只应摘掉来源标签"
    assert stats["total"] == 23, "摘光来源的旧成分应 enabled=False，不参与预热"
    print("  ✓ 拉取完整时的合法缩水（30→23）被接受")


def test_incomplete_fetch_advances_freshness_and_does_not_hammer():
    """
    锁存的真正根因：跳过清理时**什么都不写** → `updated_at` 停在过去 →
    `_index_fresh` 永远 False → 每轮重拉、且永远拒绝新清单。
    修复后不完整拉取仍会增量补写，`updated_at` 因此前进，本刷新周期内不再重打。
    """
    svc = HKUSPreheatService()
    db = _FakeDB({})
    stats, prune_tags, coll, calls = _run_universe_refresh(
        svc, db, member_count=5, existing_count=100, truncated=True,
        force=False, stale=True)
    assert calls["fetch"] == 1, "首次应发起拉取"
    assert _INDEX_TAG not in prune_tags, "不完整拉取应跳过清理"
    assert stats[_INDEX_TAG] == 100, stats
    assert asyncio.run(svc._index_fresh(coll, _INDEX_TAG, 7)) is True, \
        "补写必须推进 updated_at，否则会永久锁存"

    # 第二个刷新周期内再跑一次：应当被新鲜度直接跳过，不再打 Wind
    _, prune_tags2, _, calls2 = _run_universe_refresh(
        svc, db, member_count=5, existing_count=100, truncated=True,
        force=False, coll=coll)
    assert calls2["fetch"] == 0, "同一刷新周期内不应重复拉取（避免每日空转）"
    assert _INDEX_TAG not in prune_tags2
    print("  ✓ 不完整拉取推进新鲜度且不重复重打（锁存已修）")


def test_fetch_recursive_reports_truncation():
    """细分到最大深度仍返回满页 → 如实标记 truncated，而不是无声丢弃"""
    svc = HKUSPreheatService()
    full_page = [(f"X{i:03d}.HK", "N") for i in range(100)]   # == _SEARCH_PAGE_LIMIT
    calls = {"n": 0}

    def _stub(question, index_name="", state=None):
        calls["n"] += 1
        return list(full_page)

    out, state = {}, {"truncated": False, "failed": []}
    with patch.object(svc, "_search_members", _stub):
        # 用单字符表递归，调用次数 = 深度 0..3 共 4 次
        svc._fetch_recursive("恒生指数", "", ["A"], out, 0, state)
    assert state["truncated"] is True, state
    assert calls["n"] == 4, f"应递归到最大深度，实际调用 {calls['n']} 次"

    # 未满页 → 不标记截断
    out2, state2 = {}, {"truncated": False, "failed": []}
    with patch.object(svc, "_search_members",
                      lambda q, i="", state=None: full_page[:99]):
        svc._fetch_recursive("恒生指数", "", ["A"], out2, 0, state2)
    assert state2["truncated"] is False
    assert len(out2) == 99
    print("  ✓ _fetch_recursive 如实上报截断")


def test_search_members_records_failure():
    """Wind 调用抛异常 → 计入 state['failed']，并冒泡到 _fetch_index_members"""
    svc = HKUSPreheatService()

    class _BoomClient:
        def call_table(self, *a, **kw):
            raise RuntimeError("wind down")

    state = {"truncated": False, "failed": []}
    with patch.object(svc, "_wind_client", lambda: _BoomClient()):
        rows = svc._search_members("恒生指数成分股", "恒生指数", state)
        assert rows == [] and len(state["failed"]) == 1, state
        members, state2 = svc._fetch_index_members("恒生指数", "HK")
    assert members == [] and state2["failed"], state2
    assert svc._fetch_incomplete(state2) is True
    print("  ✓ 子查询异常计入 failed 并冒泡")


def test_fetch_incomplete_ignores_clean_state():
    """完整性判定：无截断也无失败时不误报为不完整"""
    assert HKUSPreheatService._fetch_incomplete(None) is False
    assert HKUSPreheatService._fetch_incomplete({}) is False
    assert HKUSPreheatService._fetch_incomplete(
        {"truncated": False, "failed": []}) is False
    assert HKUSPreheatService._fetch_incomplete({"truncated": True}) is True
    assert HKUSPreheatService._fetch_incomplete({"failed": ["q"]}) is True
    print("  ✓ 完整性判定不误报")


# ==================== C. 市场识别各处一致性 ====================
_SAMPLES = [
    ("BRK.B", "US", "BRK.B"), ("BRK-B", "US", "BRK.B"),
    ("AAPL.O", "US", "AAPL"), ("CBOE.BAT", "US", "CBOE"),
    ("GOOGL.OQ", "US", "GOOGL"), ("AAPL", "US", "AAPL"),
    ("00700", "HK", "00700"), ("700.HK", "HK", "00700"),
    ("600519", "CN", "600519"), ("510300", "CN", "510300"),
]


def test_market_detection_expectations():
    """stocks.py 的识别结果符合预期（含点号类 / 交易所后缀）"""
    from app.routers.stocks import _detect_market_and_code as detect
    for raw, want_market, want_code in _SAMPLES:
        got_market, got_code = detect(raw)
        assert (got_market, got_code) == (want_market, want_code), \
            f"{raw}: 期望 {(want_market, want_code)}，实际 {(got_market, got_code)}"
    print("  ✓ stocks.py 市场识别（BRK.B / AAPL.O / CBOE.BAT）")


def test_market_detection_consistent_between_routers():
    """stocks.py 与 paper.py 两份实现必须完全一致（P2：paper 曾漏改）"""
    from app.routers.stocks import _detect_market_and_code as s
    from app.routers.paper import _detect_market_and_code as p
    for raw, _, _ in _SAMPLES:
        assert s(raw) == p(raw), f"{raw}: stocks={s(raw)} paper={p(raw)}"
    print("  ✓ stocks.py 与 paper.py 识别一致")


def test_us_exch_suffixes_single_source():
    """
    第三轮 Review 观察 3：后缀集合收敛为单一事实来源。
    历史上是四份字面量副本 + 「改动需同步」注释——任一处漏改都会造成
    「路由侧认美股、预热/分析侧认不出」的静默失配。
    """
    from app.core.market_codes import US_EXCH_SUFFIXES as shared
    from app.worker.hk_us_preheat_service import US_EXCH_SUFFIXES as preheat
    from app.routers.stocks import US_EXCH_SUFFIXES as stocks
    from app.routers.paper import US_EXCH_SUFFIXES as paper
    from tradingagents.utils.stock_utils import US_EXCH_SUFFIXES as analysis
    for name, got in (("preheat", preheat), ("stocks", stocks),
                      ("paper", paper), ("analysis", analysis)):
        assert got is shared, f"{name} 引用的不是共享集合（后缀集合已漂移）"
    assert "BAT" in shared and "A" not in shared and "B" not in shared
    print("  ✓ 四处引用同一后缀集合（防漂移）")


def test_normalize_symbol_by_market():
    """
    `app/core/market_codes.normalize_symbol` —— 校验/同步链路共用的按市场归一化。

    回归背景：数据校验服务曾对所有市场无条件 `zfill(6)`，把港股 5 位码
    `03669` 补成 `003669`，与 `stock_basic_info_hk` 的 5 位码永不匹配，
    表现为「基础信息不存在」的误报（且数据同步成功也无法自愈）。
    """
    from app.core.market_codes import internal_to_windcode, normalize_symbol

    assert normalize_symbol("3669", "hk") == "03669"
    assert normalize_symbol("03669", "hk") == "03669"
    assert normalize_symbol("003669", "hk") == "03669"        # 旧 bug 脏值 → 修回 5 位
    assert normalize_symbol("0700.HK", "hk") == "00700"
    assert normalize_symbol("700", "hk") == "00700"

    assert normalize_symbol("aapl", "us") == "AAPL"
    assert normalize_symbol("AAPL.O", "us") == "AAPL"
    assert normalize_symbol("BRK-B", "us") == "BRK.B"
    assert normalize_symbol("BRK_B.N", "us") == "BRK.B"

    assert normalize_symbol("1", "cn") == "000001"
    assert normalize_symbol("600519", "cn") == "600519"

    # 市场被写成中文/大写也应正确分流
    assert normalize_symbol("003669", "港股") == "03669"
    assert normalize_symbol("AAPL", "US_STOCKS") == "AAPL"

    # 内部 symbol → Wind 代码（universe 注册用）
    assert internal_to_windcode("03669", "hk") == "3669.HK"
    assert internal_to_windcode("00700", "hk") == "0700.HK"
    assert internal_to_windcode("NVDA", "us") == "NVDA"

    # 第五轮 Review P3-1：无效长码必须判为「无法识别」，不得静默截末 4 位
    # 注册成另一只股票（"1234567" → "4567.HK" 曾会把 3669 之外的他司数据写进来）
    assert normalize_symbol("123456", "hk") == ""
    assert normalize_symbol("1234567", "hk") == ""
    assert internal_to_windcode("1234567", "hk") == ""
    # 但旧脏值（6 位、有效 4 位）仍须修回 5 位，不能被 guard 误伤
    assert normalize_symbol("003669", "hk") == "03669"
    assert internal_to_windcode("003669", "hk") == "3669.HK"
    print("  ✓ 按市场归一化内部 symbol（含旧补零脏值修复 + 无效长码拒绝）")


def test_guess_market_and_collection_routing():
    """
    第五轮 Review P3-2：名称解析拿不到 market 参数，只能按码形推断市场，
    否则港股 5 位码会被 zfill(6) 后查 A 股库 → 永远失配（报告里退回「股票03669」）。
    """
    from app.core.market_codes import basic_info_collection, guess_market, normalize_symbol

    cases = {
        "03669": ("hk", "03669"),        # 港股 5 位
        "00700": ("hk", "00700"),
        "700": ("hk", "00700"),          # 港股 4 位
        "0700.HK": ("hk", "00700"),      # 显式 .HK 后缀优先
        "000001": ("cn", "000001"),      # A 股 6 位
        "003669": ("cn", "003669"),      # 6 位（含脏值形态）按 A 股处理，P3-3
        "600519.SH": ("cn", "600519"),
        "AAPL": ("us", "AAPL"),
        "AAPL.O": ("us", "AAPL"),
        "BRK.B": ("us", "BRK.B"),
        "": ("cn", ""),
    }
    for raw, (want_market, want_code) in cases.items():
        got_market = guess_market(raw)
        assert got_market == want_market, f"{raw!r}: market {got_market} != {want_market}"
        assert normalize_symbol(raw, got_market) == want_code, \
            f"{raw!r}: code {normalize_symbol(raw, got_market)!r} != {want_code!r}"

    # 集合名按市场分流（名称解析共用，避免各处硬编码）
    assert basic_info_collection("hk") == "stock_basic_info_hk"
    assert basic_info_collection("us") == "stock_basic_info_us"
    assert basic_info_collection("cn") == "stock_basic_info"
    assert basic_info_collection("港股") == "stock_basic_info_hk"
    assert basic_info_collection("未知") == "stock_basic_info"

    # 端到端：港股 03669 必须命中 _hk 集合且键为 5 位
    m = guess_market("03669")
    assert (basic_info_collection(m), normalize_symbol("03669", m)) == \
        ("stock_basic_info_hk", "03669")
    print("  ✓ 名称解析按码形分流（港股不再补成 6 位查 A 股库）")


def test_preheat_stats_skipped_is_success():
    """
    单股按需同步的成败判定：`skipped`＝节流/增量窗口内「数据已是最新」，
    必须算成功——否则首发后的第二次同步会被误报为失败。
    """
    from app.routers.stock_sync import _interpret_preheat_stats

    assert _interpret_preheat_stats({"total": 1, "ok": 1, "skipped": 0, "failed": 0})[0] is True
    assert _interpret_preheat_stats({"total": 1, "ok": 0, "skipped": 1, "failed": 0})[0] is True
    assert _interpret_preheat_stats({"total": 1, "ok": 0, "skipped": 1, "failed": 1})[0] is False
    assert _interpret_preheat_stats({"total": 1, "ok": 0, "skipped": 0, "failed": 1})[0] is False
    ok, why = _interpret_preheat_stats({"total": 0, "ok": 0, "skipped": 0, "failed": 0})
    assert ok is False and why, "空目标池应报明确原因"
    print("  ✓ skipped 判定为成功（数据已最新）")


def test_analysis_side_market_detection_matches_routers():
    """
    第三轮 Review 观察 2：分析链路（identify_stock_market）原先不认多字母
    交易所后缀，`CBOE.BAT` 会落 UNKNOWN。现与路由层对齐。
    """
    from tradingagents.utils.stock_utils import StockUtils, StockMarket
    _MAP = {"US": StockMarket.US, "HK": StockMarket.HONG_KONG,
            "CN": StockMarket.CHINA_A}
    for raw, want_market, _ in _SAMPLES:
        got = StockUtils.identify_stock_market(raw)
        assert got is _MAP[want_market], \
            f"{raw}: 分析侧 {got.value} vs 路由侧 {want_market}"
    # 未知多字母尾段两侧都不认（路由按默认 A 股兜底、分析按 UNKNOWN）
    assert StockUtils.identify_stock_market("BRK.XY") is StockMarket.UNKNOWN
    print("  ✓ 分析侧与路由层市场识别一致（含 CBOE.BAT）")


# ==================== 运行入口 ====================

def _main():
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    print(f"\n🧪 港美股预热代码/不变式测试（{len(tests)} 例）")
    print("=" * 72)
    failed = 0
    for name, fn in tests:
        try:
            fn()
        except AssertionError as e:
            failed += 1
            print(f"  ✗ {name}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  ✗ {name}: {type(e).__name__}: {e}")
    print("=" * 72)
    if failed:
        print(f"❌ {failed}/{len(tests)} 例失败")
        return 1
    print(f"✅ 全部 {len(tests)} 例通过")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
