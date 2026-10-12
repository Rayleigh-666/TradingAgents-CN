#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
分析前预检（/api/analysis/precheck）必须按市场查对应的基础信息集合。

回归背景
--------
预检第 3 步原先写死 `db["stock_basic_info"]` 且直接用原始 symbol 查询，
港股 03669 的基础信息落在 `stock_basic_info_hk`，于是**永远**判定
「股票 03669 的基础数据未同步，请先同步该股票数据」——用户明明已同步成功，
点「开始分析」仍被拦下（且该接口连 market_type 参数都没有，前端也无从传起）。

修复后：预检接受 market_type，用 `normalize_symbol` 归一化代码、
用 `basic_info_collection` 选集合；且**绝不回退**到 A 股集合。

本用例用假 DB 断言「实际查询的集合名 + 归一化后的查询键」，不依赖真实 MongoDB。
"""

import asyncio
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

# 支持 `python tests/test_analysis_precheck_market.py` 直接运行（pytest 走 conftest.py）
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from app.routers.analysis import precheck_analysis


# ==================== 假 DB ====================

class _FakeCollection:
    """记录被查询的键，命中则返回一条文档。"""

    def __init__(self, present_codes=(), total=None):
        self.present = {str(c) for c in present_codes}
        self.total = len(self.present) if total is None else total
        self.queried_codes = []          # 每次 find_one 查询的候选键

    async def count_documents(self, _query):
        return self.total

    async def find_one(self, query, _projection=None):
        codes = set()
        for cond in query.get("$or", []):
            codes.update(str(v) for k, v in cond.items() if k in ("code", "symbol"))
        self.queried_codes.append(codes)
        hit = codes & self.present
        return {"code": sorted(hit)[0]} if hit else None


class _FakeDB:
    def __init__(self, **collections):
        self._colls = dict(collections)

    def __getitem__(self, name):
        return self._colls.setdefault(name, _FakeCollection(total=0))


def _fake_config():
    return SimpleNamespace(data_source_configs=[
        SimpleNamespace(enabled=True, type="yahoo"),
        SimpleNamespace(enabled=True, type="local"),   # 缓存型，应被忽略
    ])


async def _precheck(db, symbol, market_type):
    with patch("app.routers.analysis.get_mongo_db", return_value=db), \
         patch("app.services.config_service.config_service.get_system_config",
               new=AsyncMock(return_value=_fake_config())):
        return await precheck_analysis(symbol=symbol, market_type=market_type, user={})


def _run(coro):
    return asyncio.run(coro)


# ==================== 用例 ====================

def test_hk_target_passes_and_queries_hk_collection():
    """核心回归：港股已同步 → 放行，且查的是 _hk 集合、键为 5 位码。"""
    db = _FakeDB(
        stock_basic_info=_FakeCollection(total=11147),      # A 股库里有别的股票
        stock_basic_info_hk=_FakeCollection(["03669"], total=674),
    )
    r = _run(_precheck(db, "03669", "hk"))

    assert r["can_submit"] is True, r
    assert r["reason"] == "ok"
    hk = db["stock_basic_info_hk"]
    assert hk.queried_codes == [{"03669"}], hk.queried_codes
    # 关键：不允许回退去查 A 股集合（旧实现正是在这里误报）
    assert db["stock_basic_info"].queried_codes == [], \
        "港股预检不得查询 stock_basic_info"


def test_hk_dirty_code_003669_normalized_to_5_digits():
    """旧补零脏值 003669 作为输入，也应归一化为 03669 后命中。"""
    db = _FakeDB(stock_basic_info_hk=_FakeCollection(["03669"], total=674))
    r = _run(_precheck(db, "003669", "hk"))

    assert r["can_submit"] is True, r
    assert db["stock_basic_info_hk"].queried_codes == [{"03669"}]


def test_hk_not_found_in_a_share_collection_still_blocked():
    """03669 只存在于 A 股集合时，港股预检仍须判定未同步（不跨市场误放行）。"""
    db = _FakeDB(
        stock_basic_info=_FakeCollection(["03669", "600519"], total=11147),
        # _hk 有基础数据（否则会先命中 no_basics），但其中没有 03669
        stock_basic_info_hk=_FakeCollection(["00700", "00001"], total=674),
    )
    r = _run(_precheck(db, "03669", "hk"))

    assert r["can_submit"] is False, r
    assert r["reason"] == "stock_not_found", r
    assert r["details"]["basics_collection"] == "stock_basic_info_hk"


def test_us_target_queries_us_collection():
    db = _FakeDB(
        stock_basic_info=_FakeCollection(total=11147),
        stock_basic_info_us=_FakeCollection(["AAPL", "NVDA"], total=517),
    )
    r = _run(_precheck(db, "aapl", "us"))          # 小写输入也应归一化为 AAPL

    assert r["can_submit"] is True, r
    assert db["stock_basic_info_us"].queried_codes == [{"AAPL"}]
    assert db["stock_basic_info"].queried_codes == []


def test_cn_behavior_unchanged():
    """A 股行为保持原样：查 stock_basic_info，1 → 000001，缺失则拦。"""
    db = _FakeDB(stock_basic_info=_FakeCollection(["000001", "600519"], total=11147))

    r1 = _run(_precheck(db, "1", "cn"))
    assert r1["can_submit"] is True, r1
    assert db["stock_basic_info"].queried_codes == [{"000001"}]

    r2 = _run(_precheck(db, "999999", "cn"))
    assert r2["can_submit"] is False and r2["reason"] == "stock_not_found"


def test_invalid_long_hk_code_is_blocked_not_mis_routed():
    """无效长码（港股有效位数最多 5 位）→ 判未同步，且不得被静默截成他司代码。"""
    db = _FakeDB(stock_basic_info_hk=_FakeCollection(["03669"], total=674))
    r = _run(_precheck(db, "1234567", "hk"))

    assert r["can_submit"] is False
    assert r["reason"] == "stock_not_found"
    assert db["stock_basic_info_hk"].queried_codes == [], "无效码不应发起查询"


def test_market_aware_basics_count():
    """港股部署：仅有 _hk 基础数据、A 股集合为空时，仍应放行（不再误判「尚未同步」）。"""
    db = _FakeDB(
        stock_basic_info=_FakeCollection(total=0),
        stock_basic_info_hk=_FakeCollection(["00700"], total=674),
    )
    r = _run(_precheck(db, "00700", "hk"))

    assert r["can_submit"] is True, r

    # 对照组：该市场基础数据为空 → no_basics
    db2 = _FakeDB(stock_basic_info=_FakeCollection(total=11147),
                  stock_basic_info_hk=_FakeCollection(total=0))
    r2 = _run(_precheck(db2, "00700", "hk"))
    assert r2["can_submit"] is False and r2["reason"] == "no_basics"


def test_no_symbol_passes_for_all_markets():
    db = _FakeDB(
        stock_basic_info=_FakeCollection(total=11147),
        stock_basic_info_hk=_FakeCollection(total=674),
        stock_basic_info_us=_FakeCollection(total=517),
    )
    for mkt in ("cn", "hk", "us", "港股", "美股", "US_STOCKS"):
        r = _run(_precheck(db, None, mkt))
        assert r["can_submit"] is True, (mkt, r)


def test_no_datasource_still_blocks():
    """第 1 步数据源检查不受影响（全是缓存型源 → 拦截）。"""
    db = _FakeDB(stock_basic_info=_FakeCollection(total=11147))
    with patch("app.routers.analysis.get_mongo_db", return_value=db), \
         patch("app.services.config_service.config_service.get_system_config",
               new=AsyncMock(return_value=SimpleNamespace(data_source_configs=[
                   SimpleNamespace(enabled=True, type="local")]))):
        r = _run(precheck_analysis(symbol="600519", market_type="cn", user={}))

    assert r["can_submit"] is False and r["reason"] == "no_datasource"


# ==================== 运行入口 ====================

def _main():
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    print(f"\n🧪 分析预检按市场分流测试（{len(tests)} 例）")
    print("=" * 72)
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  ✓ {name}")
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
