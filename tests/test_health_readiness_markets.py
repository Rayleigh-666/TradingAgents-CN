#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
系统就绪检查（/api/system/readiness）必须按市场统计基础数据，而非只数 A 股集合。

回归背景
--------
`app/routers/health.py` 的就绪检查（原 87/131 行）写死
`db["stock_basic_info"].count_documents({})`，而基础信息按市场分集合存放
（cn→`stock_basic_info` / hk→`stock_basic_info_hk` / us→`stock_basic_info_us`）。
港股/美股专用部署只同步了 `_hk`/`_us`，A 股集合为空 → readiness 误报
`basics_synced=false`「基础数据未同步」，首页引导卡片一直卡在第 3 步。

修复口径（全局就绪，无市场上下文）：**任一市场有基础数据即视为已同步**；
`basics_count` 为三市场合计，新增 `basics_by_market` 明细。
两处分支（jdyun 特化 + 常规）共用 `count_basics_by_market()`。

本用例用假 DB 断言「统计到的集合 + 汇总口径」，不依赖真实 MongoDB。
"""

import asyncio
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

# 支持 `python tests/test_health_readiness_markets.py` 直接运行（pytest 走 conftest.py）
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from app.routers.health import count_basics_by_market, system_readiness


# ==================== 假 DB ====================

class _FakeCollection:
    def __init__(self, total=0, raises=False):
        self.total = total
        self.raises = raises
        self.count_calls = 0

    async def count_documents(self, _query):
        self.count_calls += 1
        if self.raises:
            raise RuntimeError("boom")
        return self.total


class _FakeDB:
    def __init__(self, **collections):
        self._colls = dict(collections)

    def __getitem__(self, name):
        return self._colls.setdefault(name, _FakeCollection(total=0))


def _ready_config():
    """数据源 + LLM 都已配置，便于把断言聚焦在第 3 步基础数据上。"""
    return SimpleNamespace(
        data_source_configs=[SimpleNamespace(enabled=True, type="yahoo")],
        llm_configs=[SimpleNamespace(enabled=True)],
    )


def _run_readiness(db, jdyun=False, config=None):
    with patch("app.core.database.get_mongo_db", return_value=db), \
         patch("app.routers.health.is_jdyun_mode", return_value=jdyun), \
         patch("app.services.config_service.config_service.get_system_config",
               new=AsyncMock(return_value=config or _ready_config())):
        return asyncio.run(system_readiness())


def _run(coro):
    return asyncio.run(coro)


# ==================== 用例 ====================

def test_hk_only_deployment_reports_synced():
    """核心回归：只有 _hk 有基础数据、A 股集合为空 → 不得误报「基础数据未同步」。"""
    db = _FakeDB(
        stock_basic_info=_FakeCollection(total=0),       # A 股未同步
        stock_basic_info_hk=_FakeCollection(total=674),
    )
    r = _run_readiness(db)

    assert r["basics_synced"] is True, r
    assert r["overall_ready"] is True, r
    assert r["next_step"] == "ready", r
    assert r["basics_count"] == 674, r
    assert r["basics_by_market"] == {"cn": 0, "hk": 674, "us": 0}, r
    # 三张集合都应被统计到（不是只查 A 股）
    assert db["stock_basic_info_hk"].count_calls == 1


def test_us_only_deployment_reports_synced():
    db = _FakeDB(
        stock_basic_info=_FakeCollection(total=0),
        stock_basic_info_us=_FakeCollection(total=517),
    )
    r = _run_readiness(db)

    assert r["basics_synced"] is True, r
    assert r["basics_count"] == 517, r
    assert r["basics_by_market"] == {"cn": 0, "hk": 0, "us": 517}, r


def test_all_markets_empty_still_blocks_with_sync_step():
    """三市场皆空 → 保持拦截，next_step=sync_data（不因改动而放宽）。"""
    db = _FakeDB()          # 所有集合默认 total=0
    r = _run_readiness(db)

    assert r["basics_synced"] is False, r
    assert r["overall_ready"] is False, r
    assert r["next_step"] == "sync_data", r
    assert r["basics_count"] == 0, r
    assert r["basics_by_market"] == {"cn": 0, "hk": 0, "us": 0}, r


def test_basics_count_is_sum_across_markets():
    db = _FakeDB(
        stock_basic_info=_FakeCollection(total=11147),
        stock_basic_info_hk=_FakeCollection(total=674),
        stock_basic_info_us=_FakeCollection(total=517),
    )
    r = _run_readiness(db)

    assert r["basics_count"] == 11147 + 674 + 517, r
    assert r["basics_by_market"] == {"cn": 11147, "hk": 674, "us": 517}, r


def test_collection_names_follow_market_codes_helper():
    """统计的集合名必须来自 basic_info_collection（防再次硬编码漂移）。"""
    from app.core.market_codes import basic_info_collection

    db = _FakeDB()
    counts = _run(count_basics_by_market(db))

    assert set(counts) == {"cn", "hk", "us"}
    for market in ("cn", "hk", "us"):
        # 访问该集合即视为被统计（_FakeDB 会按需创建并记录）
        assert basic_info_collection(market) in db._colls


def test_single_market_failure_is_isolated():
    """单个市场统计抛错不应让整个就绪检查失败（按 0 计，其余照常）。"""
    db = _FakeDB(
        stock_basic_info=_FakeCollection(total=11147),
        stock_basic_info_hk=_FakeCollection(raises=True),
        stock_basic_info_us=_FakeCollection(total=517),
    )
    r = _run_readiness(db)

    assert r["basics_synced"] is True, r
    assert r["basics_by_market"] == {"cn": 11147, "hk": 0, "us": 517}, r
    assert r["basics_count"] == 11147 + 517, r


def test_jdyun_branch_also_market_aware():
    """jdyun 特化分支同样按市场统计（原 87 行处）。"""
    db = _FakeDB(
        stock_basic_info=_FakeCollection(total=0),
        stock_basic_info_hk=_FakeCollection(total=674),
    )
    r = _run_readiness(db, jdyun=True)

    assert r.get("jdyun_mode") is True, r
    assert r["basics_synced"] is True, r
    assert r["overall_ready"] is True, r
    assert r["basics_by_market"] == {"cn": 0, "hk": 674, "us": 0}, r


def test_jdyun_branch_still_blocks_when_empty():
    db = _FakeDB()
    r = _run_readiness(db, jdyun=True)

    assert r["basics_synced"] is False, r
    assert r["next_step"] == "sync_data", r


def test_datasource_step_still_takes_precedence():
    """改动不越过前两步：数据源未配置时仍是 configure_datasource。"""
    db = _FakeDB(stock_basic_info=_FakeCollection(total=11147))
    r = _run_readiness(db, config=SimpleNamespace(
        data_source_configs=[SimpleNamespace(enabled=True, type="local")],   # 缓存型 → 不算
        llm_configs=[SimpleNamespace(enabled=True)],
    ))

    assert r["next_step"] == "configure_datasource", r
    assert r["datasource_configured"] is False, r


# ==================== 运行入口 ====================

def _main():
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    print(f"\n🧪 系统就绪检查按市场统计测试（{len(tests)} 例）")
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
