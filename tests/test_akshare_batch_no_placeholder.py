"""
AKShare 批量基础信息同步：占位行守卫

`stock_basic_info` 里 `name == "股票<code>"` 的记录是纯脏数据（真股票名不可能长这样），
一旦落库会让不存在的代码「查得到基础信息」。本用例锁定批量链路的两道防线：

1. `_build_basic_info_from_list` 不再伪造占位名（名称缺失留空）；
2. `_process_basic_info_batch` 对「名称缺失/占位」或 `sync_status == "degraded"`
   的结果**拒绝写库**，计入 error_count 并留下明确原因。
"""

import asyncio
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from app.worker.akshare_sync_service import AKShareSyncService  # noqa: E402


def _service(provider_basic_info):
    svc = AKShareSyncService()
    svc.db = Mock()
    svc.db.stock_basic_info.update_one = AsyncMock(return_value=Mock())
    svc.db.stock_basic_info.find_one = AsyncMock(return_value=None)
    svc.provider = Mock()
    svc.provider.get_stock_basic_info = AsyncMock(return_value=provider_basic_info)
    return svc


def _run(svc, batch, mapping):
    with patch("app.services.official_industry_service.fetch_industry_mapping",
               new=AsyncMock(return_value=mapping)):
        return asyncio.run(svc._process_basic_info_batch(batch, force_update=True))


def test_batch_skips_row_when_list_name_missing():
    """股票列表没给名字 → 不伪造占位名、不写库，计为错误"""
    svc = _service(None)
    stats = _run(svc, [{"code": "600519", "name": ""}], {"600519": "白酒"})

    svc.db.stock_basic_info.update_one.assert_not_awaited()
    assert stats["success_count"] == 0
    assert stats["error_count"] == 1
    assert stats["errors"][0]["context"] == "placeholder_guard"


def test_batch_skips_row_when_provider_name_is_placeholder():
    """详情接口只回占位名 → 同样不写库"""
    svc = _service({"code": "600519", "name": "股票600519", "industry": "未知"})
    stats = _run(svc, [{"code": "600519", "name": "贵州茅台"}], {})

    svc.db.stock_basic_info.update_one.assert_not_awaited()
    assert stats["error_count"] == 1
    assert stats["errors"][0]["context"] == "placeholder_guard"


def test_batch_still_writes_real_row():
    """真实数据照旧写库（守卫不得误伤）"""
    svc = _service({"code": "600519", "name": "贵州茅台", "industry": "白酒"})
    stats = _run(svc, [{"code": "600519", "name": "贵州茅台"}], {})

    assert stats["success_count"] == 1
    assert stats["error_count"] == 0
    payload = svc.db.stock_basic_info.update_one.await_args.args[1]["$set"]
    assert payload["name"] == "贵州茅台"


def test_build_basic_info_from_list_does_not_fabricate_name():
    svc = AKShareSyncService()
    doc = svc._build_basic_info_from_list("600519", "", "白酒")
    assert doc["name"] == "", "名称缺失时必须留空，不得伪造 '股票<code>'"
    assert doc["code"] == "600519"
    assert svc._build_basic_info_from_list("600519", "贵州茅台")["name"] == "贵州茅台"


if __name__ == "__main__":
    import sys

    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  ✓ {name}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  ✗ {name}: {type(e).__name__}: {e}")
    print(f"{'❌' if failed else '✅'} {len(tests) - failed}/{len(tests)} 通过")
    sys.exit(1 if failed else 0)
