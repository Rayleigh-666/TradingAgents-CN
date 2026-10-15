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


def test_input_config_has_required_analysis_target(workflows):
    for wf in workflows:
        fields = wf.input_config.fields
        by_name = {f.name: f for f in fields}
        assert "analysis_target" in by_name, f"{wf.id} 缺少 analysis_target"
        assert by_name["analysis_target"].required is True, f"{wf.id} analysis_target 应为必填"
        assert by_name["analysis_target"].type == "textarea"
        # 模型字段命名必须精确
        for f in fields:
            if f.name in ("quick_analysis_model", "deep_analysis_model"):
                assert f.type == "select"
        # 字段 type 必须是受支持的类型
        allowed = {"string", "textarea", "number", "boolean", "date", "select", "multiselect"}
        for f in fields:
            assert f.type in allowed, f"{wf.id} 字段 {f.name} 非法 type={f.type}"


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
