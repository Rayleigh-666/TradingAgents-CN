"""
获取市场快照工具

获取交易期间的市场数据快照，用于复盘分析
"""

import logging
from typing import Dict, Any, Optional, Annotated
from core.tools.base import register_tool

logger = logging.getLogger(__name__)


@register_tool(
    tool_id="get_market_snapshot_for_review",
    name="获取市场快照",
    description="【暂不可用】交易复盘市场快照：底层统一K线数据接口尚未实现，当前恒返回 success=False；请勿依赖本工具获取K线/技术指标数据",
    category="trade_review",
    is_online=True,
    data_source_handling="self_contained",
    capability_tags=["market_snapshot", "snapshot", "kline_data", "market_data", "trade_review", "trading", "review_analysis", "复盘"],
    tool_role_hint="specialized",
    output_shape="structured_json",
    preferred_for=["trade_review_analysis", "market_context_retrieval", "kline_data_fetch", "trade_period_review"],
)
def get_market_snapshot_for_review(
    code: Annotated[str, "股票代码"],
    market: Annotated[str, "市场: 'CN'(A股) 或 'US'(美股)"],
    start_date: Annotated[Optional[str], "开始日期 YYYY-MM-DD"] = None,
    end_date: Annotated[Optional[str], "结束日期 YYYY-MM-DD"] = None
) -> Dict[str, Any]:
    """
    获取市场快照

    Args:
        code: 股票代码
        market: 市场类型
        start_date: 开始日期
        end_date: 结束日期

    Returns:
        包含K线数据的字典
    """
    # 历史遗留说明（stale import 清理）：
    # 原实现依赖 `from tradingagents.dataflows.interface import get_stock_data_unified`，
    # 但该符号在整个代码库中从未存在（interface 仅提供同步的 get_china_stock_data_unified /
    # get_hk_stock_data_unified / get_stock_data_by_market，均返回格式化字符串而非 K 线列表，
    # 且不存在对应的异步统一接口）。这使本工具历史上恒返回 success=False，属静默失效的死路。
    # 由于没有可直接替换、且能被验证的统一异步 K 线接口，此处移除该死 import 与死 K 线分支；
    # 如需该能力需另行实现数据获取（可参考 app/services/trade_review_service.py 的 _fetch_kline_unified）。
    return {
        "success": False,
        "error": "市场快照暂不可用：底层统一K线数据接口尚未实现",
        "data": None,
    }

