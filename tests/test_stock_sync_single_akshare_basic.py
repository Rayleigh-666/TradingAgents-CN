"""
单股 AKShare 基础信息同步：写库守卫

覆盖两件事：
1. 正常成功路径照旧写库，且系统行业映射优先；
2. **占位/降级结果不落库** —— 降级结果（`sync_status == "degraded"`，name 必为
   `股票<code>`）除占位字段外不含任何真实信息。旧实现会把它 upsert 进
   `stock_basic_info`，于是留下 `{code:'003669', name:'股票003669'}` 这类脏行，
   让不存在的 A 股代码「查得到基础信息」。

注：`sync_single_stock` 端点已改为「提交后台任务」的薄封装，同步逻辑在
`run_single_stock_sync`，故用例直接调用后者（旧用例仍按已移除的同步端点签名调用）。
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from app.routers.stock_sync import SingleStockSyncRequest, run_single_stock_sync


def _request(symbol: str) -> SingleStockSyncRequest:
    return SingleStockSyncRequest(
        symbol=symbol,
        sync_realtime=False,
        sync_historical=False,
        sync_financial=False,
        sync_basic=True,
        data_source="akshare",
    )


def _fake_service(basic_info):
    provider = Mock()
    provider.get_stock_basic_info_for_single_sync = AsyncMock(return_value=basic_info)
    provider.get_stock_basic_info = AsyncMock(return_value=None)
    return SimpleNamespace(provider=provider)


def _run(symbol: str, basic_info, mapped_industry):
    fake_db = Mock()
    fake_db.stock_basic_info.find_one = AsyncMock(return_value=None)
    fake_db.stock_basic_info.update_one = AsyncMock(return_value=Mock())

    with patch("app.routers.stock_sync.get_mongo_db", return_value=fake_db), \
         patch("app.routers.stock_sync.get_akshare_sync_service",
               new=AsyncMock(return_value=_fake_service(basic_info))), \
         patch("app.services.official_industry_service.fetch_industry_mapping",
               new=AsyncMock(return_value=mapped_industry)):
        result = asyncio.run(run_single_stock_sync(_request(symbol)))
    return result, fake_db


def test_success_path_writes_basic_info_and_prefers_system_industry():
    result, fake_db = _run(
        "300750",
        {
            "code": "300750",
            "name": "宁德时代",
            "industry": "电气机械和器材制造业",
            "market": "创业板",
            "sse": "深圳证券交易所",
            "list_date": "2018-06-11",
            "sync_status": "success",
        },
        {"300750": "电力设备"},
    )

    assert result["overall_success"] is True
    assert result["basic_sync"]["success"] is True

    update_payload = fake_db.stock_basic_info.update_one.await_args.args[1]["$set"]
    assert update_payload["industry"] == "电力设备"
    assert update_payload["industry_source"] == "system_industry_mapping"
    assert update_payload["name"] == "宁德时代"


def test_degraded_result_is_not_written():
    """降级结果（name 为占位 '股票003669'）必须跳过写库，避免产生脏行"""
    result, fake_db = _run(
        "003669",
        {
            "code": "003669",
            "name": "股票003669",
            "area": "未知",
            "industry": "未知",
            "market": "主板",
            "sse": "深圳证券交易所",
            "list_date": "",
            "full_symbol": "003669.SZ",
            "data_source": "akshare",
            "sync_status": "degraded",
            "detail_fetch_error": "AKShare 单股基础信息接口未返回有效数据",
            "detail_fetch_symbol": "SZ003669",
        },
        {},
    )

    assert result["basic_sync"]["success"] is False
    assert result["basic_sync"]["degraded"] is True
    assert result["basic_sync"]["skipped_write"] is True
    assert result["overall_success"] is False
    # 降级结果不得写库
    fake_db.stock_basic_info.update_one.assert_not_awaited()


def test_placeholder_name_without_degraded_flag_is_not_written():
    """即使没有 degraded 标记，只要 name 仍是占位形态也不得写库"""
    result, fake_db = _run(
        "003669",
        {
            "code": "003669",
            "name": "股票003669",
            "industry": "未知",
            "sync_status": "success",
        },
        {},
    )

    assert result["basic_sync"]["success"] is False
    assert result["basic_sync"]["skipped_write"] is True
    fake_db.stock_basic_info.update_one.assert_not_awaited()


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
