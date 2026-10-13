"""
研究报告管理 API 路由
"""
import os
import re
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel

from .auth_db import get_current_user
from ..core.database import get_mongo_db
from ..utils.timezone import to_config_tz
import logging

logger = logging.getLogger("webapi")

ETF_CODE_PREFIXES = ("50", "51", "52", "56", "58", "15", "16", "18")

# 股票名称缓存
_stock_name_cache = {}
_DISPLAY_SANITIZED_MODULE_KEYS = {
    "bull_researcher",
    "bull_report",
    "bear_researcher",
    "bear_report",
    "research_team_decision",
    "investment_plan",
    "trader_investment_plan",
    "risky_analyst",
    "risky_opinion",
    "safe_analyst",
    "safe_opinion",
    "neutral_analyst",
    "neutral_opinion",
    "risk_management_decision",
}


def _is_cn_etf_symbol(stock_code: str) -> bool:
    code_str = str(stock_code or "").strip().zfill(6)
    return len(code_str) == 6 and code_str.isdigit() and code_str.startswith(ETF_CODE_PREFIXES)


def _is_invalid_stock_name(stock_code: str, stock_name: Optional[str]) -> bool:
    if not stock_name:
        return True
    code_str = str(stock_code or "").strip().zfill(6)
    name = str(stock_name).strip()
    generic_names = {
        code_str,
        stock_code,
        f"股票{code_str}",
        f"股票{stock_code}",
        f"ETF{code_str}",
        f"ETF{stock_code}",
    }
    return name in generic_names

def get_stock_name(stock_code: str) -> str:
    """
    获取股票名称
    优先级：缓存 -> MongoDB（按数据源优先级） -> 默认返回股票代码
    """
    global _stock_name_cache

    # 检查缓存
    if stock_code in _stock_name_cache:
        return _stock_name_cache[stock_code]

    try:
        # 从 MongoDB 获取股票名称
        from ..core.database import get_mongo_db_sync
        from ..core.unified_config import UnifiedConfigManager

        db = get_mongo_db_sync()
        code6 = str(stock_code).zfill(6)

        if _is_cn_etf_symbol(code6):
            etf_info = db.etf_basic_info.find_one(
                {"code": code6},
                {"name": 1, "fund_name": 1, "etf_name": 1}
            )
            if etf_info:
                etf_name = etf_info.get("name") or etf_info.get("fund_name") or etf_info.get("etf_name")
                if etf_name:
                    _stock_name_cache[stock_code] = etf_name
                    return etf_name

        # 🔥 按数据源优先级查询
        config = UnifiedConfigManager()
        data_source_configs = config.get_data_source_configs()

        # 提取启用的数据源，按优先级排序
        enabled_sources = [
            ds.type.lower() for ds in data_source_configs
            if ds.enabled and ds.type.lower() in ['tushare', 'akshare', 'baostock']
        ]

        if not enabled_sources:
            enabled_sources = ['tushare', 'akshare', 'baostock']

        # 按数据源优先级查询
        stock_info = None
        for data_source in enabled_sources:
            stock_info = db.stock_basic_info.find_one(
                {"$or": [{"symbol": code6}, {"code": code6}], "source": data_source}
            )
            if stock_info:
                logger.debug(f"✅ 使用数据源 {data_source} 获取股票名称 {code6}")
                break

        # 如果所有数据源都没有，尝试不带 source 条件查询（兼容旧数据）
        if not stock_info:
            stock_info = db.stock_basic_info.find_one(
                {"$or": [{"symbol": code6}, {"code": code6}]}
            )
            if stock_info:
                logger.warning(f"⚠️ 使用旧数据（无 source 字段）获取股票名称 {code6}")

        if stock_info and stock_info.get("name"):
            stock_name = stock_info["name"]
            _stock_name_cache[stock_code] = stock_name
            return stock_name

        # 如果没有找到，返回股票代码
        _stock_name_cache[stock_code] = stock_code
        return stock_code

    except Exception as e:
        logger.warning(f"⚠️ 获取股票名称失败 {stock_code}: {e}")
        return stock_code


# 统一构建报告查询：支持 _id(ObjectId) / analysis_id / task_id 三种
def _build_report_query(report_id: str) -> Dict[str, Any]:
    ors = [
        {"analysis_id": report_id},
        {"task_id": report_id},
    ]
    try:
        from bson import ObjectId
        ors.append({"_id": ObjectId(report_id)})
    except Exception:
        pass
    return {"$or": ors}


def _rebuild_report_modules(raw_result: Dict[str, Any], existing_reports: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """从任务原始结果中尽可能还原完整报告模块。"""
    rebuilt_reports: Dict[str, Any] = {}
    from app.utils.compliance import sanitize_display_report_text

    if isinstance(existing_reports, dict):
        for key, value in existing_reports.items():
            if key == "structured_reports":
                continue
            if isinstance(value, str) and value.strip():
                text_value = value.strip()
                if key in _DISPLAY_SANITIZED_MODULE_KEYS:
                    text_value = sanitize_display_report_text(text_value, fallback=text_value)
                rebuilt_reports[key] = text_value
            elif value is not None:
                text_value = str(value).strip()
                if text_value:
                    if key in _DISPLAY_SANITIZED_MODULE_KEYS:
                        text_value = sanitize_display_report_text(text_value, fallback=text_value)
                    rebuilt_reports[key] = text_value

    if not isinstance(raw_result, dict):
        return rebuilt_reports

    try:
        from app.utils.report_formatter import extract_reports_from_state

        extracted_from_result = extract_reports_from_state(raw_result)
        if extracted_from_result:
            rebuilt_reports.update(extracted_from_result)

        state = raw_result.get("state")
        if state:
            extracted_from_state = extract_reports_from_state(state)
            if extracted_from_state:
                rebuilt_reports.update(extracted_from_state)
    except Exception as e:
        logger.warning(f"⚠️ 还原完整报告模块失败: {e}")

    return rebuilt_reports

router = APIRouter(prefix="/api/reports", tags=["reports"])

class ReportFilter(BaseModel):
    """报告筛选参数"""
    search_keyword: Optional[str] = None
    market_filter: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    stock_code: Optional[str] = None
    report_type: Optional[str] = None

class ReportListResponse(BaseModel):
    """报告列表响应"""
    reports: List[Dict[str, Any]]
    total: int
    page: int
    page_size: int

@router.get("/list", response_model=Dict[str, Any])
async def get_reports_list(
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=100, description="每页数量"),
    search_keyword: Optional[str] = Query(None, description="搜索关键词"),
    market_filter: Optional[str] = Query(None, description="市场筛选（A股/港股/美股）"),
    start_date: Optional[str] = Query(None, description="开始日期"),
    end_date: Optional[str] = Query(None, description="结束日期"),
    stock_code: Optional[str] = Query(None, description="股票代码"),
    user: dict = Depends(get_current_user)
):
    """获取研究报告列表"""
    try:
        logger.info(f"🔍 获取报告列表: 用户={user['id']}, 页码={page}, 每页={page_size}, 市场={market_filter}")

        db = get_mongo_db()

        # 构建查询条件
        query = {}

        # 搜索关键词
        if search_keyword:
            query["$or"] = [
                {"stock_symbol": {"$regex": search_keyword, "$options": "i"}},
                {"analysis_id": {"$regex": search_keyword, "$options": "i"}},
                {"summary": {"$regex": search_keyword, "$options": "i"}}
            ]

        # 市场筛选
        if market_filter:
            query["market_type"] = market_filter

        # 股票代码筛选
        if stock_code:
            query["stock_symbol"] = stock_code

        # 日期范围筛选
        if start_date or end_date:
            date_query = {}
            if start_date:
                date_query["$gte"] = start_date
            if end_date:
                date_query["$lte"] = end_date
            query["analysis_date"] = date_query

        logger.info(f"📊 查询条件: {query}")

        # 计算总数
        total = await db.analysis_reports.count_documents(query)

        # 分页查询
        skip = (page - 1) * page_size
        cursor = db.analysis_reports.find(query).sort("created_at", -1).skip(skip).limit(page_size)

        reports = []
        async for doc in cursor:
            # 转换为前端需要的格式
            stock_code = doc.get("stock_symbol", "")
            # 🔥 优先使用MongoDB中保存的股票名称，如果没有则查询
            stock_name = doc.get("stock_name")
            if _is_invalid_stock_name(stock_code, stock_name):
                stock_name = get_stock_name(stock_code)

            # 🔥 获取市场类型，如果没有则根据股票代码推断
            market_type = doc.get("market_type")
            if not market_type:
                from tradingagents.utils.stock_utils import StockUtils
                market_info = StockUtils.get_market_info(stock_code)
                market_type_map = {
                    "china_a": "A股",
                    "hong_kong": "港股",
                    "us": "美股",
                    "unknown": "A股"
                }
                market_type = market_type_map.get(market_info.get("market", "unknown"), "A股")

            # 获取创建时间（数据库中是 UTC 时间，需要转换为 UTC+8）
            created_at = doc.get("created_at", datetime.utcnow())
            created_at_tz = to_config_tz(created_at)  # 转换为 UTC+8 并添加时区信息

            report = {
                "id": str(doc["_id"]),
                "analysis_id": doc.get("analysis_id", ""),
                "title": f"{stock_name}({stock_code}) 研究报告",
                "stock_code": stock_code,
                "stock_name": stock_name,
                "market_type": market_type,  # 🔥 添加市场类型字段
                "model_info": doc.get("model_info", "Unknown"),  # 🔥 添加模型信息字段
                "type": "single",  # 目前主要是单股研究
                "format": "markdown",  # 主要格式
                "status": doc.get("status", "completed"),
                "created_at": created_at_tz.isoformat() if created_at_tz else str(created_at),
                "analysis_date": doc.get("analysis_date", ""),
                "analysts": doc.get("analysts", []),
                "research_depth": doc.get("research_depth", 1),
                "summary": doc.get("summary", ""),
                "file_size": len(str(doc.get("reports", {}))),  # 估算大小
                "source": doc.get("source", "unknown"),
                "task_id": doc.get("task_id", "")
            }
            reports.append(report)

        logger.info(f"✅ 查询完成: 总数={total}, 返回={len(reports)}")

        return {
            "success": True,
            "data": {
                "reports": reports,
                "total": total,
                "page": page,
                "page_size": page_size
            },
            "message": "报告列表获取成功"
        }

    except Exception as e:
        logger.error(f"❌ 获取报告列表失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/{report_id}/detail")
async def get_report_detail(
    report_id: str,
    include_state: bool = Query(False, description="是否包含完整工作流状态（调试用）"),
    include_detailed: bool = Query(False, description="是否包含详细分析数据（调试用）"),
    user: dict = Depends(get_current_user)
):
    """获取报告详情
    
    默认只返回必要字段（reports, decision, summary等），不包含 state 和 detailed_analysis。
    如需完整数据，可通过查询参数 include_state=true&include_detailed=true 获取。
    """
    try:
        logger.info(f"🔍 获取报告详情: {report_id}")

        db = get_mongo_db()

        # 支持 ObjectId / analysis_id / task_id
        query = _build_report_query(report_id)
        doc = await db.analysis_reports.find_one(query)

        if not doc:
            # 兜底：从 unified_analysis_tasks 或 analysis_tasks.result 中还原报告详情
            logger.info(f"⚠️ 未在analysis_reports找到，尝试从任务中心还原: {report_id}")
            
            # 首先尝试从 unified_analysis_tasks 查询（新任务中心）
            from bson import ObjectId
            tasks_doc = None
            
            # 尝试查询 unified_analysis_tasks（需要 user_id，但这里先尝试不限制用户）
            try:
                # 先尝试通过 task_id 查询
                tasks_doc = await db.unified_analysis_tasks.find_one(
                    {"task_id": report_id},
                    {"result": 1, "task_id": 1, "task_type": 1, "task_params": 1, "created_at": 1, "completed_at": 1, "status": 1}
                )
            except Exception as e:
                logger.warning(f"⚠️ 查询 unified_analysis_tasks 失败: {e}")
            
            # 如果 unified_analysis_tasks 中没找到，尝试从 analysis_tasks 查询（旧系统）
            if not tasks_doc:
                logger.info(f"⚠️ unified_analysis_tasks 中未找到，尝试从 analysis_tasks 查询: {report_id}")
                tasks_doc = await db.analysis_tasks.find_one(
                    {"$or": [{"task_id": report_id}, {"result.analysis_id": report_id}]},
                    {"result": 1, "task_id": 1, "stock_code": 1, "created_at": 1, "completed_at": 1}
                )
            
            if not tasks_doc or not tasks_doc.get("result"):
                raise HTTPException(status_code=404, detail="报告不存在")

            r = tasks_doc["result"] or {}
            created_at = tasks_doc.get("created_at")
            updated_at = tasks_doc.get("completed_at") or created_at

            # 转换时区：数据库中是 UTC 时间，转换为 UTC+8
            created_at_tz = to_config_tz(created_at)
            updated_at_tz = to_config_tz(updated_at)

            def to_iso(x):
                if hasattr(x, "isoformat"):
                    return x.isoformat()
                return x or ""

            # 从 result 或 task_params 中提取股票代码
            task_params = tasks_doc.get("task_params", {})
            stock_symbol = (
                r.get("stock_symbol") or 
                r.get("stock_code") or 
                task_params.get("symbol") or 
                task_params.get("stock_code") or 
                task_params.get("code") or
                tasks_doc.get("stock_code", "")
            )
            stock_name = r.get("stock_name")
            if _is_invalid_stock_name(stock_symbol, stock_name):
                stock_name = get_stock_name(stock_symbol)

            reports_raw = r.get("reports", {})
            cleaned_reports = _rebuild_report_modules(r, reports_raw)
            
            report = {
                "id": tasks_doc.get("task_id", report_id),
                "analysis_id": r.get("analysis_id", ""),
                "stock_symbol": stock_symbol,
                "stock_name": stock_name,  # 🔥 添加股票名称字段
                "model_info": r.get("model_info", "Unknown"),  # 🔥 添加模型信息字段
                "analysis_date": r.get("analysis_date", ""),
                "status": r.get("status", "completed"),
                "created_at": to_iso(created_at_tz),
                "updated_at": to_iso(updated_at_tz),
                "analysts": r.get("analysts", []),
                "research_depth": r.get("research_depth", 1),
                "summary": r.get("summary", ""),
                "reports": cleaned_reports,  # 🔥 使用清理后的 reports
                "source": "unified_analysis_tasks" if tasks_doc.get("task_type") else "analysis_tasks",
                "task_id": tasks_doc.get("task_id", report_id),
                "recommendation": r.get("recommendation", ""),
                "confidence_score": r.get("confidence_score", 0.0),
                "risk_level": r.get("risk_level", "中等"),
                "key_points": r.get("key_points", []),
                "execution_time": r.get("execution_time", 0),
                "tokens_used": r.get("tokens_used", 0),
                "decision": r.get("decision", {}),  # 🔥 添加 decision 字段
                "task_type": tasks_doc.get("task_type", "")
            }

            if r.get("report_manifest"):
                report["report_manifest"] = r.get("report_manifest")
            
            # 🔥 可选字段：根据查询参数决定是否包含
            if include_state:
                report["state"] = r.get("state", {})
            if include_detailed:
                report["detailed_analysis"] = r.get("detailed_analysis", {})
        else:
            # 转换为详细格式（analysis_reports 命中）
            stock_symbol = doc.get("stock_symbol", "")
            stock_name = doc.get("stock_name")
            if _is_invalid_stock_name(stock_symbol, stock_name):
                stock_name = get_stock_name(stock_symbol)

            # 获取时间（数据库中是 UTC 时间，需要转换为 UTC+8）
            created_at = doc.get("created_at", datetime.utcnow())
            updated_at = doc.get("updated_at", datetime.utcnow())

            # 转换时区：数据库中是 UTC 时间，转换为 UTC+8
            created_at_tz = to_config_tz(created_at)
            updated_at_tz = to_config_tz(updated_at)

            reports_raw = doc.get("reports", {})
            cleaned_reports = _rebuild_report_modules(doc, reports_raw)
            
            report = {
                "id": str(doc["_id"]),
                "analysis_id": doc.get("analysis_id", ""),
                "stock_symbol": stock_symbol,
                "stock_name": stock_name,  # 🔥 添加股票名称字段
                "model_info": doc.get("model_info", "Unknown"),  # 🔥 添加模型信息字段
                "analysis_date": doc.get("analysis_date", ""),
                "status": doc.get("status", "completed"),
                "created_at": created_at_tz.isoformat() if created_at_tz else str(created_at),
                "updated_at": updated_at_tz.isoformat() if updated_at_tz else str(updated_at),
                "analysts": doc.get("analysts", []),
                "research_depth": doc.get("research_depth", 1),
                "summary": doc.get("summary", ""),
                "reports": cleaned_reports,  # 🔥 使用清理后的 reports
                "source": doc.get("source", "unknown"),
                "task_id": doc.get("task_id", ""),
                "recommendation": doc.get("recommendation", ""),
                "confidence_score": doc.get("confidence_score", 0.0),
                "risk_level": doc.get("risk_level", "中等"),
                "key_points": doc.get("key_points", []),
                "execution_time": doc.get("execution_time", 0),
                "tokens_used": doc.get("tokens_used", 0),
                "decision": doc.get("decision", {}),  # 🔥 添加 decision 字段用于格式化
                "task_type": doc.get("task_type", "")
            }

            if doc.get("report_manifest"):
                report["report_manifest"] = doc.get("report_manifest")
            
            tasks_doc = None
            task_id = doc.get("task_id", report_id)
            if task_id:
                tasks_doc = await db.unified_analysis_tasks.find_one(
                    {"task_id": task_id},
                    {"result": 1}
                )
                if not tasks_doc:
                    tasks_doc = await db.analysis_tasks.find_one(
                        {"task_id": task_id},
                        {"result": 1}
                    )

            if tasks_doc and tasks_doc.get("result"):
                result = tasks_doc["result"]
                rebuilt_from_task = _rebuild_report_modules(result, cleaned_reports)
                if len(rebuilt_from_task) > len(cleaned_reports):
                    cleaned_reports = rebuilt_from_task
                    report["reports"] = cleaned_reports

                if result.get("report_manifest"):
                    report["report_manifest"] = result.get("report_manifest")

                if include_state:
                    report["state"] = result.get("state", {})
                if include_detailed:
                    report["detailed_analysis"] = result.get("detailed_analysis", {})

            logger.info("📊 [REPORT] 保留报告原始内容返回，跳过读取时压缩")

        return {
            "success": True,
            "data": report,
            "message": "报告详情获取成功"
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ 获取报告详情失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/{report_id}/content/{module}")
async def get_report_module_content(
    report_id: str,
    module: str,
    user: dict = Depends(get_current_user)
):
    """获取报告特定模块的内容"""
    try:
        logger.info(f"🔍 获取报告模块内容: {report_id}/{module}")

        db = get_mongo_db()

        # 查询报告（支持多种ID）
        query = _build_report_query(report_id)
        doc = await db.analysis_reports.find_one(query)

        if not doc:
            raise HTTPException(status_code=404, detail="报告不存在")

        reports = doc.get("reports", {})

        if module not in reports:
            raise HTTPException(status_code=404, detail=f"模块 {module} 不存在")

        content = reports[module]

        return {
            "success": True,
            "data": {
                "module": module,
                "content": content,
                "content_type": "markdown" if isinstance(content, str) else "json"
            },
            "message": "模块内容获取成功"
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ 获取报告模块内容失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.delete("/{report_id}")
async def delete_report(
    report_id: str,
    user: dict = Depends(get_current_user)
):
    """删除报告"""
    try:
        logger.info(f"🗑️ 删除报告: {report_id}")

        db = get_mongo_db()

        # 查询报告（支持多种ID）
        query = _build_report_query(report_id)
        result = await db.analysis_reports.delete_one(query)

        if result.deleted_count == 0:
            raise HTTPException(status_code=404, detail="报告不存在")

        logger.info(f"✅ 报告删除成功: {report_id}")

        return {
            "success": True,
            "message": "报告删除成功"
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ 删除报告失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# 报告导出下载（社区版自建实现）
#
# 背景：v3.6.0 社区版分离时，导出下载端点原计划拆到 Pro 侧
# （docs/05-design/v3.0/v3.6.0-community-edition-separation-plan.md §5.2），
# 但对应模块 reports_export.py 从未落地 ⇒ 前端「下载报告」始终 404。
# 此处提供社区版实现，支持 Markdown / PDF 两种格式：
#   - Markdown：直接返回 report_exporter 生成的 .md 文本
#   - PDF：走 report_exporter 的引擎链（WeasyPrint → pdfkit → 无头 Chrome）
# 若 Pro 版重新引入导出路由，需在社区路由之前注册才能覆盖本实现
# （router_registry 先注册 COMMUNITY_ROUTERS 再注册 PRO_ROUTERS）。
# =============================================================================

async def _fetch_task_result_for_export(
    db, report_id: str, by_analysis_id: bool = False
) -> Optional[Dict[str, Any]]:
    """从任务中心（unified_analysis_tasks / analysis_tasks）取原始 result。"""
    if by_analysis_id:
        query: Dict[str, Any] = {
            "$or": [{"task_id": report_id}, {"result.analysis_id": report_id}]
        }
    else:
        query = {"task_id": report_id}

    for coll in ("unified_analysis_tasks", "analysis_tasks"):
        try:
            task_doc = await db[coll].find_one(query, {"result": 1})
        except Exception as e:
            logger.warning(f"⚠️ 导出时查询 {coll} 失败: {e}")
            continue
        if task_doc and task_doc.get("result"):
            return task_doc["result"]
    return None


async def _load_report_doc_for_export(db, report_id: str) -> Optional[Dict[str, Any]]:
    """加载用于导出的报告文档（口径与 GET /{report_id}/detail 保持一致）。

    1) 优先取 analysis_reports；
    2) 用任务原始结果补齐可能缺失的报告模块（取更全的那份）；
    3) 完全找不到时回退到任务中心还原一份最小可用文档。
    返回 None 表示报告不存在。
    """
    doc = await db.analysis_reports.find_one(_build_report_query(report_id))

    if doc:
        doc["reports"] = _rebuild_report_modules(doc, doc.get("reports", {}))
        task_id = doc.get("task_id")
        if task_id:
            result = await _fetch_task_result_for_export(db, task_id)
            if result:
                rebuilt = _rebuild_report_modules(result, doc.get("reports", {}))
                if len(rebuilt) > len(doc.get("reports") or {}):
                    doc["reports"] = rebuilt
        return doc

    # 兜底：报告只存在于任务中心
    result = await _fetch_task_result_for_export(db, report_id, by_analysis_id=True)
    if not result:
        return None

    logger.info(f"ℹ️ analysis_reports 未找到 {report_id}，已从任务中心还原用于导出")
    return {
        "analysis_id": result.get("analysis_id", report_id),
        "task_id": result.get("task_id", report_id),
        "stock_symbol": result.get("stock_symbol") or result.get("stock_code") or "",
        "stock_name": result.get("stock_name"),
        "analysis_date": result.get("analysis_date", ""),
        "analysts": result.get("analysts", []),
        "research_depth": result.get("research_depth", 1),
        "summary": result.get("summary", ""),
        "reports": _rebuild_report_modules(result),
        "decision": result.get("decision", {}),
        "status": "completed",
    }


def _build_export_filename(doc: Dict[str, Any], ext: str) -> str:
    """构造下载文件名：股票代码_股票名称_分析报告_日期.ext"""
    symbol = str(doc.get("stock_symbol") or doc.get("stock_code") or "report").strip()
    name = doc.get("stock_name")
    if _is_invalid_stock_name(symbol, name):
        name = ""
    # 移除文件名非法字符与空白
    clean_name = re.sub(r'[\\/:*?"<>|\s]+', "", str(name)) if name else ""
    date_part = str(doc.get("analysis_date") or "").strip().split(" ")[0]

    parts = [symbol]
    if clean_name:
        parts.append(clean_name)
    parts.append("分析报告")
    if date_part:
        parts.append(date_part)
    return "_".join(p for p in parts if p) + f".{ext}"


def _ascii_fallback_filename(filename: str, fallback: str) -> str:
    """为 Content-Disposition 的 `filename=` 生成纯 ASCII 兜底名。

    HTTP 头按 latin-1 编码（Starlette 会直接 `.encode("latin-1")`），
    把中文名原样塞进 `filename="..."` 会抛 UnicodeEncodeError → 500。
    因此中文真实名走 RFC 5987 的 `filename*=UTF-8''<百分号编码>`，
    这里只保留 ASCII 部分作为老客户端兜底。
    """
    ascii_name = filename.encode("ascii", "ignore").decode("ascii")
    ascii_name = re.sub(r"_+", "_", ascii_name)                     # 合并连续下划线
    ascii_name = re.sub(r"_+(\.[A-Za-z0-9]+)$", r"\1", ascii_name)  # 去掉扩展名前多余下划线
    ascii_name = ascii_name.strip("_ ")
    # 中文被全部剥掉后可能只剩 ".md" 这类无主名的形态（等于隐藏文件），回退兜底名
    if not ascii_name or ascii_name.startswith("."):
        return fallback
    return ascii_name


@router.get("/{report_id}/download")
async def download_report(
    report_id: str,
    export_format: str = Query(
        "markdown", alias="format", description="导出格式：markdown | pdf"
    ),
    user: dict = Depends(get_current_user),
):
    """导出并下载研究报告。

    - `format=markdown` → 返回 `text/markdown` 文本（.md）
    - `format=pdf`      → 返回 `application/pdf` 二进制
    """
    fmt = (export_format or "markdown").strip().lower()
    if fmt in ("md", "markdown"):
        fmt = "markdown"
    if fmt not in ("markdown", "pdf"):
        raise HTTPException(
            status_code=400,
            detail=f"不支持的导出格式：{export_format}（当前支持 markdown、pdf）",
        )

    try:
        db = get_mongo_db()
        doc = await _load_report_doc_for_export(db, report_id)
        if not doc:
            raise HTTPException(status_code=404, detail="报告不存在")

        from ..utils.report_exporter import report_exporter

        ext = "md" if fmt == "markdown" else "pdf"
        filename = _build_export_filename(doc, ext)
        # filename= 必须是纯 ASCII（HTTP 头按 latin-1 编码），
        # 中文真实名走 RFC 5987 的 filename*=UTF-8''，两者并存以兼容各浏览器。
        ascii_name = _ascii_fallback_filename(filename, f"report.{ext}")
        disposition = (
            f"attachment; filename=\"{ascii_name}\"; "
            f"filename*=UTF-8''{quote(filename, safe='')}"
        )

        logger.info(f"⬇️ 导出报告: {report_id} 格式={fmt} 文件名={filename}")

        if fmt == "markdown":
            content = report_exporter.generate_markdown_report(doc)
            return Response(
                content=content.encode("utf-8"),
                media_type="text/markdown; charset=utf-8",
                headers={"Content-Disposition": disposition},
            )

        pdf_bytes = report_exporter.generate_pdf_report(doc)
        return Response(
            content=pdf_bytes,
            media_type="application/pdf",
            headers={"Content-Disposition": disposition},
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ 导出报告失败: {report_id}/{fmt} - {e}")
        raise HTTPException(status_code=500, detail=f"导出报告失败: {e}")
