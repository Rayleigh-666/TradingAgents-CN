"""
分析工作流注册表

提供统一的工作流配置管理和查询功能
"""

from typing import Dict, List, Optional, Any
from pydantic import BaseModel, Field
from app.models.analysis import AnalysisTaskType
import logging

logger = logging.getLogger(__name__)


class AnalysisWorkflowConfig(BaseModel):
    """分析工作流配置
    
    定义一个分析流程的完整配置信息
    """
    task_type: AnalysisTaskType = Field(..., description="任务类型")
    workflow_id: str = Field(..., description="工作流ID")
    name: str = Field(..., description="流程名称")
    description: str = Field(default="", description="流程描述")
    
    # 引擎配置
    default_engine: str = Field(default="workflow", description="默认引擎: workflow/legacy/llm")
    supported_engines: List[str] = Field(
        default_factory=lambda: ["workflow", "legacy", "llm"],
        description="支持的引擎列表"
    )
    
    # 参数配置
    required_params: List[str] = Field(default_factory=list, description="必需参数列表")
    optional_params: Dict[str, Any] = Field(default_factory=dict, description="可选参数及默认值")
    
    # 执行配置
    timeout: int = Field(default=300, description="超时时间（秒）")
    max_retries: int = Field(default=3, description="最大重试次数")
    
    # 偏好支持
    supports_preference: bool = Field(default=True, description="是否支持分析偏好（aggressive/neutral/conservative）")
    
    # 元数据
    version: str = Field(default="1.0.0", description="版本号")
    tags: List[str] = Field(default_factory=list, description="标签")
    is_active: bool = Field(default=True, description="是否启用")


class AnalysisWorkflowRegistry:
    """分析工作流注册表
    
    单例模式，管理所有分析流程的配置
    
    使用示例:
        # 注册流程
        AnalysisWorkflowRegistry.register(config)
        
        # 获取流程配置
        config = AnalysisWorkflowRegistry.get_config(AnalysisTaskType.STOCK_ANALYSIS)
        
        # 列出所有流程
        all_configs = AnalysisWorkflowRegistry.list_all()
    """
    
    _registry: Dict[AnalysisTaskType, AnalysisWorkflowConfig] = {}
    _initialized: bool = False
    
    @classmethod
    def register(cls, config: AnalysisWorkflowConfig) -> None:
        """注册工作流配置
        
        Args:
            config: 工作流配置对象
        """
        if config.task_type in cls._registry:
            logger.warning(f"工作流配置已存在，将被覆盖: {config.task_type}")
        
        cls._registry[config.task_type] = config
        logger.info(f"✅ 注册工作流: {config.task_type} -> {config.workflow_id}")
    
    @classmethod
    def get_config(cls, task_type: AnalysisTaskType) -> Optional[AnalysisWorkflowConfig]:
        """获取工作流配置
        
        Args:
            task_type: 任务类型（可以是枚举值或字符串）
            
        Returns:
            工作流配置对象，如果不存在则返回 None
        """
        # 如果 task_type 是字符串，尝试转换为枚举
        if isinstance(task_type, str):
            try:
                task_type = AnalysisTaskType(task_type)
            except ValueError:
                logger.warning(f"无效的任务类型字符串: {task_type}")
                return None
        
        return cls._registry.get(task_type)
    
    @classmethod
    def list_all(cls, active_only: bool = True) -> List[AnalysisWorkflowConfig]:
        """列出所有工作流配置
        
        Args:
            active_only: 是否只返回启用的流程
            
        Returns:
            工作流配置列表
        """
        configs = list(cls._registry.values())
        if active_only:
            configs = [c for c in configs if c.is_active]
        return configs
    
    @classmethod
    def list_by_tag(cls, tag: str) -> List[AnalysisWorkflowConfig]:
        """根据标签筛选工作流
        
        Args:
            tag: 标签名称
            
        Returns:
            匹配的工作流配置列表
        """
        return [c for c in cls._registry.values() if tag in c.tags and c.is_active]
    
    @classmethod
    def validate_params(cls, task_type: AnalysisTaskType, params: Dict[str, Any]) -> tuple[bool, Optional[str]]:
        """验证任务参数
        
        Args:
            task_type: 任务类型（可以是枚举值或字符串）
            params: 任务参数
            
        Returns:
            (是否有效, 错误信息)
        """
        # 如果 task_type 是字符串，尝试转换为枚举
        if isinstance(task_type, str):
            try:
                task_type = AnalysisTaskType(task_type)
            except ValueError:
                return False, f"无效的任务类型字符串: {task_type}"
        
        config = cls.get_config(task_type)
        if not config:
            return False, f"未注册的任务类型: {task_type}"
        
        # 检查必需参数
        #
        # ⚠️ 通用研究工作流（workflow_type=general）以「分析目标」自由文本驱动，不针对个股，
        #    因此 task_params 里**没有 symbol**（由 app/routers/workflows.py 的
        #    _resolve_market_and_symbol 保证：只有真正识别为股票代码时才写 symbol/stock_code）。
        #    general 复用 STOCK_ANALYSIS 载体（见 workflows.py 的
        #    WORKFLOW_TYPE_TO_TASK_TYPE），而 STOCK_ANALYSIS 的 required_params 含 symbol，
        #    若不豁免会把所有通用研究任务卡死在参数校验（报「缺少必需参数: symbol」）。
        #    豁免依据是 workflows.py 写入的 task_params["task_category"] == "general"，
        #    而不是「params 里没有 symbol」——后者会让普通股票分析漏校验。
        #    豁免范围**限定 STOCK_ANALYSIS**：ETF 等其它类型仍必须给 symbol，
        #    这样若将来有人把 ETF 流程也打上 general 标记，会立刻暴露而不是静默放过。
        #    market_type 仍然必需：通用流程也必须明确 cn/hk/us。
        required_params = list(config.required_params)
        is_general_research = (
            task_type == AnalysisTaskType.STOCK_ANALYSIS
            and str(params.get("task_category") or "").strip().lower() == "general"
        )
        if is_general_research:
            required_params = [p for p in required_params if p != "symbol"]

        for required_param in required_params:
            if required_param not in params:
                return False, f"缺少必需参数: {required_param}"

        return True, None
    
    @classmethod
    def get_default_params(cls, task_type: AnalysisTaskType) -> Dict[str, Any]:
        """获取默认参数
        
        Args:
            task_type: 任务类型（可以是枚举值或字符串）
            
        Returns:
            默认参数字典
        """
        # 如果 task_type 是字符串，尝试转换为枚举
        if isinstance(task_type, str):
            try:
                task_type = AnalysisTaskType(task_type)
            except ValueError:
                logger.warning(f"无效的任务类型字符串: {task_type}")
                return {}
        
        config = cls.get_config(task_type)
        if not config:
            return {}
        
        return config.optional_params.copy()
    
    @classmethod
    def is_initialized(cls) -> bool:
        """检查是否已初始化"""
        return cls._initialized
    
    @classmethod
    def mark_initialized(cls) -> None:
        """标记为已初始化"""
        cls._initialized = True


def initialize_builtin_workflows() -> None:
    """初始化内置工作流配置

    注册系统内置的研究流程：
    1. 股票分析（TradingAgents完整流程）
    2. 持仓研究
    3. 交易复盘
    """
    if AnalysisWorkflowRegistry.is_initialized():
        logger.info("⏭️  工作流注册表已初始化，跳过")
        return

    logger.info("🚀 开始初始化内置工作流配置...")

    # 1. 股票分析流程
    AnalysisWorkflowRegistry.register(AnalysisWorkflowConfig(
        task_type=AnalysisTaskType.STOCK_ANALYSIS,
        workflow_id="v2_stock_analysis",
        name="股票完整分析",
        description="基于v2.0 Agent架构的完整多智能体协作分析流程。包含6个专业分析师并行分析，2个研究员多情景研究，研究整合员综合评估并汇总结论，风险评估师把控风险。",
        default_engine="workflow",
        supported_engines=["workflow", "legacy"],
        required_params=["symbol", "market_type"],
        optional_params={
            "research_depth": "标准",
            "analysis_date": None,
            "selected_analysts": ["market", "fundamentals", "news", "social"],
            "include_sentiment": True,
            "include_risk": True
        },
        timeout=600,  # 10分钟
        max_retries=3,
        supports_preference=True,
        version="2.0.0",
        tags=["股票", "完整分析", "多智能体", "辩论机制"],
        is_active=True
    ))

    # 2. 持仓研究流程
    AnalysisWorkflowRegistry.register(AnalysisWorkflowConfig(
        task_type=AnalysisTaskType.POSITION_ANALYSIS,
        workflow_id="position_analysis_v2",
        name="持仓研究",
        description="单股持仓研究流程。包含持仓技术研究、持仓基本面研究、持仓风险评估与持仓结论整合。",
        default_engine="workflow",
        supported_engines=["workflow", "llm"],
        required_params=["position_id"],
        optional_params={
            "research_depth": "标准",
            "include_add_position": True,
            "target_profit_pct": 20.0,
            "total_capital": None,
            "max_position_pct": 30.0,
            "max_loss_pct": 10.0
        },
        timeout=300,  # 5分钟
        max_retries=3,
        supports_preference=True,
        version="1.0.0",
        tags=["持仓", "研究观察", "风险审阅"],
        is_active=True
    ))

    # 2.5 ETF 分析流程
    AnalysisWorkflowRegistry.register(AnalysisWorkflowConfig(
        task_type=AnalysisTaskType.ETF_ANALYSIS,
        workflow_id="v2_etf_analysis",
        name="ETF 分析",
        description="ETF 专用分析流程。包含 ETF 分析师、市场分析师、新闻分析师，无牛熊辩论。",
        default_engine="workflow",
        supported_engines=["workflow"],
        required_params=["symbol", "market_type"],
        optional_params={
            "research_depth": "标准",
            "analysis_date": None,
            "selected_analysts": ["etf", "market", "news"],
        },
        timeout=300,
        max_retries=3,
        supports_preference=True,
        version="2.0.0",
        tags=["ETF", "基金", "简化流程"],
        is_active=True
    ))

    # 3. 交易复盘流程
    AnalysisWorkflowRegistry.register(AnalysisWorkflowConfig(
        task_type=AnalysisTaskType.TRADE_REVIEW,
        workflow_id="trade_review_v2",
        name="交易复盘",
        description="交易复盘流程。包含时机复盘、仓位复盘、情绪与纪律复盘、归因复盘与复盘总结。",
        default_engine="workflow",
        supported_engines=["workflow", "llm"],
        required_params=["review_id"],
        optional_params={
            "trading_system_id": None,
            "include_plan_analysis": True
        },
        timeout=300,  # 5分钟
        max_retries=3,
        supports_preference=True,
        version="2.0.0",
        tags=["复盘", "交易计划", "情绪复盘"],
        is_active=True
    ))

    # 4. 组合健康度分析流程（预留）
    AnalysisWorkflowRegistry.register(AnalysisWorkflowConfig(
        task_type=AnalysisTaskType.PORTFOLIO_HEALTH,
        workflow_id="portfolio_health",
        name="组合健康度分析",
        description="投资组合健康度分析流程。评估组合的风险、收益、集中度等指标。",
        default_engine="llm",
        supported_engines=["llm"],
        required_params=["user_id"],
        optional_params={
            "include_paper": True,
            "research_depth": "标准"
        },
        timeout=180,  # 3分钟
        max_retries=3,
        supports_preference=True,
        version="1.0.0",
        tags=["组合", "健康度", "风险评估"],
        is_active=True
    ))

    AnalysisWorkflowRegistry.mark_initialized()

    logger.info(f"✅ 内置工作流配置初始化完成，共注册 {len(AnalysisWorkflowRegistry.list_all())} 个流程")
    for config in AnalysisWorkflowRegistry.list_all():
        logger.info(f"   - {config.task_type}: {config.name} ({config.workflow_id})")

