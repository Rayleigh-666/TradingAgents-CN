#!/usr/bin/env python3
"""
港美股预热「基础信息 / 财务」刷新周期节流（省 Wind 积分）—— 单元测试

背景：基础信息与财务的主源是 Wind（`HK_US_PREHEAT_PRIMARY_SOURCE` 只作用于日线），
且原先没有增量逻辑，随日频 cron 每次全量重拉（约 2380 积分/天）。
本测试覆盖新加的按 `updated_at` 节流：

1. `_as_naive_dt` 对 naive / aware / ISO 字符串 / 非法值的归一化
2. `_is_fresh` 的边界语义（正好等于 cutoff 算新鲜）
3. `_split_fresh` 按库内更新时间拆分 fresh / stale，refresh_days=0 时关闭节流
4. 节流命中时**不发起任何外部请求**，被跳过的标的计入 stats["skipped"] 而非 failed
5. `force=True` 绕过节流，强制全量

可直接运行：`python tests/test_hk_us_preheat_meta_throttle.py`
（亦兼容 pytest 收集；项目 venv 未安装 pytest，故同时提供 __main__ 入口）
"""

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from app.worker.hk_us_preheat_service import HKUSPreheatService

DB_PATCH = "app.worker.hk_us_preheat_service.get_mongo_db"


# ==================== 轻量假 MongoDB ====================

class _FakeCursor:
    """支持 `async for` 的游标"""

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


class _FakeUpdateResult:
    upserted_id = None


class _FakeColl:
    def __init__(self, docs=None):
        self.docs = list(docs or [])
        self.filters = []      # 记录 update_one 的 filter，用于断言「只写 stale」

    def find(self, *a, **kw):
        return _FakeCursor(self.docs)

    async def update_one(self, filter_, *a, **kw):
        self.filters.append(filter_)
        return _FakeUpdateResult()

    async def count_documents(self, *a, **kw):
        return len(self.docs)

    @property
    def written_symbols(self):
        return [f.get("symbol") for f in self.filters]


class _FakeDB:
    def __init__(self, colls=None):
        self._colls = dict(colls or {})

    def __getitem__(self, name):
        return self._colls.setdefault(name, _FakeColl())


# ==================== 用例 ====================

def _targets():
    """两只港股（windcode 与内部 symbol 不同，顺带验证 key 映射正确）"""
    return [
        {"market": "HK", "symbol": "00700", "windcode": "0700.HK",
         "yahoocode": "0700.HK", "yahoo_only": False},
        {"market": "HK", "symbol": "00005", "windcode": "0005.HK",
         "yahoocode": "0005.HK", "yahoo_only": False},
    ]


def test_as_naive_dt_normalizes():
    """naive / aware / ISO 字符串统一成本地 naive；非法值返回 None"""
    svc = HKUSPreheatService()
    naive = datetime(2026, 10, 1, 12, 0, 0)
    assert svc._as_naive_dt(naive) == naive

    aware = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)
    got = svc._as_naive_dt(aware)
    assert got is not None and got.tzinfo is None
    assert got == aware.astimezone().replace(tzinfo=None)

    assert svc._as_naive_dt("2026-10-01T12:00:00") == naive
    assert svc._as_naive_dt("2026-10-01T12:00:00Z") is not None

    assert svc._as_naive_dt(None) is None
    assert svc._as_naive_dt(12345) is None
    assert svc._as_naive_dt("not-a-date") is None
    print("  ✓ _as_naive_dt 归一化（naive/aware/ISO/非法值）")


def test_is_fresh_boundary():
    """截止时间正好相等算新鲜；早 1 秒算过期"""
    svc = HKUSPreheatService()
    cutoff = datetime(2026, 10, 1, 0, 0, 0)
    assert svc._is_fresh(cutoff, cutoff) is True
    assert svc._is_fresh(cutoff + timedelta(seconds=1), cutoff) is True
    assert svc._is_fresh(cutoff - timedelta(seconds=1), cutoff) is False
    assert svc._is_fresh(None, cutoff) is False
    print("  ✓ _is_fresh 边界（相等算新鲜）")


def test_split_fresh_splits_by_updated_at():
    """库内更新在周期内的 → fresh；超期或库内没有的 → stale"""
    svc = HKUSPreheatService()
    now = datetime.now()
    coll = _FakeColl([
        {"symbol": "00700", "updated_at": now - timedelta(days=1)},   # 新鲜
        {"symbol": "00005", "updated_at": now - timedelta(days=30)},  # 超期
    ])
    db = _FakeDB({"stock_basic_info_hk": coll})
    with patch(DB_PATCH, lambda: db):
        fresh, stale = asyncio.run(svc._split_fresh("basic", _targets(), 14))
    assert [t["symbol"] for t in fresh] == ["00700"], fresh
    assert [t["symbol"] for t in stale] == ["00005"], stale
    print("  ✓ _split_fresh 按 updated_at 拆分")


def test_split_fresh_handles_missing_and_string_updated_at():
    """库内缺记录 / updated_at 为字符串 时的判定"""
    svc = HKUSPreheatService()
    now = datetime.now()
    coll = _FakeColl([
        # 字符串形式且很新 → 仍应判为新鲜
        {"symbol": "00700", "updated_at": (now - timedelta(days=2)).isoformat()},
        # 无 updated_at → 不新鲜
        {"symbol": "00005"},
    ])
    db = _FakeDB({"stock_basic_info_hk": coll})
    with patch(DB_PATCH, lambda: db):
        fresh, stale = asyncio.run(svc._split_fresh("basic", _targets(), 14))
    assert [t["symbol"] for t in fresh] == ["00700"]
    assert [t["symbol"] for t in stale] == ["00005"]
    print("  ✓ _split_fresh 兼容字符串 / 缺失 updated_at")


def test_split_fresh_disabled_when_zero():
    """refresh_days=0 → 不节流，全部待刷新且不查库"""
    svc = HKUSPreheatService()
    db = _FakeDB({})
    with patch(DB_PATCH, lambda: db):
        fresh, stale = asyncio.run(svc._split_fresh("basic", _targets(), 0))
    assert fresh == []
    assert len(stale) == 2
    print("  ✓ _split_fresh refresh_days=0 关闭节流")


def _run_sync(svc, kind, targets, db, force=False):
    """跑一次 sync_basic_info / sync_financial，返回 (stats, 外部调用计数)"""
    calls = {"batch": 0, "single": 0, "retry": 0}

    def _fake_batch(codes):
        calls["batch"] += 1
        return {c: {"name": "STUB", "list_date": "2000-01-01",
                    "report_period": "2026-06-30", "revenue": 1.0}
                for c in codes}

    def _fake_financial_batch(codes):
        calls["batch"] += 1
        return {c: {"report_period": "2026-06-30", "report_period_label": "2026中报",
                    "revenue": 1.0, "net_profit": 0.5, "roe": 10.0}
                for c in codes}

    def _fake_single(*a, **kw):
        calls["single"] += 1
        return None

    async def _fake_retry(tg, info_map, stats):
        calls["retry"] += 1

    async def _fake_load(*a, **kw):
        return targets

    if kind == "basic":
        patches = [
            patch.object(svc, "_load_targets", _fake_load),
            patch.object(svc, "_fetch_basic_batch", _fake_batch),
            patch.object(svc, "_fetch_basic_info", _fake_single),
            patch.object(svc, "_retry_missing_basic", _fake_retry),
            patch(DB_PATCH, lambda: db),
        ]
        runner = lambda: svc.sync_basic_info(market="HK", force=force)
    else:
        patches = [
            patch.object(svc, "_load_targets", _fake_load),
            patch.object(svc, "_fetch_financial_batch", _fake_financial_batch),
            patch.object(svc, "_fetch_financial", _fake_single),
            patch(DB_PATCH, lambda: db),
        ]
        runner = lambda: svc.sync_financial(market="HK", force=force)

    for p in patches:
        p.start()
    try:
        stats = asyncio.run(runner())
    finally:
        for p in patches:
            p.stop()
    return stats, calls


def test_sync_basic_info_skips_fresh_without_requests():
    """一只新鲜 + 一只过期：只对过期的那只发起请求，新鲜的不计入 failed"""
    svc = HKUSPreheatService()
    now = datetime.now()
    coll = _FakeColl([
        {"symbol": "00700", "updated_at": now - timedelta(days=1)},
        {"symbol": "00005", "updated_at": now - timedelta(days=30)},
    ])
    db = _FakeDB({"stock_basic_info_hk": coll})
    stats, calls = _run_sync(svc, "basic", _targets(), db)

    assert stats["total"] == 2
    assert stats["skipped"] == 1, stats
    assert stats["ok"] == 1, stats
    assert calls["batch"] == 1 and calls["single"] == 0, calls
    assert coll.written_symbols == ["00005"], coll.written_symbols
    print("  ✓ sync_basic_info 跳过新鲜标的且不发起请求")


def test_sync_financial_skips_fresh_without_requests():
    """财务同理：新鲜标的零请求、零 failed"""
    svc = HKUSPreheatService()
    now = datetime.now()
    coll = _FakeColl([
        {"symbol": "00700", "updated_at": now - timedelta(days=2)},
        {"symbol": "00005", "updated_at": now - timedelta(days=60)},
    ])
    db = _FakeDB({"stock_financial_data_hk": coll})
    stats, calls = _run_sync(svc, "financial", _targets(), db)

    assert stats["total"] == 2
    assert stats["skipped"] == 1, stats
    assert stats["ok"] == 1, stats
    assert calls["batch"] == 1 and calls["single"] == 0, calls
    assert coll.written_symbols == ["00005"], coll.written_symbols
    print("  ✓ sync_financial 跳过新鲜标的且不发起请求")


def test_all_fresh_means_zero_external_calls():
    """全部在周期内 → 一次外部请求都不发（这是省积分的核心路径）"""
    svc = HKUSPreheatService()
    now = datetime.now()
    coll = _FakeColl([
        {"symbol": "00700", "updated_at": now - timedelta(days=1)},
        {"symbol": "00005", "updated_at": now - timedelta(days=3)},
    ])
    db = _FakeDB({"stock_basic_info_hk": coll})
    stats, calls = _run_sync(svc, "basic", _targets(), db)

    assert stats == {"total": 2, "ok": 0, "failed": 0, "skipped": 2,
                     "failed_symbols": [], "batches": 0}, stats
    assert calls["batch"] == 0 and calls["single"] == 0 and calls["retry"] == 0, calls
    assert coll.written_symbols == [], coll.written_symbols
    print("  ✓ 全新鲜：零外部请求（省积分核心路径）")


def test_force_bypasses_throttle():
    """force=True 忽略节流，新鲜标的也会被重新拉取"""
    svc = HKUSPreheatService()
    now = datetime.now()
    coll = _FakeColl([
        {"symbol": "00700", "updated_at": now - timedelta(days=1)},
        {"symbol": "00005", "updated_at": now - timedelta(days=3)},
    ])
    db = _FakeDB({"stock_basic_info_hk": coll})
    stats, calls = _run_sync(svc, "basic", _targets(), db, force=True)

    assert stats["skipped"] == 0, stats
    assert stats["ok"] == 2, stats
    assert calls["batch"] == 1, calls
    assert sorted(coll.written_symbols) == ["00005", "00700"], coll.written_symbols
    print("  ✓ force=True 绕过节流")


def test_new_symbol_always_refreshed():
    """新入池标的（库内无记录）立即刷新，不受节流影响"""
    svc = HKUSPreheatService()
    now = datetime.now()
    coll = _FakeColl([
        {"symbol": "00700", "updated_at": now - timedelta(days=1)},
    ])
    db = _FakeDB({"stock_basic_info_hk": coll})
    stats, calls = _run_sync(svc, "basic", _targets(), db)

    assert stats["skipped"] == 1
    assert stats["ok"] == 1
    assert coll.written_symbols == ["00005"]
    print("  ✓ 新入池标的立即刷新")


# ==================== 运行入口 ====================

def _main():
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    print(f"\n🧪 港美股预热元数据节流测试（{len(tests)} 例）")
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
