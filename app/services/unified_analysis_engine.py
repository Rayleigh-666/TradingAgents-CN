"""
统一分析引擎

提供统一的任务执行接口，支持多种执行引擎：
- workflow: 工作流引擎（推荐）
- legacy: 旧引擎 TradingAgentsGraph
- llm: 直接 LLM 调用
"""

from typing import Dict, Any, Optional, Callable
import logging
from datetime import datetime

from app.models.analysis import UnifiedAnalysisTask, AnalysisTaskType, AnalysisStatus
from app.services.workflow_registry import AnalysisWorkflowRegistry
from app.utils.api_key_utils import is_valid_api_key
from app.utils.timezone import now_tz
from core.workflow.default_workflow_provider import get_default_workflow_provider

logger = logging.getLogger(__name__)

# 通用研究工作流在缺分析目标文本时使用的占位 ticker（仅引擎入参，不进 symbol）。
#
# ⚠️ 这是一个**引擎内部占位串，不是证券代码**：既不能拿去查行情，也不能拿去查库。
#    任何「把 ticker 解析为市场」的调用方**必须**走
#    ``app.core.market_codes.resolve_instrument`` —— 它对 ``"__GENERAL__"`` 返回
#    ``("", "")``，调用方据此按「没有标的」处理。
#    **不要**直接调用 ``app.core.market_codes.guess_market``：它的规则是「含字母 → us」，
#    会把任何含字母的串（本哨兵、以及含拉丁字母的自由文本分析目标）一律判成美股。
#
#    澄清一个错误归因：曾有报告称原值 ``"MARKET"`` 会命中美股代码正则
#    ``^[A-Z]{1,5}$``。这是错的 —— ``"MARKET"`` 有 6 个字母，1-5 位正则压根不匹配，
#    ``StockUtils.identify_stock_market("MARKET")`` 实测返回 ``UNKNOWN``。
#    真正的问题出在 ``guess_market`` 的「含字母 → us」规则上。也正因为如此，
#    把哨兵换成同样含字母的 ``"__GENERAL__"`` **并不能**靠「避开正则」解决；
#    正解是解析侧一律经 ``resolve_instrument``（形态白名单优先），而不是朴素猜市场。
GENERAL_TICKER_SENTINEL = "__GENERAL__"


def apply_general_ticker_fallback(workflow_inputs: Dict[str, Any]) -> Optional[str]:
    """通用研究工作流的 ticker 入参兜底（**仅对 general 任务生效**）。

    背景：``task_category == "general"`` 的研究流程没有个股代码，上游
    ``app/routers/workflows.py::_resolve_market_and_symbol`` 刻意**不**把自由文本写进
    ``symbol`` / ``stock_code``（否则会污染市场识别、报告标题与导出文件名）。
    但工作流引擎的 agent 以 ``ticker`` 作为「分析标的」入参（见各 adapter 的
    ``AgentInput(name="ticker", ...)``），缺失时会直接报
    ``Missing required parameters: ticker``，导致全部分析维度秒失败、任务「假成功」。

    本函数**只写 workflow_inputs["ticker"]**（引擎内部入参），
    绝不回写 ``symbol`` / ``stock_code``。

    门控：整个兜底都要求 ``task_category == "general"``（大小写/空白不敏感）。
    - 非 general 任务（普通股票 / ETF / 持仓 / 复盘）：``ticker`` 为空时**不做任何补齐**，
      返回 ``None``，让真实的「缺 ticker」配置错误照常暴露 —— 无条件补齐会把它悄悄掩盖。
    - general 任务：按 ``analysis_target`` > ``analysis_goal`` > ``company_of_interest``
      > ``GENERAL_TICKER_SENTINEL`` 的优先级补齐。

    Args:
        workflow_inputs: 工作流输入参数字典（原地修改 ticker）

    Returns:
        实际写入的 ticker 值；未做任何写入时返回 None。
    """
    if workflow_inputs.get("ticker"):
        return None

    if str(workflow_inputs.get("task_category") or "").strip().lower() != "general":
        # 非通用研究任务不做任何兜底：真正的缺参会照常报错，不能被掩盖。
        return None

    target = (
        workflow_inputs.get("analysis_target")
        or workflow_inputs.get("analysis_goal")
        or workflow_inputs.get("company_of_interest")
    )
    value = str(target).strip() if target and str(target).strip() else GENERAL_TICKER_SENTINEL

    workflow_inputs["ticker"] = value
    return value


def apply_general_trade_date_fallback(workflow_inputs: Dict[str, Any]) -> Optional[str]:
    """通用研究工作流：让用户显式提供的 trade_date 优先于「今天」。

    仅对 ``task_category == "general"`` 生效。

    背景：``_execute_via_workflow`` 原本**无条件**把 ``analysis_date`` 补成
    ``datetime.now()``，而 ``core/workflow/engine.py`` 的映射优先级是
    ``analysis_date or trade_date``，于是流程 1 的「交易日」字段被恒覆盖，
    用户选的日期永远不生效。本函数在兜底成「今天」之前，把用户显式提供的
    ``trade_date`` 写入 ``analysis_date``。

    门控/写入门槛：
    - 非 general 任务：不做任何写入，返回 ``None``；
    - ``analysis_date`` 已有值：不覆盖，返回 ``None``；
    - ``trade_date`` 缺省或为空白串：不写入，返回 ``None``；
    - 只写 ``analysis_date``，**绝不回写 ``trade_date``**。

    Args:
        workflow_inputs: 工作流输入参数字典（原地修改 analysis_date）

    Returns:
        实际写入 analysis_date 的值；未做任何写入时返回 None。
    """
    # 门控：仅通用研究任务（大小写/空白不敏感，与 apply_general_ticker_fallback 一致）
    if str(workflow_inputs.get("task_category") or "").strip().lower() != "general":
        return None

    # 已有 analysis_date 时不覆盖（用户/上游显式给定优先）
    if workflow_inputs.get("analysis_date"):
        return None

    trade_date = workflow_inputs.get("trade_date")
    value = str(trade_date).strip() if trade_date is not None else ""
    if not value:
        return None

    workflow_inputs["analysis_date"] = value
    return value


class UnifiedAnalysisEngine:
    """统一分析引擎
    
    负责执行各种类型的分析任务，自动选择合适的执行引擎
    
    使用示例:
        engine = UnifiedAnalysisEngine()
        result = await engine.execute_task(task, progress_callback)
    """
    
    def __init__(self):
        """初始化引擎"""
        self.logger = logger

    def _workflow_exists(self, workflow_id: Optional[str]) -> bool:
        """检查工作流是否存在于系统预置、数据库或文件系统中。"""
        if not workflow_id:
            return False

        provider = get_default_workflow_provider()
        if provider.get_system_workflow(workflow_id) is not None:
            return True

        from core.api.workflow_api import WorkflowAPI

        try:
            return WorkflowAPI().get(workflow_id) is not None
        except Exception as exc:
            self.logger.warning(f"⚠️ 校验工作流存在性失败: workflow_id={workflow_id}, error={exc}")
            return False

    def _workflow_type_for_task(self, task_type: AnalysisTaskType, task_params: dict = None) -> str:
        """将任务类型映射为工作流类型。"""
        # 如果有 task_category 标记，优先使用（通用研究任务用 STOCK_ANALYSIS 作为载体但实际是 general 类型）
        if task_params and task_params.get("task_category") == "general":
            return "general"
        mapping = {
            AnalysisTaskType.STOCK_ANALYSIS: "stock_analysis",
            AnalysisTaskType.ETF_ANALYSIS: "etf_analysis",
            AnalysisTaskType.POSITION_ANALYSIS: "position_analysis",
            AnalysisTaskType.TRADE_REVIEW: "trade_review",
        }
        return mapping.get(task_type, task_type.value if hasattr(task_type, "value") else str(task_type))

    def _resolve_workflow_id(self, task: UnifiedAnalysisTask, config) -> str:
        """解析本次任务应使用的工作流 ID。

        优先级：
        1. 任务显式指定的 workflow_id（如 /api/workflows/{id}/execute 传入）
        2. 我的分析流（用户为当前 task_type 设置的默认工作流）
        3. 注册表默认配置
        4. 系统硬编码默认值
        """
        task_type = task.task_type
        if isinstance(task_type, str):
            task_type = AnalysisTaskType(task_type)

        workflow_type = self._workflow_type_for_task(task_type, task.task_params)

        # 优先级 1：任务显式指定的 workflow_id
        explicit_workflow_id = task.workflow_id or task.task_params.get("workflow_id")
        if explicit_workflow_id:
            if self._workflow_exists(explicit_workflow_id):
                self.logger.info(f"📋 工作流选择: 使用任务显式指定的 workflow_id={explicit_workflow_id}")
                return explicit_workflow_id
            self.logger.warning(
                f"⚠️ 任务显式指定的工作流不存在，将回退到我的分析流配置: workflow_id={explicit_workflow_id}"
            )

        # 优先级 2：我的分析流（用户为当前 task_type 设置的默认工作流）
        if task_type in (
            AnalysisTaskType.STOCK_ANALYSIS,
            AnalysisTaskType.ETF_ANALYSIS,
            AnalysisTaskType.POSITION_ANALYSIS,
            AnalysisTaskType.TRADE_REVIEW,
        ):
            try:
                workflow_provider = get_default_workflow_provider()
                active_workflow_id = workflow_provider.get_active_workflow_id(workflow_type)
                if active_workflow_id:
                    if self._workflow_exists(active_workflow_id):
                        self.logger.info(f"📋 工作流选择: 使用我的分析流配置 type={workflow_type}, workflow_id={active_workflow_id}")
                        return active_workflow_id
                    try:
                        workflow_provider.load_workflow(active_workflow_id)
                    except Exception as exc:
                        self.logger.warning(
                            f"⚠️ 我的分析流配置 {active_workflow_id} 无效: {exc}"
                        )
                    else:
                        self.logger.info(f"📋 工作流选择: 使用我的分析流配置 type={workflow_type}, workflow_id={active_workflow_id}")
                        return active_workflow_id
                    self.logger.warning(
                        f"⚠️ 我的分析流配置不存在，将回退到注册表默认: workflow_id={active_workflow_id}"
                    )
            except Exception as exc:
                self.logger.warning(f"⚠️ 获取我的分析流配置失败: {exc}")

        # 优先级 3：注册表默认配置
        if self._workflow_exists(config.workflow_id):
            self.logger.info(f"📋 工作流选择: 使用注册表默认 workflow_id={config.workflow_id}")
            return config.workflow_id

        # 优先级 4：系统硬编码默认值
        fallback_workflow_id = get_default_workflow_provider().get_default_workflow_id_for_type(workflow_type)
        self.logger.warning(
            f"⚠️ 所有配置均无效，回退到系统默认工作流: type={workflow_type}, workflow_id={fallback_workflow_id}"
        )
        return fallback_workflow_id


    async def execute_task(
        self,
        task: UnifiedAnalysisTask,
        progress_callback: Optional[Callable[[int, str], None]] = None
    ) -> Dict[str, Any]:
        """执行分析任务
        
        Args:
            task: 统一分析任务对象
            progress_callback: 进度回调函数 callback(progress: int, message: str)
            
        Returns:
            分析结果字典
            
        Raises:
            ValueError: 任务类型未注册或参数无效
            RuntimeError: 执行失败
        """
        self.logger.info(f"🚀 开始执行任务: {task.task_id} (类型: {task.task_type})")
        
        # 1. 获取流程配置
        # 确保 task_type 是枚举类型（从数据库加载时可能是字符串）
        task_type = task.task_type
        if isinstance(task_type, str):
            try:
                task_type = AnalysisTaskType(task_type)
            except ValueError:
                raise ValueError(f"无效的任务类型: {task_type}")
        
        config = AnalysisWorkflowRegistry.get_config(task_type)
        if not config:
            raise ValueError(f"未注册的任务类型: {task_type}")
        
        # 2. 验证参数
        is_valid, error_msg = AnalysisWorkflowRegistry.validate_params(
            task.task_type,
            task.task_params
        )
        if not is_valid:
            raise ValueError(f"任务参数无效: {error_msg}")
        
        # 3. 选择执行引擎
        engine_type = self._select_engine(task.engine_type, config.default_engine)
        self.logger.info(f"📌 选择执行引擎: {engine_type}")
        
        # 4. 更新任务状态
        task.status = AnalysisStatus.PROCESSING
        task.started_at = now_tz()
        task.current_step = f"使用 {engine_type} 引擎执行"
        task.progress = 5

        # 通知进度
        if progress_callback:
            await self._call_progress_callback(progress_callback, 5, f"开始使用 {engine_type} 引擎执行")

        # 5. 执行分析
        try:
            if engine_type == "workflow":
                result = await self._execute_via_workflow(task, config, progress_callback)
            elif engine_type == "legacy":
                result = await self._execute_via_legacy(task, config, progress_callback)
            elif engine_type == "llm":
                result = await self._execute_via_llm(task, config, progress_callback)
            else:
                raise ValueError(f"不支持的引擎类型: {engine_type}")

            # 6. 更新任务状态
            task.status = AnalysisStatus.COMPLETED
            task.completed_at = now_tz()
            task.result = result
            task.progress = 100

            if task.started_at:
                task.execution_time = (task.completed_at - task.started_at).total_seconds()

            # 通知进度
            if progress_callback:
                await self._call_progress_callback(progress_callback, 100, "任务执行完成")

            self.logger.info(f"✅ 任务执行成功: {task.task_id} (耗时: {task.execution_time:.2f}秒)")

            return result

        except Exception as e:
            # 🔥 优雅处理：检查是否是取消异常
            from app.services.task_analysis_service import TaskCancelledException

            if isinstance(e, TaskCancelledException):
                # 任务取消是正常操作，不记录为错误
                self.logger.info(f"🚫 任务已取消: {task.task_id}")
                # 直接重新抛出，让上层处理
                raise

            # 其他异常才是真正的失败
            task.status = AnalysisStatus.FAILED
            task.completed_at = now_tz()
            task.error_message = str(e)

            if task.started_at:
                task.execution_time = (task.completed_at - task.started_at).total_seconds()

            self.logger.error(f"❌ 任务执行失败: {task.task_id} - {e}")
            raise RuntimeError(f"任务执行失败: {e}") from e
    
    async def _build_llm_config(
        self,
        quick_model: Optional[str],
        deep_model: Optional[str]
    ) -> Dict[str, Any]:
        """根据模型名称从数据库获取完整的 LLM 配置

        Args:
            quick_model: 快速分析模型名称
            deep_model: 深度分析模型名称

        Returns:
            包含 provider、api_key、backend_url 等的完整配置
        """
        if not quick_model and not deep_model:
            return {}

        from app.core.database import get_mongo_db

        db = get_mongo_db()

        # 获取系统配置
        config_doc = await db.system_configs.find_one(
            {"is_active": True},
            sort=[("version", -1)]
        )

        if not config_doc or "llm_configs" not in config_doc:
            self.logger.warning("数据库中没有 LLM 配置")
            return {}

        llm_configs = config_doc["llm_configs"]

        # 查找匹配的模型配置
        target_model = quick_model or deep_model
        model_config = None

        for cfg in llm_configs:
            if cfg.get("model_name") == target_model and cfg.get("enabled", True):
                model_config = cfg
                break

        if not model_config:
            self.logger.warning(f"未找到模型配置: {target_model}")
            return {}

        # 获取厂家配置
        provider_name = model_config.get("provider", "")
        provider_doc = await db.llm_providers.find_one({"name": provider_name})

        # 🔥 确定 API Key（优先级：模型配置 > 厂家配置 > 环境变量）
        # 守卫统一走 is_valid_api_key：会拒绝 your_*、*_here、sk-xxx、changeme
        # 等占位符，避免把 .env 里的模板值当有效 Key 发出去（历史 401 根因）
        api_key = None
        model_api_key = model_config.get("api_key", "")
        if is_valid_api_key(model_api_key):
            api_key = model_api_key
            self.logger.info(f"✅ [LLM配置] 使用模型配置的 API Key")
        elif model_api_key:
            self.logger.warning(
                f"⚠️ [LLM配置] 模型配置（{target_model}）的 API Key 是占位符/无效值，已忽略并继续回退"
            )
        if not api_key and provider_doc:
            provider_api_key = provider_doc.get("api_key", "")
            if is_valid_api_key(provider_api_key):
                api_key = provider_api_key
                self.logger.info(f"✅ [LLM配置] 使用厂家配置的 API Key")
            elif provider_api_key:
                self.logger.warning(
                    f"⚠️ [LLM配置] 厂家 {provider_name} 的 API Key 是占位符/无效值，已忽略"
                )
        
        backend_url = model_config.get("api_base", "")
        if provider_doc and not backend_url:
            backend_url = provider_doc.get("default_base_url", "")

        # 如果数据库没有 API Key，尝试从环境变量获取
        if not api_key:
            import os
            env_key_map = {
                "openai": "OPENAI_API_KEY",
                "deepseek": "DEEPSEEK_API_KEY",
                "dashscope": "DASHSCOPE_API_KEY",
                "google": "GOOGLE_API_KEY",
                "zhipu": "ZHIPU_API_KEY",
                "siliconflow": "SILICONFLOW_API_KEY",
            }
            env_name = env_key_map.get(provider_name.lower())
            if env_name:
                env_api_key = os.getenv(env_name)
                if env_api_key and not is_valid_api_key(env_api_key):
                    self.logger.warning(
                        f"⚠️ [LLM配置] 环境变量 {env_name} 是占位符/无效值，已忽略"
                    )
                else:
                    api_key = env_api_key

        # 🔥 获取快速模型配置
        quick_model_config = None
        if quick_model:
            for cfg in llm_configs:
                if cfg.get("model_name") == quick_model and cfg.get("enabled", True):
                    quick_model_config = cfg
                    break
        
        # 🔥 获取深度模型配置
        deep_model_config = None
        if deep_model and deep_model != quick_model:
            for cfg in llm_configs:
                if cfg.get("model_name") == deep_model and cfg.get("enabled", True):
                    deep_model_config = cfg
                    break
        
        # 如果深度模型配置不存在，使用快速模型配置
        if not deep_model_config:
            deep_model_config = quick_model_config or model_config
        
        # 获取深度模型的provider和api_key（如果不同）
        deep_provider_name = provider_name
        deep_api_key = api_key
        deep_backend_url = backend_url
        
        if deep_model_config and deep_model_config != quick_model_config:
            deep_provider_name = deep_model_config.get("provider", provider_name)
            # 🔥 确定深度模型的 API Key（优先级：模型配置 > 厂家配置 > 环境变量）
            deep_model_api_key = deep_model_config.get("api_key", "")
            if is_valid_api_key(deep_model_api_key):
                deep_api_key = deep_model_api_key
                self.logger.info(f"✅ [LLM配置] 深度模型使用模型配置的 API Key")
            elif deep_provider_name != provider_name:
                deep_provider_doc = await db.llm_providers.find_one({"name": deep_provider_name})
                if deep_provider_doc:
                    deep_provider_api_key = deep_provider_doc.get("api_key", "")
                    if is_valid_api_key(deep_provider_api_key):
                        deep_api_key = deep_provider_api_key
                        self.logger.info(f"✅ [LLM配置] 深度模型使用厂家配置的 API Key")
                    deep_backend_url = deep_model_config.get("api_base", "") or deep_provider_doc.get("default_base_url", "")
            if not is_valid_api_key(deep_api_key):
                import os
                env_key_map = {
                    "openai": "OPENAI_API_KEY",
                    "deepseek": "DEEPSEEK_API_KEY",
                    "dashscope": "DASHSCOPE_API_KEY",
                    "google": "GOOGLE_API_KEY",
                    "zhipu": "ZHIPU_API_KEY",
                    "siliconflow": "SILICONFLOW_API_KEY",
                }
                env_name = env_key_map.get(deep_provider_name.lower())
                if env_name:
                    env_api_key = os.getenv(env_name)
                    if env_api_key and not is_valid_api_key(env_api_key):
                        self.logger.warning(
                            f"⚠️ [LLM配置] 环境变量 {env_name} 是占位符/无效值，已忽略"
                        )
                    elif env_api_key:
                        deep_api_key = env_api_key
            else:
                deep_backend_url = deep_model_config.get("api_base", "") or backend_url
        
        result = {
            "llm_provider": provider_name,
            "quick_think_llm": quick_model or target_model,
            "deep_think_llm": deep_model or target_model,
            "backend_url": backend_url,
            "api_key": api_key,
            "quick_api_key": api_key,
            "deep_api_key": deep_api_key,
            "quick_temperature": (quick_model_config or model_config).get("temperature", 0.1),
            "quick_max_tokens": (quick_model_config or model_config).get("max_tokens", 2000),
            "quick_timeout": (quick_model_config or model_config).get("timeout", 60),
            "deep_temperature": deep_model_config.get("temperature", 0.1) if deep_model_config else (quick_model_config or model_config).get("temperature", 0.1),
            "deep_max_tokens": deep_model_config.get("max_tokens", 4000) if deep_model_config else (quick_model_config or model_config).get("max_tokens", 4000),
            "deep_timeout": deep_model_config.get("timeout", 120) if deep_model_config else (quick_model_config or model_config).get("timeout", 120),
            "deep_backend_url": deep_backend_url,  # 🔥 始终返回深度模型的backend_url
        }
        
        # 如果深度模型和快速模型使用不同的provider，添加深度模型的provider信息
        if deep_provider_name != provider_name:
            result["deep_llm_provider"] = deep_provider_name

        self.logger.info(f"从数据库获取模型配置: provider={provider_name}, quick={quick_model}, deep={deep_model}")
        self.logger.info(f"  quick: temperature={result['quick_temperature']}, max_tokens={result['quick_max_tokens']}, timeout={result['quick_timeout']}")
        self.logger.info(f"  deep: temperature={result['deep_temperature']}, max_tokens={result['deep_max_tokens']}, timeout={result['deep_timeout']}")

        return result

    async def _call_progress_callback(
        self,
        callback: Optional[Callable],
        progress: int,
        message: str,
        **kwargs
    ):
        """调用进度回调（支持同步和异步）"""
        if callback:
            import asyncio
            if asyncio.iscoroutinefunction(callback):
                await callback(progress, message, **kwargs)
            else:
                callback(progress, message, **kwargs)

    def _select_engine(self, requested_engine: str, default_engine: str) -> str:
        """选择执行引擎

        Args:
            requested_engine: 请求的引擎类型 (auto/workflow/legacy/llm)
            default_engine: 默认引擎类型

        Returns:
            实际使用的引擎类型

        Raises:
            RuntimeError: 当请求 legacy 引擎时（已停用）
        """
        # v3.0 起旧版 legacy 引擎已停用
        if requested_engine == "legacy":
            raise RuntimeError(
                "[已停用] UnifiedAnalysisEngine._select_engine 拒绝 legacy 引擎，"
                "请改用 workflow 或 auto。"
                "详见 docs/99-archive/deprecated-features/legacy-agents-deprecation.md"
            )
        if requested_engine == "auto":
            # 防御：若注册表默认引擎被改成 legacy，强制改为 workflow
            if default_engine == "legacy":
                self.logger.warning(
                    "⚠️ 注册表默认引擎为 legacy，已强制改为 workflow（legacy 已停用）"
                )
                return "workflow"
            return default_engine
        return requested_engine

    async def _inject_memory_facts(self, workflow_inputs: Dict[str, Any]) -> None:
        """查询该股票的未过期事实记忆并注入工作流输入（失败不影响分析）。

        事实记忆来源：智能助手联网工具提取（assistant_memory_facts 集合），
        按 symbol / related_terms 匹配股票代码，只取 valid_until 未过期的记录。
        注入文本含采集日期与来源，供辩论环节（bull/bear）与风险评估环节
        （risk_manager）作为可溯源材料参考（消费方见 core/agents/base.py
        的事实记忆注入白名单）。
        """
        try:
            ticker_val = str(workflow_inputs.get("ticker") or "").strip()
            if not ticker_val:
                return
            from datetime import datetime as _dt
            from app.core.database import get_mongo_db
            db = get_mongo_db()
            now_iso = _dt.now().isoformat(timespec="seconds")
            cursor = (
                db["assistant_memory_facts"]
                .find(
                    {
                        "valid_until": {"$gte": now_iso},
                        "$or": [
                            {"symbol": ticker_val},
                            {"related_terms": ticker_val},
                        ],
                    }
                )
                .sort("fetched_at", -1)
                .limit(8)
            )
            docs = [doc async for doc in cursor]
            if not docs:
                return
            blocks = [
                "【外部事实记忆】以下事实来自智能助手此前通过联网工具获取并验证的真实数据"
                "（含采集日期与来源，已通过时效校验）。可将其作为本次分析材料的一部分参考，"
                "引用时需标注采集日期；若与本次分析获取的最新数据不一致，以本次数据为准。"
            ]
            for i, doc in enumerate(docs, 1):
                fetched = str(doc.get("fetched_at", ""))[:16].replace("T", " ")
                blocks.append(
                    f"--- 事实 {i} | 采集于 {fetched} | 来源 {doc.get('tool_id', '')} | "
                    f"主题：{doc.get('topic', '')} ---\n{str(doc.get('content', ''))[:1000]}"
                )
            workflow_inputs["memory_facts_summary"] = "\n\n".join(blocks)
            self.logger.info(
                "🧠 [事实记忆注入] ticker=%s 已注入 %d 条外部事实记忆",
                ticker_val, len(docs),
            )
        except Exception as e:
            self.logger.warning(f"⚠️ [事实记忆注入] 失败（不影响分析）: {e}")

    async def _execute_via_workflow(
        self,
        task: UnifiedAnalysisTask,
        config,
        progress_callback: Optional[Callable] = None
    ) -> Dict[str, Any]:
        """通过工作流引擎执行

        Args:
            task: 任务对象
            config: 工作流配置
            progress_callback: 进度回调

        Returns:
            执行结果
        """
        workflow_id = self._resolve_workflow_id(task, config)
        self.logger.info(f"🔄 使用工作流引擎执行: {workflow_id}")

        # 导入工作流 API
        from core.api.workflow_api import WorkflowAPI

        workflow_api = WorkflowAPI()

        # 保存主事件循环的引用（在调用 asyncio.to_thread 之前）
        import asyncio
        main_loop = asyncio.get_running_loop()

        # 创建包装的进度回调，用于更新任务对象
        # 注意：这个回调会在工作流引擎的线程中被调用（因为使用了 asyncio.to_thread）
        # 所以不能直接调用异步函数，需要使用 asyncio.run_coroutine_threadsafe
        def wrapped_progress_callback(progress: int, message: str, **kwargs):
            """包装的进度回调：更新任务对象并调用原始回调"""
            # 🔑 关键：从 kwargs 中提取 step_name（简短名称）
            step_name = kwargs.get("step_name", "")

            # 更新任务对象（这是线程安全的，因为只是修改对象属性）
            task.progress = progress
            task.current_step = step_name or message  # ✅ 使用 step_name 而不是 message
            task.message = message  # ✅ 保存详细描述到 message 字段

            self.logger.debug(f"📊 进度更新: {progress}% - {step_name} - {message}")

            # 调用原始回调（原始回调会检查取消标记）
            if progress_callback:
                if asyncio.iscoroutinefunction(progress_callback):
                    # 如果是异步回调，需要在主事件循环中运行
                    try:
                        # 使用保存的主事件循环引用
                        future = asyncio.run_coroutine_threadsafe(
                            progress_callback(progress, message, **kwargs),
                            main_loop
                        )
                        # 🔥 关键：等待回调完成，以便捕获 TaskCancelledException
                        try:
                            future.result(timeout=5)  # 等待最多5秒
                        except Exception as callback_error:
                            # 如果回调抛出异常（比如 TaskCancelledException），向上传播
                            self.logger.warning(f"⚠️ 进度回调执行异常: {callback_error}")
                            raise callback_error
                    except Exception as e:
                        self.logger.warning(f"⚠️ 进度回调调度失败: {e}")
                        raise  # 向上传播异常
                else:
                    # 同步回调可以直接调用
                    try:
                        progress_callback(progress, message, **kwargs)
                    except Exception as e:
                        self.logger.warning(f"⚠️ 进度回调执行失败: {e}")
                        raise  # 向上传播异常
        
        # 准备工作流输入
        workflow_inputs = task.task_params.copy()

        # 🔑 创建 AgentContext 并添加到工作流输入（用于获取用户配置的提示词）
        from tradingagents.agents.utils.agent_context import AgentContext

        agent_context = AgentContext(
            user_id=str(task.user_id),
            preference_id=task.preference_type,  # 使用任务的偏好类型
            session_id=None,
            request_id=None,
            is_debug_mode=bool(task.task_params.get("debug_mode", False)),
            debug_template_id=task.task_params.get("debug_template_id"),
            workflow_id=workflow_id,
        )

        # 将 AgentContext 添加到工作流输入
        workflow_inputs["context"] = agent_context

        # 参数映射：将 symbol/code/stock_code 映射为 ticker（工作流引擎使用 ticker）
        # ⚠️ 不同 service 用的字段名不一致：
        #   - stock_analysis 用 symbol
        #   - trade_review 用 code
        #   - portfolio_analysis 用 stock_code
        # 统一映射到 ticker，避免 agents 报 "Missing required parameters: ticker"
        if "ticker" not in workflow_inputs or not workflow_inputs.get("ticker"):
            for key in ("symbol", "code", "stock_code"):
                val = workflow_inputs.get(key)
                if val:
                    workflow_inputs["ticker"] = val
                    self.logger.debug(f"📋 参数映射: {key} → ticker = {val}")
                    break

        # 🧠 外部事实记忆注入：查询该股票相关的未过期事实记忆（智能助手联网提取），
        # 写入 memory_facts_summary 供辩论环节（bull/bear）与风险评估环节（risk_manager）
        # 作为分析材料参考（消费方见 core/agents/base.py 的事实记忆注入白名单）。
        await self._inject_memory_facts(workflow_inputs)

        # 🆕 读取工作流的 input_config，根据字段定义补充缺失参数
        try:
            from app.core.database import get_mongo_db
            db = get_mongo_db()
            wf_doc = await db.workflows.find_one({"_id": workflow_id}, {"input_config": 1, "workflow_type": 1})
            if wf_doc and wf_doc.get("input_config"):
                fields = wf_doc["input_config"].get("fields", [])
                for field in fields:
                    fname = field.get("name")
                    if fname and fname not in workflow_inputs and fname not in ("context",):
                        default_val = field.get("default")
                        if default_val is not None:
                            workflow_inputs[fname] = default_val
                            self.logger.debug(f"📋 从 input_config 补充参数: {fname}={default_val}")
        except Exception as e:
            self.logger.warning(f"⚠️ 读取 input_config 失败，使用硬编码默认值: {e}")

        # 🆕 通用研究工作流（task_category=general）没有个股代码。
        # 上游 app/routers/workflows.py::_resolve_market_and_symbol 刻意**不**把自由文本
        # 写进 symbol / stock_code（否则会污染市场识别、报告标题与导出文件名）。
        # 但工作流引擎的 agent 以 ticker 作为「分析标的」入参，缺失时会直接报
        # "Missing required parameters: ticker"，使全部分析维度秒失败、任务假成功。
        # 兜底实现见 apply_general_ticker_fallback（**只补 ticker 入参**，不回写 symbol）。
        _filled_ticker = apply_general_ticker_fallback(workflow_inputs)
        if _filled_ticker:
            self.logger.info(
                f"📋 [general] 分析目标 → ticker 入参 = {_filled_ticker!r}"
                f"（仅引擎入参，不写 symbol/stock_code）"
            )

        # 🆕 [general] 流程 1「交易日」：让用户显式提供的 trade_date 优先于「今天」。
        # 真因：下面 `if not analysis_date` 会**无条件**兜底成 datetime.now()，而引擎
        # 的映射优先级是 `analysis_date or trade_date`，用户选的交易日因此被恒覆盖。
        # 兜底见 apply_general_trade_date_fallback（**只写 analysis_date**，不回写 trade_date）。
        _filled_trade_date = apply_general_trade_date_fallback(workflow_inputs)
        if _filled_trade_date:
            self.logger.info(
                f"📋 [general] 交易日 trade_date → analysis_date 入参 = {_filled_trade_date!r}"
            )

        # 通用兜底参数（当 input_config 不存在或未定义对应字段时）
        from datetime import datetime
        # 🔑 注意：不能用 setdefault，因为 API 请求可能传入 analysis_date=None，
        # setdefault 不会覆盖已存在但值为 None 的 key，导致下游 Agent 报错
        if not workflow_inputs.get("analysis_date"):
            workflow_inputs["analysis_date"] = datetime.now().strftime("%Y-%m-%d")
        # 同上：不能用 setdefault —— key 存在但值为 None 时 setdefault 不生效。
        # research_depth 是字符串，空串等价于「未指定」；两个数字参数用 is None 判断，
        # 因为 0 可能是合法值（如 lookback_days=0），不能被误替换成默认值。
        if not workflow_inputs.get("research_depth"):
            workflow_inputs["research_depth"] = "标准"
        # ⚠️ lookback_days 默认值**仅对 general 补齐**：该字段只存在于流程 3，若像原
        # 来那样无条件写入，单股/ETF 等没有该字段的分析类型也会被塞入默认值；一旦
        # lookback_days 被提升为动态 State 字段，就会泄漏并污染单股分析的取证窗口。
        # 保留「显式判空」的外层写法（不要改用 setdefault）。
        if workflow_inputs.get("lookback_days") is None:
            if str(workflow_inputs.get("task_category") or "").strip().lower() == "general":
                workflow_inputs["lookback_days"] = 30
        if workflow_inputs.get("max_debate_rounds") is None:
            workflow_inputs["max_debate_rounds"] = 3

        self.logger.info(f"📦 工作流输入参数: ticker={workflow_inputs.get('ticker')}, "
                        f"analysis_date={workflow_inputs.get('analysis_date')}, "
                        f"research_depth={workflow_inputs.get('research_depth')}, "
                        f"selected_analysts={workflow_inputs.get('selected_analysts')}, "
                        f"user_id={agent_context.user_id}, "
                        f"preference_id={agent_context.preference_id}")

        # 准备遗留配置（LLM配置等）
        legacy_config = {
            "preference_type": task.preference_type,
        }

        if workflow_inputs.get("debug_mode"):
            if workflow_inputs.get("debug_llm_provider"):
                legacy_config["llm_provider"] = workflow_inputs["debug_llm_provider"]
            if workflow_inputs.get("debug_llm_backend_url"):
                legacy_config["backend_url"] = workflow_inputs["debug_llm_backend_url"]
            if workflow_inputs.get("debug_llm_api_key"):
                legacy_config["quick_api_key"] = workflow_inputs["debug_llm_api_key"]
                legacy_config["deep_api_key"] = workflow_inputs["debug_llm_api_key"]
            if workflow_inputs.get("debug_llm_temperature") is not None or workflow_inputs.get("debug_llm_max_tokens") is not None:
                debug_model_config = {
                    "temperature": workflow_inputs.get("debug_llm_temperature"),
                    "max_tokens": workflow_inputs.get("debug_llm_max_tokens"),
                    "timeout": 300 if str(workflow_inputs.get("debug_llm_provider", "")).lower() == "ollama" else 180,
                }
                legacy_config["quick_model_config"] = debug_model_config
                legacy_config["deep_model_config"] = debug_model_config

        # 🔑 关键：从任务参数中提取 selected_analysts（用于动态裁剪工作流）
        if "selected_analysts" in workflow_inputs:
            legacy_config["selected_analysts"] = workflow_inputs["selected_analysts"]
            self.logger.info(f"🎯 选中的分析师: {workflow_inputs['selected_analysts']}")

        # 🧠 A1b: 深度档位 → 思考预算（传给 builder，与 DB 显式配置合并，仅火山方舟推理模型注入）
        try:
            from app.services.simple_analysis_service import RESEARCH_DEPTH_REASONING_EFFORT
            _depth_key = str(workflow_inputs.get("research_depth") or "标准").strip()
            legacy_config["depth_reasoning_effort"] = RESEARCH_DEPTH_REASONING_EFFORT.get(_depth_key, "medium")
            self.logger.info(
                f"🧠 [reasoning_effort] 档位={_depth_key} → 思考预算默认={legacy_config['depth_reasoning_effort']}"
                f"（DB 显式配置优先，仅火山方舟推理模型注入）"
            )
        except ImportError:
            pass

        # 从任务参数中提取 LLM 配置；若未指定，则从数据库系统配置读取默认值
        quick_model = workflow_inputs.get("quick_analysis_model")
        deep_model = workflow_inputs.get("deep_analysis_model")

        if not quick_model or not deep_model:
            # 从数据库读取系统默认模型
            try:
                from app.core.database import get_mongo_db as _get_db
                _db = _get_db()
                _cfg = await _db.system_configs.find_one(
                    {"is_active": True}, sort=[("version", -1)]
                )
                if _cfg:
                    _default_models = _cfg.get("default_models") or {}
                    _sys_settings = _cfg.get("system_settings") or {}
                    if not quick_model:
                        quick_model = (
                            _default_models.get("quick_analysis_model")
                            or _sys_settings.get("quick_analysis_model")
                        )
                    if not deep_model:
                        deep_model = (
                            _default_models.get("deep_analysis_model")
                            or _sys_settings.get("deep_analysis_model")
                        )
                    self.logger.info(
                        f"📋 从系统配置读取默认模型: quick={quick_model}, deep={deep_model}"
                    )
            except Exception as _e:
                self.logger.warning(f"⚠️ 读取系统默认模型失败: {_e}")

        if quick_model:
            legacy_config["quick_think_llm"] = quick_model
        if deep_model:
            legacy_config["deep_think_llm"] = deep_model

        # 始终调用 _build_llm_config 从数据库获取完整 LLM 配置（api_key / backend_url 等）
        if quick_model or deep_model:
            full_config = await self._build_llm_config(quick_model, deep_model)
            legacy_config.update(full_config)

            if workflow_inputs.get("debug_mode"):
                if workflow_inputs.get("debug_llm_provider"):
                    legacy_config["llm_provider"] = workflow_inputs["debug_llm_provider"]
                if workflow_inputs.get("debug_llm_backend_url"):
                    legacy_config["backend_url"] = workflow_inputs["debug_llm_backend_url"]
                if workflow_inputs.get("debug_llm_api_key"):
                    legacy_config["quick_api_key"] = workflow_inputs["debug_llm_api_key"]
                    legacy_config["deep_api_key"] = workflow_inputs["debug_llm_api_key"]
                if workflow_inputs.get("debug_llm_temperature") is not None or workflow_inputs.get("debug_llm_max_tokens") is not None:
                    debug_model_config = {
                        "temperature": workflow_inputs.get("debug_llm_temperature"),
                        "max_tokens": workflow_inputs.get("debug_llm_max_tokens"),
                        "timeout": 300 if str(workflow_inputs.get("debug_llm_provider", "")).lower() == "ollama" else 180,
                    }
                    legacy_config["quick_model_config"] = debug_model_config
                    legacy_config["deep_model_config"] = debug_model_config

        self.logger.info(f"🔧 LLM配置: quick={legacy_config.get('quick_think_llm')}, "
                        f"deep={legacy_config.get('deep_think_llm')}, "
                        f"provider={legacy_config.get('llm_provider')}")

        # 执行工作流（WorkflowAPI.execute 是同步方法，需要在线程中运行）
        import asyncio

        # 🔑 执行轨迹单次开启：如果任务输入指定 enable_trace=true，临时全局开启
        # 注意：不能用线程本地变量，因为分析师在并行子线程执行，读不到主线程的标记
        def _execute_with_trace_context():
            from app.core.config import settings
            old_value = settings.AGENT_EXECUTION_TRACE_ENABLED
            if task.task_params.get("enable_trace"):
                settings.AGENT_EXECUTION_TRACE_ENABLED = True
            try:
                return workflow_api.execute(
                    workflow_id=workflow_id,
                    inputs=workflow_inputs,
                    legacy_config=legacy_config,
                    progress_callback=wrapped_progress_callback,
                    task_id=task.task_id,
                )
            finally:
                settings.AGENT_EXECUTION_TRACE_ENABLED = old_value

        result = await asyncio.to_thread(_execute_with_trace_context)

        # 检查执行结果
        if not result.get("success"):
            error_msg = result.get("error", "工作流执行失败")
            raise RuntimeError(error_msg)

        return result.get("result", {})

    async def _execute_via_legacy(
        self,
        task: UnifiedAnalysisTask,
        config,
        progress_callback: Optional[Callable] = None
    ) -> Dict[str, Any]:
        """通过旧引擎执行（TradingAgentsGraph）— 已停用

        v3.0 起旧版 legacy 引擎已停用，所有入口已封堵。
        详见 docs/99-archive/deprecated-features/legacy-agents-deprecation.md

        Args:
            task: 任务对象
            config: 工作流配置
            progress_callback: 进度回调

        Returns:
            执行结果

        Raises:
            RuntimeError: 始终抛出（方法已停用）
        """
        raise RuntimeError(
            "[已停用] UnifiedAnalysisEngine._execute_via_legacy 已停用，"
            "任务 %s 应改用 workflow 引擎执行。"
            "详见 docs/99-archive/deprecated-features/legacy-agents-deprecation.md"
            % getattr(task, "task_id", "<unknown>")
        )
        # pragma: no cover - 以下原代码保留但不执行，为 v4.0 删除做准备
        self.logger.info(f"🔄 使用旧引擎执行: TradingAgentsGraph")

        # 只支持股票分析任务
        if task.task_type != AnalysisTaskType.STOCK_ANALYSIS:
            raise ValueError(f"旧引擎只支持股票分析任务，当前任务类型: {task.task_type}")

        # 导入旧引擎
        from tradingagents.graph.trading_graph import TradingAgentsGraph

        # 准备参数
        symbol = task.task_params.get("symbol")
        analysis_date = task.task_params.get("analysis_date")

        if not symbol:
            raise ValueError("缺少必需参数: symbol")

        # 创建引擎实例
        graph = TradingAgentsGraph(debug=False)

        # 执行分析
        self.logger.info(f"📊 分析股票: {symbol} (日期: {analysis_date or '最新'})")

        # 调用 propagate 方法
        state, decision = graph.propagate(symbol, analysis_date)

        # 转换结果格式
        result = {
            "engine": "legacy",
            "symbol": symbol,
            "analysis_date": analysis_date,
            "decision": decision,
            "state": state,
            "source": "TradingAgentsGraph"
        }

        return result

    async def _execute_via_llm(
        self,
        task: UnifiedAnalysisTask,
        config,
        progress_callback: Optional[Callable] = None
    ) -> Dict[str, Any]:
        """通过直接 LLM 调用执行

        Args:
            task: 任务对象
            config: 工作流配置
            progress_callback: 进度回调

        Returns:
            执行结果
        """
        self.logger.info(f"🔄 使用 LLM 直接调用执行")

        # 导入 LLM 管理器
        from core.llm.llm_manager import LLMManager

        llm_manager = LLMManager()

        # 根据任务类型构建提示词
        prompt = self._build_prompt_for_task(task)

        # 调用 LLM
        self.logger.info(f"💬 调用 LLM: {task.preference_type} 偏好")

        response = await llm_manager.generate(
            prompt=prompt,
            preference_type=task.preference_type,
            temperature=0.7
        )

        # 转换结果格式
        result = {
            "engine": "llm",
            "task_type": task.task_type.value,
            "response": response,
            "source": "LLM直接调用"
        }

        return result

    def _build_prompt_for_task(self, task: UnifiedAnalysisTask) -> str:
        """为任务构建提示词

        Args:
            task: 任务对象

        Returns:
            提示词字符串
        """
        # 根据任务类型构建不同的提示词
        if task.task_type == AnalysisTaskType.PORTFOLIO_HEALTH:
            return self._build_portfolio_health_prompt(task.task_params)
        elif task.task_type == AnalysisTaskType.RISK_ASSESSMENT:
            return self._build_risk_assessment_prompt(task.task_params)
        elif task.task_type == AnalysisTaskType.MARKET_OVERVIEW:
            return self._build_market_overview_prompt(task.task_params)
        else:
            # 通用提示词
            return f"""请分析以下任务：

任务类型: {task.task_type.value}
任务参数: {task.task_params}

请提供详细的分析结果。"""

    def _build_portfolio_health_prompt(self, params: Dict[str, Any]) -> str:
        """构建组合健康度分析提示词"""
        return f"""请分析投资组合的健康度。

分析要点：
1. 持仓集中度分析
2. 风险分散情况
3. 收益稳定性
4. 资金使用效率
5. 整体健康度评分

参数: {params}

请提供详细的研究报告。"""

    def _build_risk_assessment_prompt(self, params: Dict[str, Any]) -> str:
        """构建风险评估提示词"""
        return f"""请进行风险评估分析。

评估维度：
1. 市场风险
2. 个股风险
3. 流动性风险
4. 集中度风险
5. 综合风险评级

参数: {params}

请提供详细的风险评估报告。"""

    def _build_market_overview_prompt(self, params: Dict[str, Any]) -> str:
        """构建市场概览提示词"""
        return f"""请提供市场概览分析。

分析内容：
1. 市场整体走势
2. 板块表现
3. 资金流向
4. 市场情绪
5. 研究结论

参数: {params}

请提供详细的市场研究报告。"""

