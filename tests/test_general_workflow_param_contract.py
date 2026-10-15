"""通用研究工作流（general）参数契约回归测试。

背景（两条曾被漏掉的真实故障）：

1. 「缺少必需参数: symbol」
   general 复用 ``AnalysisTaskType.STOCK_ANALYSIS`` 载体，而 STOCK_ANALYSIS 的
   ``required_params = ["symbol", "market_type"]``。当工作流执行入口按
   ``task_category=general`` 刻意不写 ``symbol``（自由文本不再是股票代码）后，
   ``AnalysisWorkflowRegistry.validate_params`` 会把**所有**通用研究任务卡死在
   参数校验，任务秒失败并报「任务参数无效: 缺少必需参数: symbol」。

2. 「Missing required parameters: ticker」
   ``symbol`` 不再写之后，``unified_analysis_engine._execute_via_workflow`` 的
   symbol/code/stock_code → ticker 映射也拿不到值，agent 以 ticker 为必需入参
   （见各 adapter 的 ``AgentInput(name="ticker", ...)``），结果全部维度秒失败、
   任务「假成功」（status=completed 但 reports 全是降级文案）。

   ⇒ ``apply_general_ticker_fallback`` **只补 workflow_inputs["ticker"]**，
     绝不回写 ``symbol`` / ``stock_code``（否则会污染市场识别/报告标题/导出文件名）。

3. custom_params 无过滤合并（P1-1，契约的**入口侧**）
   ``app/routers/workflows.py`` 在写完归一化 ``market_type`` 与「真识别才写」的
   ``symbol/stock_code``，以及服务端 ``task_category`` 标记之后，曾把 ``custom_params``
   的原始值原样 ``update`` 回去：客户端传 ``market_type="a_share"`` 会覆盖 cn/hk/us、
   传 ``symbol="自由文本"`` 会绕过守卫、非 general 流程传 ``task_category="general"``
   会命中 registry 的 symbol 豁免判据。⇒ 入口侧新增 ``_sanitize_custom_params``，
   丢弃 None 值与四个保留键。

4. 「交易日」字段形同虚设
   ``_execute_via_workflow`` 原**无条件**把 ``analysis_date`` 补成 ``datetime.now()``，
   而 ``core/workflow/engine.py`` 的映射优先级是 ``analysis_date or trade_date``，
   于是流程 1 用户选的 ``trade_date`` 永远被覆盖。⇒ ``apply_general_trade_date_fallback``
   在兜底成「今天」之前，把用户显式提供的 ``trade_date`` 写入 ``analysis_date``
   （仅 general 生效，且只写 analysis_date、不回写 trade_date）。
"""
from __future__ import annotations

import pytest

from app.models.analysis import AnalysisTaskType
from app.routers.workflows import (
    _RESERVED_TASK_PARAM_KEYS,
    _apply_lookback_days,
    _normalize_market_type,
    _sanitize_custom_params,
)
from app.services.workflow_registry import (
    AnalysisWorkflowRegistry,
    initialize_builtin_workflows,
)
from app.services.unified_analysis_engine import (
    GENERAL_TICKER_SENTINEL,
    apply_general_ticker_fallback,
    apply_general_trade_date_fallback,
)


@pytest.fixture(scope="module", autouse=True)
def _init_registry():
    # 与运行时一致（app/main.py:476、app/worker/analysis_worker.py:502 都调它）
    initialize_builtin_workflows()
    yield


# ==================== validate_params：symbol 的必需性 ====================


def test_general_task_without_symbol_passes_validation():
    """general 任务缺 symbol 必须通过校验（否则通用研究全线不可用）。"""
    ok, err = AnalysisWorkflowRegistry.validate_params(
        AnalysisTaskType.STOCK_ANALYSIS,
        {
            "task_category": "general",
            "market_type": "cn",
            "analysis_target": "A股市场当日盘面复盘",
        },
    )
    assert ok, f"general 任务不应被 symbol 卡住，实际错误: {err}"


def test_general_task_without_symbol_passes_with_field_defaults():
    """复刻 workflows.py 实际写入的 task_params 形态（含 input_config 默认值）。"""
    ok, err = AnalysisWorkflowRegistry.validate_params(
        AnalysisTaskType.STOCK_ANALYSIS,
        {
            "market_type": "cn",
            "analysis_date": "2026-10-15",
            "research_depth": "快速",
            "lookback_days": 30,
            "max_debate_rounds": 3,
            "analysis_target": "A股市场当日盘面复盘",
            "task_category": "general",
        },
    )
    assert ok, err


def test_general_task_still_requires_market_type():
    """放宽的只有 symbol；market_type 仍然必需（必须明确 cn/hk/us）。"""
    ok, err = AnalysisWorkflowRegistry.validate_params(
        AnalysisTaskType.STOCK_ANALYSIS,
        {"task_category": "general", "analysis_target": "A股市场当日盘面复盘"},
    )
    assert not ok
    assert "market_type" in (err or "")


def test_plain_stock_analysis_still_requires_symbol():
    """反向对照：普通股票分析缺 symbol 必须仍然失败（不能顺带放松）。"""
    ok, err = AnalysisWorkflowRegistry.validate_params(
        AnalysisTaskType.STOCK_ANALYSIS,
        {"market_type": "cn"},
    )
    assert not ok
    assert "symbol" in (err or "")


def test_task_category_is_case_and_space_insensitive():
    """task_category 判定不应被大小写/空白影响。"""
    for value in ("general", "GENERAL", " General ", "general\t"):
        ok, err = AnalysisWorkflowRegistry.validate_params(
            AnalysisTaskType.STOCK_ANALYSIS,
            {"task_category": value, "market_type": "cn"},
        )
        assert ok, f"task_category={value!r} 应被识别为通用研究，实际错误: {err}"


def test_other_task_types_unaffected():
    """ETF / 持仓 / 复盘 的必需参数不受影响。"""
    ok, err = AnalysisWorkflowRegistry.validate_params(
        AnalysisTaskType.ETF_ANALYSIS, {"task_category": "general", "market_type": "cn"}
    )
    assert not ok, "ETF 分析即便打了 general 标记，也不应豁免 symbol"
    assert "symbol" in (err or "")

    ok, err = AnalysisWorkflowRegistry.validate_params(
        AnalysisTaskType.POSITION_ANALYSIS, {"market_type": "cn"}
    )
    assert not ok
    assert "position_id" in (err or "")


# ==================== apply_general_ticker_fallback ====================


def test_fallback_uses_analysis_target():
    wi = {"task_category": "general", "analysis_target": "A股市场当日盘面复盘", "market_type": "cn"}
    assert apply_general_ticker_fallback(wi) == "A股市场当日盘面复盘"
    assert wi["ticker"] == "A股市场当日盘面复盘"


def test_fallback_keeps_existing_ticker():
    wi = {"task_category": "general", "ticker": "600519", "analysis_target": "不该覆盖"}
    assert apply_general_ticker_fallback(wi) is None
    assert wi["ticker"] == "600519"


def test_fallback_alternate_goal_keys():
    assert apply_general_ticker_fallback(
        {"task_category": "general", "analysis_goal": "新能源汽车板块"}
    ) == "新能源汽车板块"
    assert apply_general_ticker_fallback(
        {"task_category": "general", "company_of_interest": "贵州茅台"}
    ) == "贵州茅台"


def test_fallback_sentinel_when_general_has_no_target():
    wi = {"task_category": "general", "market_type": "cn"}
    assert apply_general_ticker_fallback(wi) == GENERAL_TICKER_SENTINEL


def test_fallback_noop_for_non_general():
    """非通用任务不应被塞入哨兵 ticker（否则会掩盖真正的缺参错误）。"""
    wi = {"market_type": "cn"}
    assert apply_general_ticker_fallback(wi) is None
    assert "ticker" not in wi


def test_fallback_noop_for_non_general_even_with_target():
    """非通用任务即便带了 analysis_target 也不补齐 —— 兜底整体受 general 门控。

    反向对照：这是本轮「无条件补齐」→「仅 general 补齐」的行为变更点。
    非 general 缺 ticker 时必须让真实缺参错误暴露，不能被自由文本静默填上。
    """
    wi = {"task_category": "stock_analysis", "analysis_target": "贵州茅台", "market_type": "cn"}
    assert apply_general_ticker_fallback(wi) is None
    assert "ticker" not in wi


def test_fallback_never_touches_symbol_or_stock_code():
    """核心约束：兜底只写 ticker，绝不回写 symbol / stock_code（防污染）。"""
    wi = {"task_category": "general", "analysis_target": "A股市场当日盘面复盘"}
    apply_general_ticker_fallback(wi)
    assert "symbol" not in wi
    assert "stock_code" not in wi


def test_fallback_ignores_blank_target():
    """空白目标应按「没有目标」处理，走哨兵而不是写入空白串。"""
    wi = {"task_category": "general", "analysis_target": "   "}
    assert apply_general_ticker_fallback(wi) == GENERAL_TICKER_SENTINEL
    assert wi["ticker"] == GENERAL_TICKER_SENTINEL


def test_fallback_strips_whitespace():
    wi = {"task_category": "general", "analysis_target": "  盘面复盘  "}
    assert apply_general_ticker_fallback(wi) == "盘面复盘"


# ==================== apply_general_trade_date_fallback ====================


def test_trade_date_fallback_writes_analysis_date_for_general():
    """general + 无 analysis_date + 有 trade_date → 写入 analysis_date。"""
    wi = {"task_category": "general", "trade_date": "2026-10-15"}
    assert apply_general_trade_date_fallback(wi) == "2026-10-15"
    assert wi["analysis_date"] == "2026-10-15"


def test_trade_date_fallback_noop_for_non_general():
    """非 general 任务即便带 trade_date 也不写（避免污染单股/ETF 的取证日期）。"""
    wi = {"task_category": "stock_analysis", "trade_date": "2026-10-15"}
    assert apply_general_trade_date_fallback(wi) is None
    assert "analysis_date" not in wi


def test_trade_date_fallback_noop_without_task_category():
    wi = {"trade_date": "2026-10-15"}
    assert apply_general_trade_date_fallback(wi) is None
    assert "analysis_date" not in wi


def test_trade_date_fallback_does_not_override_existing_analysis_date():
    """已有 analysis_date 时不得被 trade_date 覆盖（显式给定优先）。"""
    wi = {"task_category": "general", "analysis_date": "2026-01-01", "trade_date": "2026-10-15"}
    assert apply_general_trade_date_fallback(wi) is None
    assert wi["analysis_date"] == "2026-01-01"


def test_trade_date_fallback_noop_when_trade_date_missing_or_blank():
    """trade_date 缺省 / None / 空串 / 空白串 一律视为未提供。"""
    for wi in (
        {"task_category": "general"},
        {"task_category": "general", "trade_date": None},
        {"task_category": "general", "trade_date": ""},
        {"task_category": "general", "trade_date": "   "},
    ):
        assert apply_general_trade_date_fallback(wi) is None, wi
        assert "analysis_date" not in wi, wi


def test_trade_date_fallback_gate_is_case_and_space_insensitive():
    for value in ("general", "GENERAL", " General ", "general\t"):
        wi = {"task_category": value, "trade_date": "2026-10-15"}
        assert apply_general_trade_date_fallback(wi) == "2026-10-15", value
        assert wi["analysis_date"] == "2026-10-15"


def test_trade_date_fallback_strips_value():
    wi = {"task_category": "general", "trade_date": "  2026-10-15  "}
    assert apply_general_trade_date_fallback(wi) == "2026-10-15"
    assert wi["analysis_date"] == "2026-10-15"


def test_trade_date_fallback_never_rewrites_trade_date():
    """核心约束：兜底只写 analysis_date，绝不回写 trade_date。"""
    wi = {"task_category": "general", "trade_date": "2026-10-15"}
    apply_general_trade_date_fallback(wi)
    assert wi["trade_date"] == "2026-10-15"


# ==================== P1-1：custom_params 无过滤合并（入口侧契约） ====================


def test_sanitize_custom_params_drops_none_and_reserved_keys():
    """``_sanitize_custom_params`` 必须丢弃 None 值与四个保留键，其余照常保留。

    保留键若被放行会架空契约：
      - market_type 回盖 → 归一化结果被原值覆盖，落库非法市场；
      - symbol/stock_code 回盖 → 自由文本污染下游；
      - task_category 回盖 → 非 general 流程也能命中 registry 的 symbol 豁免。
    """
    clean, dropped = _sanitize_custom_params(
        {
            "market_type": "a_share",
            "symbol": "自由文本",
            "stock_code": "x",
            "task_category": "general",
            "watch_sectors": "银行",
        }
    )
    assert clean == {"watch_sectors": "银行"}
    assert dropped == sorted(_RESERVED_TASK_PARAM_KEYS)
    assert dropped == ["market_type", "stock_code", "symbol", "task_category"]


def test_sanitize_custom_params_drops_none_values():
    """值为 None 的普通键也必须丢弃（key 存在但值为 None 会覆盖上游默认值）。"""
    clean, dropped = _sanitize_custom_params(
        {"watch_sectors": "银行", "watch_topics": None, "market_type": None}
    )
    assert clean == {"watch_sectors": "银行"}
    # market_type 值为 None → 先被 None 规则丢；不进保留键名单
    assert dropped == []


def test_sanitize_custom_params_handles_empty_input():
    assert _sanitize_custom_params(None) == ({}, [])
    assert _sanitize_custom_params({}) == ({}, [])


def test_normalize_market_type_uses_shared_alias_table():
    """``_normalize_market_type`` 复用唯一别名表，未知写法返回 None（而不是兜底 cn）。"""
    assert _normalize_market_type("a_share") == "cn"
    assert _normalize_market_type("HONGKONG") == "hk"
    assert _normalize_market_type("xxx") is None
    assert _normalize_market_type(None) is None


def test_injected_task_category_cannot_bypass_symbol_check():
    """客户端注入 custom_params.task_category 不得架空 symbol 校验。

    非 general 流程（服务端不会写 task_category=general），即便客户端在 custom_params
    里塞了 ``task_category="general"``，也会被 ``_sanitize_custom_params`` 拦下；
    结果 task_params 既无 symbol 又无 general 标记 → validate_params 仍然失败。
    """
    clean, _ = _sanitize_custom_params({"task_category": "general", "market_type": "cn"})
    assert "task_category" not in clean

    # 复刻 workflows.py 的合并顺序：服务端 market_type 先写，clean 再 update。
    task_params = {"market_type": "cn"}
    task_params.update(clean)  # task_category 已被拦下，不会进来

    ok, err = AnalysisWorkflowRegistry.validate_params(
        AnalysisTaskType.STOCK_ANALYSIS, task_params
    )
    assert not ok, "被拦截的 task_category 不应让普通股票分析豁免 symbol"
    assert "symbol" in (err or "")


# ==================== P2：lookback_days 不得用固定默认值注入 ====================


def test_apply_lookback_days_none_writes_nothing():
    """未显式提供（None）→ **不得**写入 lookback_days 键。

    这是本缺陷的核心：请求模型默认 30 会让每一次执行都无条件注入该参数，
    而它已升格为动态 State 字段、驱动 index_analyst_v2 的「统一回溯天数」指令，
    从而改变未声明该字段的流程（流程 1/2）与单股分析的数据窗口。
    """
    task_params = {"market_type": "cn"}
    assert _apply_lookback_days(task_params, None) is False
    assert "lookback_days" not in task_params


def test_apply_lookback_days_explicit_writes_value():
    """显式提供 45 → 原样写入。"""
    task_params = {"market_type": "cn"}
    assert _apply_lookback_days(task_params, 45) is True
    assert task_params["lookback_days"] == 45


def test_apply_lookback_days_does_not_touch_other_keys():
    task_params = {"market_type": "cn", "research_depth": "标准"}
    _apply_lookback_days(task_params, 45)
    assert task_params == {"market_type": "cn", "research_depth": "标准", "lookback_days": 45}


def test_request_model_lookback_days_defaults_to_none():
    """请求模型层：不传 lookback_days → ``data.lookback_days is None``（而非 30）。"""
    from app.routers.workflows import WorkflowExecuteRequest

    data = WorkflowExecuteRequest(ticker="600519")
    assert data.lookback_days is None


def test_request_model_lookback_days_explicit_value_kept():
    """请求模型层：显式传 45 → 等于 45；约束 ge/le 仍然生效。"""
    from app.routers.workflows import WorkflowExecuteRequest

    data = WorkflowExecuteRequest(ticker="600519", lookback_days=45)
    assert data.lookback_days == 45

    with pytest.raises(Exception):
        WorkflowExecuteRequest(ticker="600519", lookback_days=0)
    with pytest.raises(Exception):
        WorkflowExecuteRequest(ticker="600519", lookback_days=366)


