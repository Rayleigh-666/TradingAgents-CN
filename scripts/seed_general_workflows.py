#!/usr/bin/env python
"""
向 MongoDB 写入 4 条 ``workflow_type="general"`` 的自定义分析工作流（「通用研究」页）。

用法::

    env -u PYTHONPATH .venv/bin/python scripts/seed_general_workflows.py --dry-run
    env -u PYTHONPATH .venv/bin/python scripts/seed_general_workflows.py            # 正式写入
    env -u PYTHONPATH .venv/bin/python scripts/seed_general_workflows.py --verify   # 写入后复查

设计要点（务必与代码保持一致）::

* 文档顶层 ``_id`` 必须写成与 ``id`` 完全相同的字符串。
  原因：``app/routers/workflows.py`` 用 ``db.workflows.find_one({"_id": workflow_id})``
  读取 ``workflow_type`` / ``input_config``，系统预置文档的 ``_id`` 是 ObjectId 会必然
  miss，从而把 workflow_type 退化为 ``stock_analysis``（触发 ticker 必填 400）。
* 节点 ``type`` 只允许使用 ``core/workflow/models.py`` 中 ``NodeType`` 的枚举值。
* 必须显式落库 ``workflow_type="general"``。
* DEBATE 节点 ``config`` 含 ``participants``（参与者 **节点 id** 列表）与 ``rounds``。
* MERGE 节点 ``config`` 使用 ``{"strategy": "wait_all"}`` 启用屏障。
* ``input_config.fields`` 每一项都包含
  ``name/label/type/required/default/description/placeholder/options/option_labels/min/max/group/order``。

关于 ``data_preparer_v2``：本仓库 ``agent_configs`` 集合中不存在该 agent_id（未启用），
按既定规则 **所有流程均不包含 data_preparer 节点**，边直接由 ``start`` 指向后续并行节点。
（注：该 agent 在 ``core/agents/config.py`` 的 ``BUILTIN_AGENTS`` 与注册表中确实存在，
如需启用可后续在 agent_configs 中补录并加回 prep 节点。）
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from typing import Any, Dict, List

# 允许直接 `python scripts/seed_general_workflows.py` 运行（把仓库根加入 sys.path）
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from pymongo import MongoClient  # noqa: E402

from core.workflow.models import (  # noqa: E402
    EdgeDefinition,
    EdgeType,
    InputFieldConfig,
    NodeDefinition,
    NodeType,
    Position,
    WorkflowDefinition,
    WorkflowInputConfig,
)

MONGO_URI = "mongodb://localhost:27017"
DB_NAME = "tradingagents"
COLLECTION_NAME = "workflows"


# --------------------------------------------------------------------------- #
# 便捷构造
# --------------------------------------------------------------------------- #
def _node(
    node_id: str,
    node_type: NodeType,
    *,
    label: str = "",
    agent_id: str | None = None,
    x: float = 0,
    y: float = 0,
    config: Dict[str, Any] | None = None,
) -> NodeDefinition:
    return NodeDefinition(
        id=node_id,
        type=node_type,
        agent_id=agent_id,
        label=label,
        position=Position(x=x, y=y),
        config=config or {},
    )


def _edge(source: str, target: str, *, label: str | None = None, animated: bool = False) -> EdgeDefinition:
    eid = f"e_{source}__{target}"
    return EdgeDefinition(
        id=eid,
        source=source,
        target=target,
        type=EdgeType.NORMAL,
        label=label,
        animated=animated,
    )


def _field(
    name: str,
    label: str,
    ftype: str,
    *,
    required: bool = False,
    default: Any = None,
    description: str = "",
    placeholder: str = "",
    options: List[Any] | None = None,
    option_labels: List[str] | None = None,
    group: str = "基本参数",
    order: int = 0,
    min: float | None = None,
    max: float | None = None,
) -> InputFieldConfig:
    return InputFieldConfig(
        name=name,
        label=label,
        type=ftype,
        required=required,
        default=default,
        description=description,
        placeholder=placeholder,
        options=options,
        option_labels=option_labels,
        min=min,
        max=max,
        group=group,
        order=order,
    )


_RESEARCH_DEPTH_OPTIONS = ["快速", "基础", "标准", "深度", "全面"]

# 目标市场：用于数据源/工具的市场路由（cn/hk/us），与分析目标自由文本相互独立
_MARKET_TYPE_OPTIONS = ["cn", "hk", "us"]
_MARKET_TYPE_LABELS = ["中国A股", "中国香港", "美股"]


def _market_type_field(order: int) -> InputFieldConfig:
    return _field(
        "market_type",
        "目标市场",
        "select",
        default="cn",
        description="用于数据源市场路由（不影响上面的分析目标文本）",
        placeholder="选择目标市场",
        options=list(_MARKET_TYPE_OPTIONS),
        option_labels=list(_MARKET_TYPE_LABELS),
        group="基本参数",
        order=order,
    )


def _research_depth_field(order: int) -> InputFieldConfig:
    return _field(
        "research_depth",
        "分析深度",
        "select",
        default="标准",
        description="分析深度，影响辩论轮数等",
        placeholder="选择分析深度",
        options=list(_RESEARCH_DEPTH_OPTIONS),
        group="模型配置",
        order=order,
    )


def _model_fields(start_order: int) -> List[InputFieldConfig]:
    return [
        _field(
            "quick_analysis_model",
            "快速分析模型",
            "select",
            description="快速分析使用的 LLM 模型",
            placeholder="选择模型",
            group="模型配置",
            order=start_order,
        ),
        _field(
            "deep_analysis_model",
            "深度分析模型",
            "select",
            description="深度分析使用的 LLM 模型",
            placeholder="选择模型",
            group="模型配置",
            order=start_order + 1,
        ),
    ]


# --------------------------------------------------------------------------- #
# 流程 1：每日市场复盘 · 盘面日报
# --------------------------------------------------------------------------- #
def build_daily_market_review() -> WorkflowDefinition:
    nodes = [
        _node("start", NodeType.START, label="开始", x=600, y=0),
        _node("p_market", NodeType.PARALLEL, label="并行分析", x=600, y=120,
              config={"description": "大盘/板块/新闻/社媒四位分析师并行工作"}),
        _node("a_index", NodeType.ANALYST, agent_id="index_analyst_v2",
              label="大盘分析师", x=120, y=260, config={"focus": "指数走势、量能、市场环境"}),
        _node("a_sector", NodeType.ANALYST, agent_id="sector_analyst_v2",
              label="板块分析师", x=440, y=260, config={"focus": "板块轮动、风格切换"}),
        _node("a_news", NodeType.ANALYST, agent_id="news_analyst_v2",
              label="新闻分析师", x=760, y=260, config={"focus": "财经新闻、政策与事件"}),
        _node("a_social", NodeType.ANALYST, agent_id="social_analyst_v2",
              label="社媒情绪分析师", x=1080, y=260, config={"focus": "市场情绪、舆情热度"}),
        _node("m_market", NodeType.MERGE, label="汇总分析", x=600, y=400,
              config={"strategy": "wait_all"}),
        _node("mgr", NodeType.MANAGER, agent_id="research_manager_v2",
              label="研究经理", x=600, y=520),
        _node("trader", NodeType.TRADER, agent_id="trader_v2",
              label="研究整合员", x=600, y=640),
        _node("end", NodeType.END, label="结束", x=600, y=760),
    ]
    edges = [
        _edge("start", "p_market"),
        _edge("p_market", "a_index"),
        _edge("p_market", "a_sector"),
        _edge("p_market", "a_news"),
        _edge("p_market", "a_social"),
        _edge("a_index", "m_market"),
        _edge("a_sector", "m_market"),
        _edge("a_news", "m_market"),
        _edge("a_social", "m_market"),
        _edge("m_market", "mgr"),
        _edge("mgr", "trader"),
        _edge("trader", "end"),
    ]
    fields = [
        _field(
            "analysis_target", "复盘对象", "textarea",
            required=True, default="A股市场当日盘面复盘",
            description="本次盘面复盘的分析对象",
            placeholder="如：A股市场当日盘面复盘",
            group="基本参数", order=1,
        ),
        _market_type_field(2),
        _field("trade_date", "交易日", "date", group="基本参数", order=3),
        _field("watch_sectors", "关注板块", "multiselect",
               options=[], group="基本参数", order=4),
        _field("watchlist", "自选股", "textarea", group="基本参数", order=5),
        _research_depth_field(6),
        *_model_fields(7),
    ]
    return WorkflowDefinition(
        id="general_daily_market_review",
        name="每日市场复盘 · 盘面日报",
        description="收盘后一屏看清指数+量能+情绪+板块+风格，输出结构化解盘与次日关注信号。",
        version="1.0.0",
        tags=["general", "盘面", "日报"],
        workflow_type="general",
        is_template=False,
        nodes=nodes,
        edges=edges,
        input_config=WorkflowInputConfig(fields=fields),
    )


# --------------------------------------------------------------------------- #
# 流程 2：市场情绪周期定位 · 仓位择时
# --------------------------------------------------------------------------- #
def build_market_emotion_cycle() -> WorkflowDefinition:
    nodes = [
        _node("start", NodeType.START, label="开始", x=600, y=0),
        _node("p_emo", NodeType.PARALLEL, label="并行分析", x=600, y=120,
              config={"description": "市场/社媒/板块/大盘四位分析师并行工作"}),
        _node("a_market", NodeType.ANALYST, agent_id="market_analyst_v2",
              label="市场分析师", x=120, y=260, config={"focus": "量价与技术面情绪"}),
        _node("a_social", NodeType.ANALYST, agent_id="social_analyst_v2",
              label="社媒情绪分析师", x=440, y=260, config={"focus": "舆情冷热与情绪强度"}),
        _node("a_sector", NodeType.ANALYST, agent_id="sector_analyst_v2",
              label="板块分析师", x=760, y=260, config={"focus": "赚钱效应与板块持续性"}),
        _node("a_index", NodeType.ANALYST, agent_id="index_analyst_v2",
              label="大盘分析师", x=1080, y=260, config={"focus": "指数位置与市场宽度"}),
        _node("m_emo", NodeType.MERGE, label="汇总分析", x=600, y=400,
              config={"strategy": "wait_all"}),
        _node("d_risk", NodeType.DEBATE, label="风险情景辩论", x=600, y=520,
              config={
                  "participants": ["r_risky", "r_safe", "r_neutral"],
                  "rounds": "auto",
                  "description": "三方就当前情绪阶段与仓位区间展开多轮讨论",
              }),
        _node("r_risky", NodeType.RISK, agent_id="risky_analyst_v2",
              label="高弹性情景分析师", x=280, y=640),
        _node("r_safe", NodeType.RISK, agent_id="safe_analyst_v2",
              label="防御情景分析师", x=600, y=640),
        _node("r_neutral", NodeType.RISK, agent_id="neutral_analyst_v2",
              label="基准情景分析师", x=920, y=640),
        _node("mgr", NodeType.MANAGER, agent_id="risk_manager_v2",
              label="风险评估师", x=600, y=780),
        _node("trader", NodeType.TRADER, agent_id="trader_v2",
              label="研究整合员", x=600, y=900),
        _node("end", NodeType.END, label="结束", x=600, y=1020),
    ]
    edges = [
        _edge("start", "p_emo"),
        _edge("p_emo", "a_market"),
        _edge("p_emo", "a_social"),
        _edge("p_emo", "a_sector"),
        _edge("p_emo", "a_index"),
        _edge("a_market", "m_emo"),
        _edge("a_social", "m_emo"),
        _edge("a_sector", "m_emo"),
        _edge("a_index", "m_emo"),
        _edge("m_emo", "d_risk"),
        _edge("d_risk", "r_risky", label="发起", animated=True),
        _edge("d_risk", "r_safe", label="发起", animated=True),
        _edge("d_risk", "r_neutral", label="发起", animated=True),
        _edge("r_risky", "d_risk", label="回应", animated=True),
        _edge("r_safe", "d_risk", label="回应", animated=True),
        _edge("r_neutral", "d_risk", label="回应", animated=True),
        _edge("d_risk", "mgr"),
        _edge("mgr", "trader"),
        _edge("trader", "end"),
    ]
    fields = [
        _field(
            "analysis_target", "分析目标", "textarea",
            required=True, default="当前A股市场情绪周期定位与仓位建议",
            description="本次情绪周期定位的分析目标",
            placeholder="如：当前A股市场情绪周期定位与仓位建议",
            group="基本参数", order=1,
        ),
        _market_type_field(2),
        _field("window_days", "观察窗口(日)", "number",
               default=20, group="基本参数", order=3),
        _field("current_position_pct", "当前仓位(%)", "number",
               group="基本参数", order=4),
        _field("breadth_scope", "市场宽度口径", "select",
               default="全市场（含ST/次新）",
               options=["全市场（含ST/次新）", "剔除ST/次新"],
               group="基本参数", order=5),
        _research_depth_field(6),
        *_model_fields(7),
    ]
    return WorkflowDefinition(
        id="general_market_emotion_cycle",
        name="市场情绪周期定位 · 仓位择时",
        description="判断当前情绪处于冰点/修复/主升/退潮哪一阶段，给出对应仓位区间与阶段切换信号。",
        version="1.0.0",
        tags=["general", "情绪周期", "仓位"],
        workflow_type="general",
        is_template=False,
        nodes=nodes,
        edges=edges,
        input_config=WorkflowInputConfig(fields=fields),
    )


# --------------------------------------------------------------------------- #
# 流程 3：北向 · 两融增量资金面监控
# --------------------------------------------------------------------------- #
def build_capital_flow_monitor() -> WorkflowDefinition:
    nodes = [
        _node("start", NodeType.START, label="开始", x=600, y=0),
        _node("p_cap", NodeType.PARALLEL, label="并行分析", x=600, y=120,
              config={"description": "大盘/板块/市场三位分析师并行工作"}),
        _node("a_index", NodeType.ANALYST, agent_id="index_analyst_v2",
              label="大盘资金面分析师", x=280, y=260,
              config={"focus": "指数与市场整体资金面"}),
        _node("a_sector", NodeType.ANALYST, agent_id="sector_analyst_v2",
              label="板块资金流向分析师", x=600, y=260,
              config={"focus": "行业与板块资金流向"}),
        _node("a_market", NodeType.ANALYST, agent_id="market_analyst_v2",
              label="量价与杠杆资金分析师", x=920, y=260,
              config={"focus": "成交量价与两融杠杆资金"}),
        _node("m_cap", NodeType.MERGE, label="汇总分析", x=600, y=400,
              config={"strategy": "wait_all"}),
        _node("mgr", NodeType.MANAGER, agent_id="research_manager_v2",
              label="资金面研判经理", x=600, y=520),
        _node("trader", NodeType.TRADER, agent_id="trader_v2",
              label="研究整合员", x=600, y=640),
        _node("end", NodeType.END, label="结束", x=600, y=760),
    ]
    edges = [
        _edge("start", "p_cap"),
        _edge("p_cap", "a_index"),
        _edge("p_cap", "a_sector"),
        _edge("p_cap", "a_market"),
        _edge("a_index", "m_cap"),
        _edge("a_sector", "m_cap"),
        _edge("a_market", "m_cap"),
        _edge("m_cap", "mgr"),
        _edge("mgr", "trader"),
        _edge("trader", "end"),
    ]
    fields = [
        _field(
            "analysis_target", "分析目标", "textarea",
            required=True, default="当前A股增量资金面（北向+两融）监控",
            description="本次资金面监控的分析目标",
            placeholder="如：当前A股增量资金面（北向+两融）监控",
            group="基本参数", order=1,
        ),
        _market_type_field(2),
        _field("lookback_days", "回溯天数", "number",
               default=20, group="基本参数", order=3),
        _field("focus_sectors", "关注行业", "multiselect",
               options=[], group="基本参数", order=4),
        _field("include_margin", "纳入两融", "boolean",
               default=True, group="基本参数", order=5),
        _research_depth_field(6),
        *_model_fields(7),
    ]
    return WorkflowDefinition(
        id="general_capital_flow_monitor",
        name="北向 · 两融增量资金面监控",
        description="判断当前 A 股是增量资金入场还是存量博弈，输出风格与仓位倾向（北向仅日频、两融 T+1）。",
        version="1.0.0",
        tags=["general", "资金面", "北向", "两融"],
        workflow_type="general",
        is_template=False,
        nodes=nodes,
        edges=edges,
        input_config=WorkflowInputConfig(fields=fields),
    )


# --------------------------------------------------------------------------- #
# 流程 4：持仓组合健康度体检
# --------------------------------------------------------------------------- #
def build_portfolio_health_check() -> WorkflowDefinition:
    """组合级健康度体检：单一 ``portfolio_analyst_v2`` 自包含分析师直接完成。

    该适配器自行读取 ``state['holdings']`` 等输入，用真实本地行情数据计算组合
    集中度（HHI/topN）、相关性、有效前沿等指标，产出 ``portfolio_report``。
    它不使用 LangChain 工具、不依赖 ``_{type}_messages`` 消息槽，因此不需要
    上游并行单股分析师，也不需要下游固定报告名契约的 risk_manager / trader
    （它们读的是 ``market_report`` 等单股报告名，与组合报告对不上）。
    """
    nodes = [
        _node("start", NodeType.START, label="开始", x=600, y=0),
        _node("a_pf", NodeType.ANALYST, agent_id="portfolio_analyst_v2",
              label="组合分析师", x=600, y=260,
              config={"focus": "组合集中度、相关性、风险指标与数据完整性"}),
        _node("end", NodeType.END, label="结束", x=600, y=520),
    ]
    edges = [
        _edge("start", "a_pf"),
        _edge("a_pf", "end"),
    ]
    fields = [
        _field(
            "analysis_target", "分析目标", "textarea",
            required=True, default="以下持仓组合的健康度体检",
            description="本次组合体检的分析目标",
            placeholder="如：以下持仓组合的健康度体检",
            group="基本参数", order=1,
        ),
        _market_type_field(2),
        _field(
            "holdings", "持仓数据", "textarea",
            required=True,
            description="每行一只：代码,权重%（或市值）,成本价",
            placeholder="600519,30,1500",
            group="基本参数", order=3,
        ),
        _field("total_position_pct", "当前总仓位(%)", "number",
               group="基本参数", order=4),
        _field("risk_preference", "风险偏好", "select",
               default="平衡", options=["保守", "平衡", "激进"],
               group="基本参数", order=5),
        _field("benchmark", "业绩基准", "select",
               default="沪深300", options=["沪深300", "中证500", "中证1000"],
               group="基本参数", order=6),
        _research_depth_field(7),
        *_model_fields(8),
    ]
    return WorkflowDefinition(
        id="general_portfolio_health_check",
        name="持仓组合健康度体检",
        description="一次评估多标的组合的集中度、组合风险指标（有效前沿）与逐只数据完整性，输出组合健康度体检结论。",
        version="1.0.0",
        tags=["general", "持仓", "组合", "风险体检"],
        workflow_type="general",
        is_template=False,
        nodes=nodes,
        edges=edges,
        input_config=WorkflowInputConfig(fields=fields),
    )


# --------------------------------------------------------------------------- #
# 汇总
# --------------------------------------------------------------------------- #
# 每条流程的落库补充字段（WorkflowDefinition 模型本身不含 is_default / is_system）
WORKFLOW_EXTRAS: Dict[str, Dict[str, Any]] = {
    "general_daily_market_review": {"is_default": True, "is_system": False},
    "general_market_emotion_cycle": {"is_default": False, "is_system": False},
    "general_capital_flow_monitor": {"is_default": False, "is_system": False},
    "general_portfolio_health_check": {"is_default": False, "is_system": False},
}


def build_workflow_definitions() -> List[WorkflowDefinition]:
    """返回 4 条流程的 ``WorkflowDefinition`` 对象（不落库）。"""
    return [
        build_daily_market_review(),
        build_market_emotion_cycle(),
        build_capital_flow_monitor(),
        build_portfolio_health_check(),
    ]


def build_workflow_documents() -> List[Dict[str, Any]]:
    """返回 4 条流程最终写入 MongoDB 的文档 dict（不落库）。"""
    docs: List[Dict[str, Any]] = []
    for wf in build_workflow_definitions():
        doc = wf.to_dict()
        doc["_id"] = wf.id  # 关键：_id 必须等于 id 字符串
        doc["workflow_type"] = "general"
        doc.update(WORKFLOW_EXTRAS.get(wf.id, {}))
        docs.append(doc)
    return docs


# 模块级定义，便于测试直接 import
WORKFLOWS: List[WorkflowDefinition] = build_workflow_definitions()
WORKFLOW_DOCUMENTS: List[Dict[str, Any]] = build_workflow_documents()


# --------------------------------------------------------------------------- #
# 落库
# --------------------------------------------------------------------------- #
def _upsert_documents(collection, documents: List[Dict[str, Any]]) -> List[str]:
    """幂等 upsert；返回每条文档的处理动作（created / updated）。"""
    actions: List[str] = []
    for doc in documents:
        wf_id = doc["id"]
        existing = collection.find_one({"_id": wf_id}, {"created_at": 1, "updated_at": 1})
        if existing:
            # 保持时间戳不变以保证重复执行结果完全一致
            doc["created_at"] = existing.get("created_at")
            doc["updated_at"] = existing.get("updated_at")
            actions.append("updated")
        else:
            now = datetime.now().isoformat()
            doc["created_at"] = now
            doc["updated_at"] = now
            actions.append("created")
        collection.replace_one({"_id": wf_id}, doc, upsert=True)
    return actions


def _summarize(documents: List[Dict[str, Any]]) -> str:
    lines = []
    for doc in documents:
        node_types = {}
        for n in doc.get("nodes", []):
            node_types[n["type"]] = node_types.get(n["type"], 0) + 1
        field_names = [f["name"] for f in doc.get("input_config", {}).get("fields", [])]
        lines.append(
            f"  - id={doc['id']!r} _id={doc['_id']!r} type={doc['workflow_type']!r} "
            f"is_default={doc.get('is_default')} nodes={len(doc.get('nodes', []))} "
            f"edges={len(doc.get('edges', []))} node_types={node_types}\n"
            f"    fields={field_names}"
        )
    return "\n".join(lines)


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="写入 4 条 general 通用研究工作流")
    parser.add_argument("--dry-run", action="store_true", help="只打印，不写库")
    parser.add_argument("--verify", action="store_true", help="写入后复查并打印摘要")
    args = parser.parse_args(argv)

    documents = build_workflow_documents()

    if args.dry_run:
        print(f"[dry-run] 将写入 {len(documents)} 条流程（未连接数据库）：")
        print(_summarize(documents))
        return 0

    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    collection = client[DB_NAME][COLLECTION_NAME]

    actions = _upsert_documents(collection, documents)
    print(f"[write] 已处理 {len(documents)} 条流程：")
    for doc, action in zip(documents, actions):
        print(f"  - {doc['id']} -> {action}")

    if args.verify:
        print("\n[verify] 复查数据库中这 4 条文档：")
        verified = []
        for doc in documents:
            row = collection.find_one({"_id": doc["id"]})
            if row is None:
                print(f"  - {doc['id']}: ✗ 未找到")
                continue
            ok_id = row.get("_id") == doc["id"]
            ok_type = row.get("workflow_type") == "general"
            field_names = [f["name"] for f in row.get("input_config", {}).get("fields", [])]
            has_target = "analysis_target" in field_names
            print(
                f"  - {doc['id']}: _id_is_str={isinstance(row['_id'], str)} "
                f"_id==id={ok_id} workflow_type={row.get('workflow_type')!r} "
                f"fields={len(field_names)} 含analysis_target={has_target}"
            )
            verified.append(ok_id and ok_type and has_target)
        general_count = collection.count_documents({"workflow_type": "general"})
        print(f"[verify] workflow_type=='general' 文档数 = {general_count}")
        if not all(verified):
            print("[verify] ✗ 存在未通过的检查项")
            return 1
        print("[verify] ✓ 全部检查通过")

    client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
