"""
Wind 万得数据源适配器

基于 wind_client.WindMCPClient 实现 DataSourceAdapter 接口。
覆盖 Wind MCP skill 支持的全部市场：A股 / 港股 / 美股 / 基金 / 指数 / 债券。

接口实现策略（按调用成本设计）：
- get_kline / get_daily_ohlc / get_news / find_latest_trade_date:
    ✅ 实现且全市场支持 —— 单标的、小数据量、Wind 数据质量最高的场景
- get_realtime_quotes_batch(codes): 扩展方法，按标的集合批量快照（≤50/批）
- get_stock_list / get_daily_basic / get_realtime_quotes:
    ⏸ 刻意返回 None —— 全市场批量类接口按次消耗大量 Wind 积分，
    由 AKShare/Baostock 等免费源承担，manager 会自动 fallback

代码转换约定（_to_windcode）：
- 含 "." 的代码（600519.SH / 00700.HK / AAPL.O / 005827.OF）→ 原样透传
- 6 位纯数字 → 按板块规则补 A 股后缀（指数请务必带后缀传入，如 000300.SH）
- 5 位纯数字 → 港股（.HK）
- 其他（字母 ticker、中文名）→ 原样透传，交由 Wind 解析

优先级：默认 2（与 AKShare 同级），推荐通过前端「数据源管理」或数据库
统一配置调整（Wind 按积分计费，通常不宜设为全市场行情的默认源）。
"""

import logging
import math
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import pandas as pd

from .base import DataSourceAdapter
from .wind_client import WindMCPClient, WindMCPError, get_wind_client

logger = logging.getLogger(__name__)

# A 股 6 位代码 → 交易所后缀规则（前缀 → 后缀）
_A_SHARE_PREFIX_RULES = {
    # 上交所：主板 / 科创板 / B股
    "600": ".SH", "601": ".SH", "603": ".SH", "605": ".SH",
    "688": ".SH", "689": ".SH", "900": ".SH",
    # 深交所：主板 / 创业板 / B股
    "000": ".SZ", "001": ".SZ", "002": ".SZ", "003": ".SZ",
    "300": ".SZ", "301": ".SZ", "302": ".SZ", "200": ".SZ",
    # 北交所
    "430": ".BJ", "830": ".BJ", "831": ".BJ", "832": ".BJ",
    "833": ".BJ", "834": ".BJ", "835": ".BJ", "836": ".BJ",
    "837": ".BJ", "838": ".BJ", "839": ".BJ",
    "870": ".BJ", "871": ".BJ", "872": ".BJ", "873": ".BJ", "920": ".BJ",
}

# adapter period → Wind K线 period
_KLINE_PERIOD_MAP = {
    "day": "1d", "week": "1w", "month": "1mo",
    "5m": "5min", "15m": "15min", "30m": "30min", "60m": "60min",
}

# 每个交易日约含多少根该周期 K线（用于推算 begin_date）
_BARS_PER_DAY = {"1d": 1, "1w": 0.2, "1mo": 0.05,
                 "5min": 48, "15min": 16, "30min": 8, "60min": 4}

# 复权方式 → Wind aftype（0=前复权, 1=后复权, 2=不复权）
_ADJ_TO_AFTYPE = {None: "2", "qfq": "0", "hfq": "1"}


class WindAdapter(DataSourceAdapter):
    """Wind 万得数据源适配器（MCP skill 接入）"""

    def __init__(self, client: Optional[WindMCPClient] = None):
        super().__init__()
        self._client = client or get_wind_client()

    # ==================== 基础属性 ====================

    @property
    def name(self) -> str:
        return "wind"

    def _get_default_priority(self) -> int:
        return 2  # 数字越大优先级越高；默认与 AKShare 同级，可由数据库配置覆盖

    def is_available(self) -> bool:
        """静态检查：CLI 与 node 存在即视为可用（不发起真实调用，不消耗积分）"""
        try:
            return self._client.is_available()
        except Exception:
            return False

    # ==================== 代码转换 ====================

    @staticmethod
    def _to_windcode(code: str) -> str:
        """把项目内部代码转换为 Wind windcode（详见模块 docstring）"""
        code = (code or "").strip()
        if not code:
            return code
        if "." in code:            # 已带后缀，透传
            return code
        if re.fullmatch(r"\d{6}", code):   # 6 位纯数字 → A 股
            return code + _A_SHARE_PREFIX_RULES.get(code[:3], ".SH")
        if re.fullmatch(r"\d{5}", code):   # 5 位纯数字 → 港股
            return code + ".HK"
        return code                # 字母 ticker / 中文名等，透传由 Wind 解析

    # ==================== K线（全市场） ====================

    def get_kline(self, code: str, period: str = "day", limit: int = 120,
                  adj: Optional[str] = None) -> Optional[List[Dict[str, Any]]]:
        """
        获取K线，返回按时间正序的 [{time, open, high, low, close, volume, amount}]。
        volume 单位为股，amount 单位为元（Wind 原始口径）。
        """
        if not self.is_available():
            return None
        windcode = self._to_windcode(code)
        wind_period = _KLINE_PERIOD_MAP.get(period, "1d")
        aftype = _ADJ_TO_AFTYPE.get(adj, "2")

        try:
            end = datetime.now()
            per_day = _BARS_PER_DAY.get(wind_period, 1)
            # 自然日 ≈ 1.5 × 交易日，另加缓冲
            lookback_days = int(math.ceil(max(1, limit) / max(per_day, 0.05)) * 1.5) + 5
            begin = end - timedelta(days=lookback_days)
            df = self._client.call_table("stock_data", "get_stock_kline", {
                "windcode": windcode,
                "begin_date": begin.strftime("%Y-%m-%d"),
                "end_date": end.strftime("%Y-%m-%d"),
                "period": wind_period,
                "count": -int(max(1, limit)),   # 负数 = 从结束日期往前取 N 条
                "aftype": aftype,
                "issusp": "1",                  # 含停牌
            })
        except WindMCPError as e:
            logger.warning(f"Wind: get_kline({code}) 失败: {e}")
            return None
        except Exception as e:
            logger.error(f"Wind: get_kline({code}) 异常: {e}")
            return None

        if df is None or df.empty:
            return None

        # Wind K线列名: TIME / OPEN / MATCH(收盘) / HIGH / LOW / TURNOVER(成交额) / VOLUME(成交量)
        col_map = {"OPEN": "open", "MATCH": "close", "HIGH": "high",
                   "LOW": "low", "VOLUME": "volume", "TURNOVER": "amount"}
        items: List[Dict[str, Any]] = []
        for _, row in df.iterrows():
            try:
                items.append({
                    "time": _normalize_iso_time(str(row.get("TIME", ""))),
                    **{dst: _to_float(row.get(src)) for src, dst in col_map.items()},
                })
            except Exception:
                continue
        items.sort(key=lambda x: x.get("time") or "")
        return items or None

    # ==================== 新闻与公告（全市场） ====================

    def get_news(self, code: str, days: int = 2, limit: int = 50,
                 include_announcements: bool = True) -> Optional[List[Dict[str, Any]]]:
        """获取新闻/公告，返回 [{title, source, time, url, type}]"""
        if not self.is_available():
            return None
        windcode = self._to_windcode(code)
        days = max(1, days)
        items: List[Dict[str, Any]] = []

        try:
            if include_announcements:
                ann = self._client.call_items("financial_docs", "get_company_announcements", {
                    "query": f"查询{windcode}近{days}天的公告",
                    "top_k": min(limit, 20),
                })
                for it in ann:
                    items.append({
                        "title": (it.get("title") or (it.get("content") or "")[:60] or "").strip(),
                        "source": "wind",
                        "time": str(it.get("date") or ""),
                        "url": it.get("url") or "",
                        "type": "announcement",
                    })
        except WindMCPError as e:
            logger.warning(f"Wind: get_company_announcements({code}) 失败: {e}")
        except Exception as e:
            logger.error(f"Wind: get_company_announcements({code}) 异常: {e}")

        try:
            remaining = max(0, limit - len(items))
            if remaining > 0:
                news = self._client.call_items("financial_docs", "get_financial_news", {
                    "query": f"查询{windcode}近{days}天的新闻",
                    "top_k": min(remaining, 20),
                })
                for it in news:
                    items.append({
                        "title": (it.get("title") or (it.get("content") or "")[:60] or "").strip(),
                        "source": "wind",
                        "time": str(it.get("date") or ""),
                        "url": it.get("url") or "",
                        "type": "news",
                    })
        except WindMCPError as e:
            logger.warning(f"Wind: get_financial_news({code}) 失败: {e}")
        except Exception as e:
            logger.error(f"Wind: get_financial_news({code}) 异常: {e}")

        return items[:limit] or None

    # ==================== 交易日历 ====================

    def find_latest_trade_date(self) -> Optional[str]:
        """通过行情快照的「最新交易日」字段获取最新交易日（YYYYMMDD），带缓存"""
        if not self.is_available():
            return None

        # 先读缓存
        try:
            from app.services.trade_date_cache_service import get_trade_date_cache_service
            cached = get_trade_date_cache_service().get_cached_trade_date_sync("wind")
            if cached:
                return cached
        except Exception as e:
            logger.debug(f"Wind: 交易日缓存读取失败: {e}")

        try:
            df = self._client.call_table("stock_data", "get_stock_price_indicators", {
                "windcode": "000001.SH",   # 平安银行，成交稳定的沪市标的
            })
            if df is not None and not df.empty and "最新交易日" in df.columns:
                latest = str(df.iloc[0]["最新交易日"]).replace("-", "")[:8]
                if re.fullmatch(r"\d{8}", latest):
                    try:
                        from app.services.trade_date_cache_service import get_trade_date_cache_service
                        get_trade_date_cache_service().set_cached_trade_date_sync("wind", latest)
                    except Exception as e:
                        logger.debug(f"Wind: 交易日缓存写入失败: {e}")
                    return latest
        except WindMCPError as e:
            logger.warning(f"Wind: find_latest_trade_date 失败: {e}")
        except Exception as e:
            logger.error(f"Wind: find_latest_trade_date 异常: {e}")
        return None

    # ==================== 刻意不实现的批量接口 ====================
    # 全市场批量类数据按次消耗大量 Wind 积分（5000+ 标的 × 单批 50 上限），
    # 交由免费源（AKShare/Baostock/Tushare）承担，此处返回 None 由 manager fallback。

    def get_stock_list(self) -> Optional[pd.DataFrame]:
        """不实现：全市场股票清单请走 AKShare/Baostock（见模块 docstring）"""
        return None

    def get_daily_basic(self, trade_date: str) -> Optional[pd.DataFrame]:
        """不实现：全市场每日指标截面请走 Tushare/AKShare（见模块 docstring）"""
        return None

    def get_realtime_quotes(self) -> Optional[Dict[str, Dict[str, Optional[float]]]]:
        """不实现全市场快照；指定标的集合请用 get_realtime_quotes_batch()"""
        return None

    # ==================== 扩展方法（超出抽象接口，供技能包/Agent 层直接调用） ====================

    def get_realtime_quotes_batch(
        self, codes: List[str], indexes: str = "最新成交价,涨跌幅,成交额,总市值2,流通市值,市盈率(TTM),市净率(LF)"
    ) -> Dict[str, Dict[str, Optional[float]]]:
        """
        批量实时快照（如自选股/持仓），自动按 50 个/批分批。
        返回 {原始code: {字段名: 值}}，失败或字段缺失为 None。
        """
        if not self.is_available() or not codes:
            return {}
        result: Dict[str, Dict[str, Optional[float]]] = {}
        unique_codes = list(dict.fromkeys(codes))
        for i in range(0, len(unique_codes), 50):
            batch = unique_codes[i:i + 50]
            windcodes = [self._to_windcode(c) for c in batch]
            try:
                df = self._client.call_table("stock_data", "get_stock_price_indicators", {
                    "windcode": ",".join(windcodes),
                    "indexes": indexes,
                })
            except WindMCPError as e:
                logger.warning(f"Wind: realtime batch {i//50} 失败: {e}")
                continue
            if df is None or df.empty or "Wind代码" not in df.columns:
                continue
            # 建立双向映射：Wind 返回的代码可能与传入格式有前导零差异
            # （如传入 00700.HK，返回 0700.HK），故同时登记精确与去零两种键
            windcode_to_code: Dict[str, str] = {}
            for c in batch:
                wc = self._to_windcode(c)
                windcode_to_code.setdefault(wc, c)
                windcode_to_code.setdefault(_strip_leading_zeros(wc), c)
            for _, row in df.iterrows():
                raw = str(row.get("Wind代码"))
                key = windcode_to_code.get(raw) or windcode_to_code.get(
                    _strip_leading_zeros(raw))
                if not key:
                    continue
                result[key] = {
                    col: _to_float(row.get(col)) for col in df.columns if col != "Wind代码"
                }
        return result

    def get_price_indicators(self, windcodes: List[str], indexes: str) -> pd.DataFrame:
        """任意指标集的截面查询（indexes 取值见 wind-mcp-skill/references/stock-indicators.md）"""
        if not self.is_available():
            return pd.DataFrame()
        return self._client.call_table("stock_data", "get_stock_price_indicators", {
            "windcode": ",".join(windcodes[:50]),
            "indexes": indexes,
        })

    def get_fundamentals(self, question: str) -> Dict[str, Any]:
        """自然语言财务/估值查询（如『贵州茅台(600519.SH)最新ROE和净利润』）"""
        return self._client.call("stock_data", "get_stock_fundamentals", {"question": question})

    def get_stock_events(self, question: str) -> Dict[str, Any]:
        """结构化事件查询（分红/增减持/解禁/诉讼等）"""
        return self._client.call("stock_data", "get_stock_events", {"question": question})

    def search_stocks(self, question: str) -> Dict[str, Any]:
        """自然语言选股筛选（如『市值超500亿且连续5日上涨的股票』）"""
        return self._client.call("stock_data", "search_stocks", {"question": question})

    def query_economic_indicator(self, query: str) -> Dict[str, Any]:
        """EDB 宏观/行业/汇率指标查询"""
        return self._client.call("economic_data", "query_economic_indicator_data", {"query": query})

    # 基金 / 指数 / 债券 域工具（server_type 分别为 fund_data / index_data / bond_data）
    def call_fund(self, tool_name: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """基金域工具透传（工具清单见 wind-mcp-skill/references/fund.md）"""
        return self._client.call("fund_data", tool_name, params)

    def call_index(self, tool_name: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """指数域工具透传（工具清单见 wind-mcp-skill/references/index.md）"""
        return self._client.call("index_data", tool_name, params)

    def call_bond(self, tool_name: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """债券域工具透传（工具清单见 wind-mcp-skill/references/bond.md）"""
        return self._client.call("bond_data", tool_name, params)


# ==================== 模块级辅助 ====================

def _to_float(value: Any) -> Optional[float]:
    """安全转 float；空值/非数值（'--' 等）返回 None"""
    if value is None:
        return None
    try:
        f = float(value)
        return f if f == f else None   # 过滤 NaN
    except (TypeError, ValueError):
        return None


def _strip_leading_zeros(windcode: str) -> str:
    """'0700.HK' → '700.HK'；用于兼容 Wind 返回代码与传入代码的前导零差异"""
    num, sep, suffix = str(windcode).partition(".")
    stripped = num.lstrip("0") or num
    return stripped + sep + suffix if sep else stripped


def _normalize_iso_time(iso: str) -> str:
    """'2026-09-22T00:00:00.000+08:00' → '2026-09-22'；带真实时刻则保留到分钟"""
    iso = (iso or "").strip()
    if "T" not in iso:
        return iso[:10] if len(iso) >= 10 else iso
    date_part, _, time_part = iso.partition("T")
    time_part = time_part.split(".")[0].split("+")[0]
    if time_part.startswith("00:00"):
        return date_part
    return f"{date_part} {time_part[:5]}"
