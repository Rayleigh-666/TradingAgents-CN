#!/usr/bin/env python3
"""
港美股预热：代码转换 / 历史不变式 / 市场识别一致性 —— 单元测试

补 c4fec099 那批修复「测试未落库」的缺口，覆盖最易回归的三处：

A. 代码转换（`_us_ticker` / `to_internal_symbol` / `to_yahoo_ticker` / `windcode_matches`）
   —— 含第二轮 Review 新修的「连字符归一」（自选股存 `BRK-B` 时不致与 `BRK.B` 分裂）
B. 日线写入不变式「已存历史永不缩短」
   —— `_purge_conflicting_dates` 只清同日异源、`_write_single_source` 拒绝缩短、
      `_build_daily_ops` 前收链（顺带证伪「无效行导致 prev_close 跳格」）
C. 市场识别三份实现一致性（stocks.py / paper.py 的 normalized_code 必须一致）

可直接运行：`python tests/test_hk_us_preheat_codes.py`
"""

import asyncio
import os
import sys
from datetime import datetime
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
            existing = [d for d in self.docs if _match(d, filt)]
            if existing:
                existing[0].update(set_)
                modified += 1
            else:
                new_doc = dict(filt)
                new_doc.update(set_)
                new_doc.update(doc.get("$setOnInsert") or {})
                self.docs.append(new_doc)
                upserted += 1
        return _BulkResult(upserted, modified)


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


# ==================== B2. 指数成分「暴跌保护」 ====================

class _FakeSettings:
    HK_US_PREHEAT_INDEXES = "恒生指数"
    HK_US_PREHEAT_UNIVERSE_REFRESH_DAYS = 7


def _run_universe_refresh(svc, db, member_count, existing_count):
    """跑一次 refresh_universe，返回 (stats, 被调用的 prune 来源标签列表)"""
    universe = _FakeColl([
        {"market": "HK", "symbol": f"{i:05d}", "sources": ["index:恒生指数"],
         "enabled": True}
        for i in range(existing_count)
    ])
    db._colls["hk_us_preheat_universe"] = universe
    calls = {"prune": []}

    async def _fake_merge(coll):
        return 0, []

    async def _fake_prune(coll, tag, keep):
        calls["prune"].append(tag)
        return 0

    def _fake_members(name, market):
        return [{"windcode": "0700.HK", "name": "X"} for _ in range(member_count)]

    with patch.object(svc, "_merge_favorites", _fake_merge), \
         patch.object(svc, "_prune_source", _fake_prune), \
         patch.object(svc, "_fetch_index_members", _fake_members), \
         patch(DB_PATCH, lambda: db), \
         patch("app.worker.hk_us_preheat_service.settings", _FakeSettings):
        stats = asyncio.run(svc.refresh_universe(force=True))
    # 注意：_prune_source 还会被自选股分支调用一次（tag="favorite"），
    # 故断言只看指数来源那一项
    return stats, calls["prune"]


_INDEX_TAG = "index:恒生指数"


def test_prune_skipped_when_members_shrink_suspiciously():
    """
    P3-3：上游按字母分批拉取时若有桶失败，成分数会暴跌；
    此时必须跳过清理，否则缺失桶的成分会被误摘标签 / 误禁用
    """
    svc = HKUSPreheatService()
    db = _FakeDB({})
    stats, prune_tags = _run_universe_refresh(svc, db, member_count=5,
                                              existing_count=100)
    assert _INDEX_TAG not in prune_tags, f"暴跌时应跳过清理，实际 {prune_tags}"
    assert stats[_INDEX_TAG] == 100, stats
    print("  ✓ 成分数暴跌时跳过清理（P3-3）")


def test_prune_runs_when_members_look_complete():
    """成分数正常（≥ 库内 80%）时照常清理，保证调仓能被跟随"""
    svc = HKUSPreheatService()
    db = _FakeDB({})
    stats, prune_tags = _run_universe_refresh(svc, db, member_count=100,
                                              existing_count=100)
    assert _INDEX_TAG in prune_tags, f"正常情况应执行清理，实际 {prune_tags}"
    print("  ✓ 成分数正常时照常清理")


def test_prune_runs_on_first_build():
    """首次建立（库内为空）不做暴跌保护，正常写入"""
    svc = HKUSPreheatService()
    db = _FakeDB({})
    stats, prune_tags = _run_universe_refresh(svc, db, member_count=5,
                                              existing_count=0)
    assert _INDEX_TAG in prune_tags, f"首次建立应正常走清理，实际 {prune_tags}"
    print("  ✓ 首次建立不做暴跌保护")


# ==================== C. 市场识别三份实现一致性 ====================
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
