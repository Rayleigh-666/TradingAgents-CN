"""4 条 general 通用研究工作流种子脚本的纯结构测试。

不依赖网络与数据库：直接 import 种子脚本模块里的定义对象做断言。
运行::

    env -u PYTHONPATH .venv/bin/python -m pytest tests/test_general_workflow_seed.py -q
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from core.workflow.models import NodeType
from core.workflow.validator import WorkflowValidator

REPO_ROOT = Path(__file__).resolve().parents[1]
SEED_PATH = REPO_ROOT / "scripts" / "seed_general_workflows.py"

AGENT_NODE_TYPES = {
    NodeType.ANALYST.value,
    NodeType.RESEARCHER.value,
    NodeType.TRADER.value,
    NodeType.RISK.value,
    NodeType.MANAGER.value,
    NodeType.POST_PROCESSOR.value,
}
VALID_NODE_TYPES = {t.value for t in NodeType}


def _load_seed_module():
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    spec = importlib.util.spec_from_file_location("seed_general_workflows", SEED_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def seed():
    return _load_seed_module()


@pytest.fixture(scope="module")
def workflows(seed):
    return seed.WORKFLOWS


@pytest.fixture(scope="module")
def documents(seed):
    return seed.WORKFLOW_DOCUMENTS


def test_has_four_general_workflows(workflows):
    assert len(workflows) == 4
    ids = {wf.id for wf in workflows}
    assert ids == {
        "general_daily_market_review",
        "general_market_emotion_cycle",
        "general_capital_flow_monitor",
        "general_portfolio_health_check",
    }
    for wf in workflows:
        assert wf.id
        assert wf.name
        assert wf.nodes
        assert wf.edges
        assert wf.workflow_type == "general"


def test_documents_have_string_id_equal_to_id(documents):
    for doc in documents:
        assert isinstance(doc["_id"], str)
        assert doc["_id"] == doc["id"]
        assert doc["workflow_type"] == "general"


def test_exactly_one_start_and_at_least_one_end(workflows):
    for wf in workflows:
        starts = [n for n in wf.nodes if n.type == NodeType.START.value]
        ends = [n for n in wf.nodes if n.type == NodeType.END.value]
        assert len(starts) == 1, f"{wf.id} start 节点数 = {len(starts)}"
        assert len(ends) >= 1, f"{wf.id} 缺少 end 节点"


def test_all_node_types_valid_and_agent_nodes_have_agent_id(workflows):
    for wf in workflows:
        for node in wf.nodes:
            assert node.type in VALID_NODE_TYPES, f"{wf.id}:{node.id} 非法 type={node.type}"
            if node.type in AGENT_NODE_TYPES:
                assert node.agent_id, f"{wf.id}:{node.id} ({node.type}) 缺少 agent_id"


def test_all_edges_reference_existing_nodes(workflows):
    for wf in workflows:
        node_ids = {n.id for n in wf.nodes}
        for edge in wf.edges:
            assert edge.source in node_ids, f"{wf.id} 边源不存在: {edge.source}"
            assert edge.target in node_ids, f"{wf.id} 边目标不存在: {edge.target}"


def test_debate_participants_exist_and_rounds_present(workflows):
    for wf in workflows:
        node_ids = {n.id for n in wf.nodes}
        debate_nodes = [n for n in wf.nodes if n.type == NodeType.DEBATE.value]
        for debate in debate_nodes:
            participants = debate.config.get("participants")
            assert participants, f"{wf.id}:{debate.id} 缺少 participants"
            for pid in participants:
                assert pid in node_ids, f"{wf.id}:{debate.id} 参与者不存在: {pid}"
            # participants 必须是节点 id（而非 agent_id）
            agent_ids = {n.agent_id for n in wf.nodes if n.agent_id}
            for pid in participants:
                assert pid not in agent_ids, f"{wf.id}:{debate.id} participants 疑似误用 agent_id: {pid}"
            assert debate.config.get("rounds") is not None, f"{wf.id}:{debate.id} 缺少 rounds"


def test_merge_nodes_use_wait_all(workflows):
    for wf in workflows:
        for node in wf.nodes:
            if node.type == NodeType.MERGE.value:
                assert node.config.get("strategy") == "wait_all", (
                    f"{wf.id}:{node.id} merge 未设置 wait_all 屏障"
                )


def test_capital_flow_monitor_drops_single_stock_market_node(workflows):
    """流程 3（北向 · 两融增量资金面监控）不得包含单股量价技术面节点。

    ``market_analyst_v2`` 是单股量价技术面模板（只绑 ``get_stock_market_data_unified``，
    prompt 全文无「北向/两融/杠杆」）。该流程的分析目标是自由文本
    （如「当前A股增量资金面（北向+两融）监控」），工具会把它当股票代码去查、
    失败后回落美股数据源，最终产出 3117 字符的「数据缺失说明书」，纯噪音。
    因此流程 3 只保留 ``a_index``（北向/两融/市场宽度）与 ``a_sector``（板块资金流）
    两条腿。
    """
    wf = next(w for w in workflows if w.id == "general_capital_flow_monitor")

    node_ids = {n.id for n in wf.nodes}
    assert "a_market" not in node_ids, "流程 3 不应再包含 a_market 节点"
    assert all(n.agent_id != "market_analyst_v2" for n in wf.nodes), (
        "流程 3 不应再引用 market_analyst_v2（单股量价技术面模板）"
    )

    # 结构计数（去掉 a_market 后的实际值：8 节点 / 8 边）
    assert len(wf.nodes) == 8, f"流程 3 节点数应为 8，实际 {len(wf.nodes)}"
    assert len(wf.edges) == 8, f"流程 3 边数应为 8，实际 {len(wf.edges)}"

    # 边必须成对消失且无孤边
    for edge in wf.edges:
        assert edge.source in node_ids, f"流程 3 边源不存在: {edge.source}"
        assert edge.target in node_ids, f"流程 3 边目标不存在: {edge.target}"


def test_market_emotion_cycle_has_research_manager_between_merge_and_debate(workflows):
    """流程 2 必须含且仅含一个 research_manager_v2 节点，且位于 m_emo 与 d_risk 之间。

    修复前流程 2 的 DAG 缺 ``research_manager_v2``：State 里永远没有
    ``investment_plan``，而 ``trader_v2`` 的入参含 ``investment_plan``，导致
    ``trader_investment_plan`` 每次退化为 54 字符错误桩
    ``{'error': 'Missing investment_plan', 'success': False}``。
    补节点后接续为：分析 → 研究经理 → 风险辩论 → 风控经理 → trader。
    """
    wf = next(w for w in workflows if w.id == "general_market_emotion_cycle")

    rm_nodes = [n for n in wf.nodes if n.agent_id == "research_manager_v2"]
    assert len(rm_nodes) == 1, (
        f"流程 2 应含且仅含 1 个 research_manager_v2 节点，实际 {len(rm_nodes)}"
    )
    rm_id = rm_nodes[0].id
    assert rm_id != "mgr", "新节点 id 不得与既有的 mgr(risk_manager_v2) 冲突"

    edge_pairs = {(e.source, e.target) for e in wf.edges}
    assert ("m_emo", rm_id) in edge_pairs, f"缺少边 m_emo → {rm_id}"
    assert (rm_id, "d_risk") in edge_pairs, f"缺少边 {rm_id} → d_risk"

    # 结构计数（补研究经理节点后：15 节点 / 20 边）
    assert len(wf.nodes) == 15, f"流程 2 节点数应为 15，实际 {len(wf.nodes)}"
    assert len(wf.edges) == 20, f"流程 2 边数应为 20，实际 {len(wf.edges)}"

    # 无孤边：边两端节点必须存在
    node_ids = {n.id for n in wf.nodes}
    for edge in wf.edges:
        assert edge.source in node_ids, f"流程 2 边源不存在: {edge.source}"
        assert edge.target in node_ids, f"流程 2 边目标不存在: {edge.target}"

    # 无孤立节点：除 start/end 外每个节点都有入边与出边
    sources = {e.source for e in wf.edges}
    targets = {e.target for e in wf.edges}
    starts = {n.id for n in wf.nodes if n.type == NodeType.START.value}
    ends = {n.id for n in wf.nodes if n.type == NodeType.END.value}
    for nid in node_ids - starts:
        assert nid in targets, f"流程 2 节点 {nid} 无入边（孤立）"
    for nid in node_ids - ends:
        assert nid in sources, f"流程 2 节点 {nid} 无出边（孤立）"

    # 风险辩论语义不变：d_risk 仍指向三个 r_* 节点，participants 仍为节点 id
    d_risk = next(n for n in wf.nodes if n.id == "d_risk")
    assert d_risk.config.get("participants") == ["r_risky", "r_safe", "r_neutral"]
    for pid in ("r_risky", "r_safe", "r_neutral"):
        assert ("d_risk", pid) in edge_pairs, f"缺少风险辩论边 d_risk → {pid}"


def test_input_config_analysis_target_shape(workflows):
    """每条流程都保留 analysis_target（textarea），并做字段 type 的通用校验。"""
    for wf in workflows:
        fields = wf.input_config.fields
        by_name = {f.name: f for f in fields}
        assert "analysis_target" in by_name, f"{wf.id} 缺少 analysis_target"
        assert by_name["analysis_target"].type == "textarea"
        # 模型字段命名必须精确
        for f in fields:
            if f.name in ("quick_analysis_model", "deep_analysis_model"):
                assert f.type == "select"
        # 字段 type 必须是受支持的类型
        allowed = {"string", "textarea", "number", "boolean", "date", "select", "multiselect"}
        for f in fields:
            assert f.type in allowed, f"{wf.id} 字段 {f.name} 非法 type={f.type}"


def test_analysis_target_requiredness_by_workflow(workflows):
    """analysis_target 必填性按流程区分：流程 1/2/3 必填，流程 4 非必填。

    流程 4（持仓组合体检）的 ``portfolio_analyst_v2`` 只读 ``holdings``，从不读
    ``ticker``；必填拦截只发生在前端 ``frontend/src/views/Analysis/GeneralAnalysis.vue``
    的 ``field.required`` 上，后端无校验。故流程 4 的 analysis_target 只是一个
    「分析目标」展示位，收紧为非必填，消除无消费方的必填假象。
    """
    by_wf = {wf.id: {f.name: f for f in wf.input_config.fields} for wf in workflows}

    for wf_id in (
        "general_daily_market_review",
        "general_market_emotion_cycle",
        "general_capital_flow_monitor",
    ):
        assert by_wf[wf_id]["analysis_target"].required is True, (
            f"{wf_id} analysis_target 应仍为必填"
        )

    assert by_wf["general_portfolio_health_check"]["analysis_target"].required is False, (
        "流程 4 analysis_target 应为非必填（无消费方，portfolio_analyst_v2 只读 holdings）"
    )


def test_dead_fields_removed(workflows):
    """3 个无消费方字段已删除：流程1 watchlist、流程2 window_days/breadth_scope。

    - ``watchlist``（流程1 自选股）：无任何 agent 读取，纯展示噪音。
    - ``window_days`` / ``breadth_scope``（流程2）：无消费方。
    - ``current_position_pct`` **保留**（另一批会把它接通为动态 State 字段）。
    """
    names = {wf.id: {f.name for f in wf.input_config.fields} for wf in workflows}

    assert "watchlist" not in names["general_daily_market_review"]
    assert "window_days" not in names["general_market_emotion_cycle"]
    assert "breadth_scope" not in names["general_market_emotion_cycle"]
    assert "current_position_pct" in names["general_market_emotion_cycle"]


def test_market_type_locked_to_cn(workflows):
    """4 条流程的 market_type 只提供 cn（不存在 hk/us 的市场级数据源）。"""
    for wf in workflows:
        f = next(x for x in wf.input_config.fields if x.name == "market_type")
        assert f.type == "select", f"{wf.id} market_type 应为 select"
        assert f.options == ["cn"], f"{wf.id} market_type options={f.options}"
        assert f.option_labels == ["中国A股"], f"{wf.id} option_labels={f.option_labels}"
        assert f.default == "cn", f"{wf.id} market_type default={f.default}"


def test_field_order_is_gapless_from_one(workflows):
    """每条流程的字段 order 必须从 1 起连续无空洞（删除字段后必须重排）。"""
    for wf in workflows:
        orders = [f.order for f in wf.input_config.fields]
        assert orders, f"{wf.id} 无字段"
        assert orders == list(range(1, len(orders) + 1)), (
            f"{wf.id} order 非 1..N 连续序列，实际={orders}"
        )


def test_input_maxlength_is_field_config_driven(workflows):
    """textarea 的 maxlength 由字段配置驱动。

    holdings（持仓数据，每行一只标的）需放宽到 4000，避免较大组合被截断；
    其余字段一律不配置（``None``）——前端 GeneralAnalysis.vue 以
    ``field.maxlength ?? 500`` 渲染，只有保持 None 才能维持默认 500 的零行为变化。
    """
    by_wf = {wf.id: {f.name: f for f in wf.input_config.fields} for wf in workflows}

    holdings = by_wf["general_portfolio_health_check"]["holdings"]
    assert holdings.type == "textarea"
    assert holdings.maxlength == 4000

    # 除 holdings 外，所有字段都不应显式配置 maxlength
    for wf in workflows:
        for f in wf.input_config.fields:
            if wf.id == "general_portfolio_health_check" and f.name == "holdings":
                continue
            assert f.maxlength is None, (
                f"{wf.id} 字段 {f.name} 不应设置 maxlength，当前={f.maxlength}"
            )

    # 显式抽查流程 1 的 analysis_target 未配置
    assert by_wf["general_daily_market_review"]["analysis_target"].maxlength is None


def test_validator_passes(workflows):
    validator = WorkflowValidator()
    for wf in workflows:
        result = validator.validate_for_execute(wf)
        assert result.is_valid, (
            f"{wf.id} 校验失败: " + "; ".join(str(e) for e in result.errors)
        )


def test_all_agent_ids_resolvable(workflows):
    """回归守卫：4 条 general 流程引用的每个 agent_id 都必须可解析。

    该测试对应 builder.py 的运行时行为：``WorkflowBuilder`` 只有在
    ``registry.is_registered(agent_id)`` 或 ``_has_agent_config_in_db(agent_id)``
    成立时才会真正创建智能体，否则回退为只输出「未实现」占位的
    ``placeholder_node``（builder.py:1709 / 1819）。因此任何引用了既未注册、
    DB 中也没有合规配置的 agent_id 的节点，运行时都会静默降级——用本测试把它
    提前暴露出来。

    首个历史上踩到的坑：``general_portfolio_health_check`` 的 ``a_val`` 节点曾
    写死 ``valuation_analyst_v2``（两个通道都不存在），导致「持仓体检」缺整个估值
    维度。该流程随后已重构为单一 ``portfolio_analyst_v2`` 组合分析师节点，不再包含
    单股 ``a_val`` 节点。

    需要 MongoDB 才能校验 DB 通道；连接约定参照仓库既有做法
    （``tests/acceptance/conftest.py`` 对后端探活失败即 ``pytest.skip``），
    此处对 Mongo 做一次带超时的探活，不可用则跳过，可用则必须真正断言通过。
    """
    # 触发 adapters 包的注册副作用，确保代码注册表已填充
    import core.agents.adapters  # noqa: F401
    from core.agents.registry import get_registry

    registry = get_registry()

    # 收集所有 (workflow_id, node_id, agent_id) 三元组
    triples = [
        (wf.id, node.id, node.agent_id)
        for wf in workflows
        for node in wf.nodes
        if node.agent_id
    ]
    assert triples, "未从 4 条流程中收集到任何 agent_id，测试前提不成立"

    # MongoDB 探活（带超时）；不可用则跳过 DB 通道校验
    try:
        from pymongo import MongoClient
    except ImportError:  # pragma: no cover - 环境缺少 pymongo
        pytest.skip("pymongo 不可用，跳过 agent 可解析性校验")

    client = MongoClient("mongodb://localhost:27017", serverSelectionTimeoutMS=3000)
    try:
        client.admin.command("ping")
    except Exception:  # pragma: no cover - 本机无 mongod
        client.close()
        pytest.skip("MongoDB 不可用，跳过 agent 可解析性校验")

    try:
        agent_configs = client["tradingagents"]["agent_configs"]
        # 与 builder.py:1836-1843 的门槛完全一致
        db_query = {
            "enabled": True,
            "$or": [
                {"metadata.source": {"$ne": "agent_workshop"}},
                {
                    "metadata.source": "agent_workshop",
                    "version_status": "active",
                    "runtime_status": "active",
                },
            ],
        }

        unresolved = []
        for wf_id, node_id, aid in triples:
            if registry.is_registered(aid):
                continue  # (a) 代码注册表命中
            doc = agent_configs.find_one({"agent_id": aid, **db_query}, {"_id": 1})
            if doc is None:  # (b) DB 门槛未命中
                unresolved.append((wf_id, node_id, aid))

        assert not unresolved, (
            "以下工作流节点引用了不可解析的 agent_id（既未在代码注册表注册，"
            "agent_configs 中也没有满足 builder 门槛的配置），运行时会退化为"
            "「智能体未实现」占位节点：\n"
            + "\n".join(f"  - workflow={w} node={n} agent_id={a}" for w, n, a in unresolved)
        )
    finally:
        client.close()
