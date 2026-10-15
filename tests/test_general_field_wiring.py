"""
通用研究工作流「死字段收口」第 2 批：三个字段真正接通到消费方 + 零影响护栏。

背景（机制已实测确认）：4 条 ``workflow_type="general"`` 流程的动态 State 白名单
= 各 agent 的 ``AgentInput`` 声明 + ``WorkflowEngine._build_internal_state_fields``
的硬编码集；未声明的键被 LangGraph 静默丢弃。因此 ``include_margin`` /
``lookback_days`` / ``current_position_pct`` 必须显式进入该白名单。

本文件覆盖：
1. ``index_analyst_v2`` 工具集过滤（``include_margin`` 三态 + 不传 state 的原样返回）；
2. ``_build_lookback_days_directive`` / ``_build_current_position_directive``
   的「缺省零影响」纯函数契约（含 ``current_position_pct=0`` 空仓合法值）；
3. ``WorkflowEngine._build_internal_state_fields`` 白名单含三个 general 字段；
4. 回归护栏：单股/ETF 复用的 agent 在不带这些 state 键时，追加指令为空串，
   拼接前后 prompt 逐字相等、工具集逐字相等。

``scripts/`` 不是 Python 包，故流程定义用 importlib 按文件路径加载
（与 ``tests/test_index_sector_prompt_guidance.py::_load_module`` 同风格）。

跑法::

    env -u PYTHONPATH .venv/bin/python -m pytest tests/test_general_field_wiring.py -q
"""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

from core.agents.adapters import index_analyst_v2 as index_mod
from core.agents.adapters import risk_manager_v2 as risk_mod
from core.state.models import FieldType
from core.workflow.engine import WorkflowEngine


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def _load_seed_module():
    """按文件路径加载 ``scripts/seed_general_workflows.py``（scripts/ 非包）。"""
    module_path = ROOT / "scripts" / "seed_general_workflows.py"
    spec = importlib.util.spec_from_file_location("seed_general_workflows", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _index_agent():
    """构造最小 ``IndexAnalystV2`` 实例：``_get_tool_ids_for_mode`` 只依赖类属性
    ``_TOOL_IDS``，无需走 ``__init__``（避免 LLM / 工具注册表依赖）。"""
    return index_mod.IndexAnalystV2.__new__(index_mod.IndexAnalystV2)


# --------------------------------------------------------------------------- #
# 1. include_margin → 工具集过滤
# --------------------------------------------------------------------------- #
def test_tool_ids_identical_when_state_absent_or_flag_not_false():
    """不带 state / 缺键 / None / True ⇒ 工具 id 与改动前 ``_TOOL_IDS`` 完全相同。"""
    agent = _index_agent()
    baseline = index_mod.IndexAnalystV2._TOOL_IDS

    assert agent._get_tool_ids_for_mode("tushare") == baseline
    assert agent._get_tool_ids_for_mode("tushare", {}) == baseline
    assert agent._get_tool_ids_for_mode("tushare", {"include_margin": None}) == baseline
    assert agent._get_tool_ids_for_mode("tushare", {"include_margin": True}) == baseline


def test_tool_ids_object_identity_when_state_not_passed():
    """不传 state 时返回的就是类属性本身，最严格意义上的「逐字一致」。"""
    assert _index_agent()._get_tool_ids_for_mode("tushare") is index_mod.IndexAnalystV2._TOOL_IDS


def test_tool_ids_drop_margin_trading_only_when_explicitly_false():
    agent = _index_agent()
    ids = agent._get_tool_ids_for_mode("tushare", {"include_margin": False})
    assert "get_margin_trading" not in ids
    assert ids == [
        tid for tid in index_mod.IndexAnalystV2._TOOL_IDS if tid != "get_margin_trading"
    ]


def test_tool_ids_keep_margin_trading_when_enabled():
    ids = _index_agent()._get_tool_ids_for_mode("tushare", {"include_margin": True})
    assert "get_margin_trading" in ids


# --------------------------------------------------------------------------- #
# 2. lookback_days → 提示词指令（纯函数）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("value", [None, "", "   "])
def test_lookback_directive_empty_for_absent_or_blank(value):
    assert index_mod._build_lookback_days_directive(value) == ""


def test_lookback_directive_empty_when_state_key_missing():
    """state 缺键 ⇒ ``state.get("lookback_days")`` 为 None ⇒ 空串。"""
    assert index_mod._build_lookback_days_directive({}.get("lookback_days")) == ""


def test_lookback_directive_renders_value():
    text = index_mod._build_lookback_days_directive(30)
    assert text != ""
    assert "30" in text


# --------------------------------------------------------------------------- #
# 3. current_position_pct → 提示词指令（纯函数，0 为合法空仓）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("value", [None, "", "   "])
def test_position_directive_empty_for_absent_or_blank(value):
    assert risk_mod._build_current_position_directive(value) == ""


def test_position_directive_empty_when_state_key_missing():
    assert risk_mod._build_current_position_directive({}.get("current_position_pct")) == ""


def test_position_directive_zero_is_rendered():
    """空仓 0 是合法业务值，必须渲染为指令（``_NUMBER_KEEP_ZERO_FIELDS`` 的意义）。"""
    text = risk_mod._build_current_position_directive(0)
    assert text != ""
    assert "0" in text


def test_position_directive_renders_value():
    text = risk_mod._build_current_position_directive(65)
    assert text != ""
    assert "65" in text


# --------------------------------------------------------------------------- #
# 4. 动态 State 白名单
# --------------------------------------------------------------------------- #
def test_internal_state_fields_include_general_flow_keys():
    seed = _load_seed_module()
    engine = WorkflowEngine()

    emotion_fields = engine._build_internal_state_fields(seed.build_market_emotion_cycle())
    assert emotion_fields["current_position_pct"] == FieldType.NUMBER

    capital_fields = engine._build_internal_state_fields(seed.build_capital_flow_monitor())
    assert capital_fields["include_margin"] == FieldType.BOOLEAN
    assert capital_fields["lookback_days"] == FieldType.NUMBER


def test_internal_state_fields_do_not_break_existing_keys():
    """只能加键：既有组合级字段仍在白名单中。"""
    seed = _load_seed_module()
    fields = WorkflowEngine()._build_internal_state_fields(seed.build_portfolio_health_check())
    for key in ("holdings", "total_position_pct", "risk_preference", "benchmark"):
        assert key in fields


# --------------------------------------------------------------------------- #
# 5. 回归护栏：state 缺键 ⇒ 零字符追加 ⇒ prompt 逐字相等
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "workflow_file",
    ["v2_stock_analysis_workflow.py", "v2_etf_analysis_workflow.py"],
)
def test_shared_agents_are_reused_by_stock_and_etf_workflows(workflow_file):
    """护栏前提：风险/大盘分析师被单股、ETF 流程复用，故改动必须缺省零影响。"""
    source = (ROOT / "core" / "workflow" / "templates" / workflow_file).read_text(
        encoding="utf-8"
    )
    assert 'agent_id="risk_manager_v2"' in source

    stock_source = (
        ROOT / "core" / "workflow" / "templates" / "v2_stock_analysis_workflow.py"
    ).read_text(encoding="utf-8")
    assert 'agent_id="index_analyst_v2"' in stock_source


def test_absent_lookback_key_appends_zero_characters_to_index_prompt():
    baseline_prompt = "【基线】单股大盘分析师用户提示词"
    state = {"ticker": "600519", "analysis_date": "2026-01-05"}  # 无 lookback_days

    directive = index_mod._build_lookback_days_directive(state.get("lookback_days"))

    assert directive == ""
    assert baseline_prompt + directive == baseline_prompt


def test_absent_position_key_appends_zero_characters_to_risk_prompt():
    baseline_prompt = "【基线】单股风险评估师用户提示词"
    state = {"ticker": "600519", "analysis_date": "2026-01-05"}  # 无 current_position_pct

    directive = risk_mod._build_current_position_directive(
        state.get("current_position_pct")
    )

    assert directive == ""
    assert baseline_prompt + directive == baseline_prompt


def test_present_keys_do_change_prompt():
    """反向断言：显式提供时确实追加了内容（防止纯函数被写成恒空串）。"""
    assert "30" in index_mod._build_lookback_days_directive(30)
    assert "30" in risk_mod._build_current_position_directive(30)
