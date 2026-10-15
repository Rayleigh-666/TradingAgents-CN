"""引擎入参兜底的回归测试（离线，直接调用生产代码）。

覆盖四类近期修的真实故障：

1. 通用研究工作流（``task_category == "general"``）缺分析目标文本时的 ``ticker``
   兜底 —— 兜底**只对 general 生效**，且**绝不**回写 ``symbol`` / ``stock_code``。
2. 哨兵 ``GENERAL_TICKER_SENTINEL`` 是**引擎内部占位串、不是证券代码**：
   它必须经 ``app.core.market_codes.resolve_instrument`` 解析为 ``("", "")``，
   任何把它直接喂给 ``guess_market`` 的调用方都会因「含字母 → us」而误判成美股。
3. ``simple_analysis_service`` 的 ``selected_analysts`` 兜底 —— 原写法
   ``X if request.parameters else [...]`` 在 ``parameters`` 存在但属性为 None 时会把
   ``None`` 灌下去（``enumerate(None)`` → TypeError），须走统一的纯函数归一化。
4. ``agent_utils`` 的 Reddit 可用性判定 —— 数据源只读本地缓存，不读 ``REDDIT_*``
   凭证，故门槛应改为「本地缓存是否存在」，且需抽成可离线测试的纯函数。
"""
from __future__ import annotations

import types
from pathlib import Path

from app.core.market_codes import resolve_instrument
from app.services.simple_analysis_service import _resolve_selected_analysts
from app.services.unified_analysis_engine import (
    GENERAL_TICKER_SENTINEL,
    apply_declared_lookback_default,
    apply_general_ticker_fallback,
)
from tradingagents.agents.utils.agent_utils import check_local_reddit_cache


# ==================== 1. ticker 兜底仅对 general 生效 ====================


def test_fallback_noop_without_task_category():
    """非 general（完全没有 task_category）不补齐：返回 None 且不写 ticker。"""
    wi = {"market_type": "cn", "analysis_target": "A股市场当日盘面复盘"}
    assert apply_general_ticker_fallback(wi) is None
    assert "ticker" not in wi


def test_fallback_noop_for_other_task_category():
    """显式非 general 分类同样不补齐（真实缺参错误须照常暴露）。"""
    wi = {"task_category": "stock_analysis", "analysis_target": "贵州茅台"}
    assert apply_general_ticker_fallback(wi) is None
    assert "ticker" not in wi


def test_fallback_writes_analysis_target_for_general():
    """general + analysis_target → 把分析目标写进 ticker。"""
    wi = {"task_category": "general", "analysis_target": "A股市场当日盘面复盘"}
    assert apply_general_ticker_fallback(wi) == "A股市场当日盘面复盘"
    assert wi["ticker"] == "A股市场当日盘面复盘"


def test_fallback_sentinel_when_general_has_no_target():
    """general 且无任何目标文本 → 写入哨兵。"""
    wi = {"task_category": "general", "market_type": "cn"}
    assert apply_general_ticker_fallback(wi) == GENERAL_TICKER_SENTINEL
    assert wi["ticker"] == GENERAL_TICKER_SENTINEL


def test_fallback_never_writes_symbol_or_stock_code():
    """核心约束：兜底只写 ticker，绝不回写 symbol / stock_code（防污染）。"""
    wi = {"task_category": "general", "analysis_target": "A股市场当日盘面复盘"}
    apply_general_ticker_fallback(wi)
    assert "symbol" not in wi
    assert "stock_code" not in wi


# ==================== 2. 哨兵不是证券代码（核心价值） ====================


def test_sentinel_is_internal_placeholder_not_an_instrument():
    """哨兵经唯一推荐入口解析必须是 ``("", "")``（调用方按其按「无标的」处理）。

    这是本类改动的核心：无论哨兵值怎么换，只要能经 ``resolve_instrument`` 得到
    ``("", "")``，下游就不会把它误当证券代码去查行情/查库。
    """
    assert resolve_instrument(GENERAL_TICKER_SENTINEL) == ("", "")


def test_sentinel_value_is_not_a_plain_ticker():
    """哨兵值本身刻意用非代码形态（避免被朴素地当成裸 ticker）。"""
    assert GENERAL_TICKER_SENTINEL == "__GENERAL__"
    assert resolve_instrument(GENERAL_TICKER_SENTINEL) == ("", "")


# ==================== 3. selected_analysts 兜底 ====================


def _params(selected_analysts):
    return types.SimpleNamespace(selected_analysts=selected_analysts)


def test_selected_analysts_none_parameters():
    """parameters 整体为 None → 默认两组。"""
    assert _resolve_selected_analysts(None) == ["market", "fundamentals"]


def test_selected_analysts_attribute_none():
    """parameters 存在但 selected_analysts 为 None → 默认两组（原 bug 点）。"""
    assert _resolve_selected_analysts(_params(None)) == ["market", "fundamentals"]


def test_selected_analysts_empty_list():
    """空列表视为「未选择」→ 默认两组（与同文件既有 ``or [...]`` 口径一致）。"""
    assert _resolve_selected_analysts(_params([])) == ["market", "fundamentals"]


def test_selected_analysts_explicit_list_is_preserved():
    """显式非空列表原样保留。"""
    assert _resolve_selected_analysts(_params(["market"])) == ["market"]


def test_simple_analysis_service_no_longer_uses_fragile_ternary():
    """源码守卫：两处调用点都不再出现 ``if request.parameters else ["market"`` 形态。

    提醒 lead：``tests/test_status_analysts_none.py`` 有 TRAP_FILES 源码守卫机制，
    可把 ``app/services/simple_analysis_service.py`` 补进其 TRAP_FILES（该文件由他人维护）。
    """
    source = (
        Path(__file__).resolve().parents[1]
        / "app" / "services" / "simple_analysis_service.py"
    ).read_text(encoding="utf-8")
    assert 'if request.parameters else ["market"' not in source
    # 两处调用点都改用统一纯函数（避免再次漂移）
    assert source.count("_resolve_selected_analysts(request.parameters)") == 2


# ==================== 4. Reddit 可用性判定（本地缓存） ====================


def test_reddit_unavailable_when_cache_dir_missing(tmp_path):
    """缓存目录不存在 → 不可用，并给出原因。"""
    missing = tmp_path / "not_exist"
    available, reason = check_local_reddit_cache(data_dir=str(missing))
    assert available is False
    assert reason  # 必须带原因，供下游输出显式降级说明


def test_reddit_unavailable_when_no_jsonl(tmp_path):
    """目录存在但没有 .jsonl → 不可用。"""
    data_dir = tmp_path / "data"
    (data_dir / "reddit_data" / "global_news").mkdir(parents=True)
    available, reason = check_local_reddit_cache(data_dir=str(data_dir))
    assert available is False
    assert "reddit_data" in reason


def test_reddit_available_when_jsonl_present(tmp_path):
    """存在 .jsonl 缓存 → 可用（与 REDDIT_* 凭证无关）。"""
    data_dir = tmp_path / "data"
    category_dir = data_dir / "reddit_data" / "global_news"
    category_dir.mkdir(parents=True)
    (category_dir / "posts.jsonl").write_text("{}\n", encoding="utf-8")
    assert check_local_reddit_cache(data_dir=str(data_dir)) == (True, "")


def test_reddit_unavailable_when_data_dir_empty():
    """数据目录为空串 → 不可用（推导失败按不可用处理）。"""
    available, reason = check_local_reddit_cache(data_dir="")
    assert available is False
    assert reason


# ==================== 5. lookback_days 默认值只给「声明该字段」的流程 ====================


def test_lookback_default_written_when_field_declared():
    """流程声明了 lookback_days 字段（如流程 3）→ 补齐默认值 30。"""
    wi = {"task_category": "general"}
    assert apply_declared_lookback_default(wi, {"lookback_days", "analysis_target"}) == 30
    assert wi["lookback_days"] == 30


def test_lookback_default_skipped_when_field_not_declared():
    """流程未声明该字段（如流程 1/2）→ 绝不注入默认值（否则会触发统一回溯指令）。"""
    wi = {"task_category": "general", "analysis_target": "A股市场当日盘面复盘"}
    assert apply_declared_lookback_default(wi, {"analysis_target", "market_type"}) is None
    assert "lookback_days" not in wi


def test_lookback_default_skipped_when_declared_set_empty():
    """读库失败 → 空集合 / None → 不写（宁可缺失，也不误注入）。"""
    wi = {"task_category": "general"}
    assert apply_declared_lookback_default(wi, set()) is None
    assert apply_declared_lookback_default(wi, None) is None
    assert "lookback_days" not in wi


def test_lookback_default_respects_existing_value():
    """已显式提供的 lookback_days 不被覆盖（含合法 0，走 ``is None`` 判空）。"""
    for existing in (5, 0, 20):
        wi = {"task_category": "general", "lookback_days": existing}
        assert apply_declared_lookback_default(wi, {"lookback_days"}) is None
        assert wi["lookback_days"] == existing


def test_lookback_default_not_gated_by_task_category_for_declared_flow():
    """判定已从「task_category == general」收敛为「字段已声明」——凡声明即补。"""
    wi = {"task_category": "stock_analysis"}  # 非 general，但只要声明了字段就补
    assert apply_declared_lookback_default(wi, {"lookback_days"}) == 30
    assert wi["lookback_days"] == 30
