"""
港股/美股行情预热缓存服务

背景
----
K 线接口 `app/routers/stocks.py:963-985` 在 `_detect_market_and_code` 之后立即分流：
港美股直接走 `ForeignStockService`，A 股那套「local → MongoDB 缓存 → 外部降级链」
港美股一步都进不去。后果：
1. 每次分析都实时打外部 API（深度分析累计几十次调用）
2. 数据检验（读 `stock_daily_quotes_hk/us` 等专用集合）因集合不存在而必然失败

本服务职责
----------
1. 构建预热标的池 `hk_us_preheat_universe` = **指数成分股 ∪ 自选股**
2. 按主备双源（默认 Yahoo 主 / Wind 备）拉取日线、基础信息、财务，
   写入 `stock_daily_quotes_hk/us`、`stock_basic_info_hk/us`、`stock_financial_data_hk/us`
3. 幂等增量：唯一索引 + upsert，重复执行不产生重复数据

关键约束（实测）
----------------
- Wind `search_stocks` 单次最多返回 100 条，标普500(503)/港股通(500+) 会被截断
  → 采用「达上限即细分」的递归分批策略（美股按 A-Z，港股按 0-9）
- 美股后缀有两种：`.O`(纳斯达克) / `.N`(纽交所)，必须使用 Wind 返回值，不可自拼
- Wind 传无后缀 `AAPL` 返回空，必须带后缀
- 自选股存储在 `users.favorite_stocks` 内嵌数组（字段含 `market`），非独立集合

设计文档：docs/design/hk-us-preheat-cache.md
"""

import argparse
import asyncio
import logging
import re
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

from pymongo import UpdateOne

from app.core.config import settings
from app.core.database import get_mongo_db

logger = logging.getLogger("webapi")


# ==================== 常量 ====================

# 预热覆盖的指数定义：索引名 → 元信息
# market 决定细分字符集与目标集合；code 仅用于日志
INDEX_DEFS: Dict[str, Dict[str, str]] = {
    "恒生指数":    {"market": "HK", "code": "HSI.HI"},
    "恒生科技":    {"market": "HK", "code": "HSTECH.HI"},
    "港股通":      {"market": "HK", "code": ""},
    "纳斯达克100": {"market": "US", "code": "NDX.GI"},
    "标普500":     {"market": "US", "code": "SPX.GI"},
}

# search_stocks 单次返回上限（实测）。达到该值即认为可能被截断，需继续细分。
_SEARCH_PAGE_LIMIT = 100
# 最大细分深度，防止极端情况下无限递归
_MAX_SPLIT_DEPTH = 3

_US_CHARS = list("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
_HK_CHARS = list("0123456789")

UNIVERSE_COLLECTION = "hk_us_preheat_universe"

# 自选股 market 字段值 → 内部市场标识
_FAVORITE_MARKET_MAP = {
    "港股": "HK", "hk": "HK", "hongkong": "HK", "香港": "HK",
    "美股": "US", "us": "US", "美国": "US",
}


def _coll_names(market: str) -> Dict[str, str]:
    """按市场返回目标集合名"""
    if (market or "").upper() == "HK":
        return {
            "daily": "stock_daily_quotes_hk",
            "basic": "stock_basic_info_hk",
            "financial": "stock_financial_data_hk",
        }
    return {
        "daily": "stock_daily_quotes_us",
        "basic": "stock_basic_info_us",
        "financial": "stock_financial_data_us",
    }


# ==================== 代码转换 ====================

def to_internal_symbol(windcode: str, market: str) -> str:
    """
    Wind 带后缀代码 → 项目内部 symbol（与 stocks.py `_detect_market_and_code` 一致）
        0002.HK → 00002   （港股 5 位数字）
        AAPL.O  → AAPL    （美股裸 ticker）
    """
    code = (windcode or "").split(".")[0].strip()
    if (market or "").upper() == "HK":
        return code.zfill(5)
    return code


def to_full_symbol(windcode: str, market: str) -> str:
    """项目内部 full_symbol"""
    if (market or "").upper() == "HK":
        return f"{to_internal_symbol(windcode, market)}.HK"
    return (windcode or "").strip()


def to_yahoo_ticker(windcode: str, market: str) -> str:
    """
    Wind 带后缀代码 → Yahoo Finance ticker
        0002.HK → 0002.HK   （Wind 已是 4 位，与 Yahoo 一致）
        AAPL.O  → AAPL
    """
    if (market or "").upper() == "HK":
        return (windcode or "").strip()
    return (windcode or "").split(".")[0].strip()


def _to_float(value: Any) -> Optional[float]:
    """安全转 float；空值 / NaN / '--' 返回 None"""
    if value is None:
        return None
    try:
        f = float(value)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


# ==================== 主服务 ====================

class HKUSPreheatService:
    """港美股行情预热缓存服务"""

    def __init__(self) -> None:
        self._wind = None  # 延迟初始化（避免无 skill 时报错）

    # ---------- 懒加载依赖 ----------

    def _wind_adapter(self):
        if self._wind is None:
            from app.services.data_sources.wind_adapter import WindAdapter
            self._wind = WindAdapter()
        return self._wind

    def _wind_client(self):
        from app.services.data_sources.wind_client import get_wind_client
        return get_wind_client()

    # ==================== 一、Universe 构建 ====================

    async def refresh_universe(self, force: bool = False) -> Dict[str, int]:
        """
        刷新预热标的池 = 指数成分股 ∪ 自选股。

        force=False 时，若某指数清单已存在且未超过 UNIVERSE_REFRESH_DAYS，则跳过重查。
        返回 {来源: 数量} 统计。
        """
        db = get_mongo_db()
        coll = db[UNIVERSE_COLLECTION]
        stats: Dict[str, int] = {}

        # ---- 1. 指数成分股 ----
        indexes = [s.strip() for s in (settings.HK_US_PREHEAT_INDEXES or "").split(",") if s.strip()]
        refresh_days = int(settings.HK_US_PREHEAT_UNIVERSE_REFRESH_DAYS or 7)
        for idx_name in indexes:
            meta = INDEX_DEFS.get(idx_name)
            if not meta:
                logger.warning(f"⚠️ 未知指数 {idx_name}，跳过（可用: {list(INDEX_DEFS)}）")
                continue
            source_tag = f"index:{idx_name}"

            if not force and await self._index_fresh(coll, source_tag, refresh_days):
                n = await coll.count_documents({"sources": source_tag, "enabled": True})
                stats[source_tag] = n
                logger.info(f"⏭️ 指数 {idx_name} 清单未过期（{n} 只），跳过重查")
                continue

            members = await asyncio.to_thread(self._fetch_index_members, idx_name, meta["market"])
            if not members:
                logger.warning(f"⚠️ 指数 {idx_name} 未取到任何成分股")
                stats[source_tag] = 0
                continue

            ops = []
            now = datetime.now()
            for m in members:
                wc, name = m["windcode"], m.get("name", "")
                ops.append(UpdateOne(
                    {"market": meta["market"], "symbol": to_internal_symbol(wc, meta["market"])},
                    {
                        "$set": {
                            "windcode": wc,
                            "yahoocode": to_yahoo_ticker(wc, meta["market"]),
                            "full_symbol": to_full_symbol(wc, meta["market"]),
                            "name": name,
                            "enabled": True,
                            "updated_at": now,
                        },
                        "$setOnInsert": {
                            "symbol": to_internal_symbol(wc, meta["market"]),
                            "market": meta["market"],
                            "added_at": now,
                        },
                        "$addToSet": {"sources": source_tag},
                    },
                    upsert=True,
                ))
            if ops:
                await coll.bulk_write(ops, ordered=False)
            stats[source_tag] = len(members)
            logger.info(f"✅ 指数 {idx_name} 成分股写入 {len(members)} 只")

        # ---- 2. 自选股并集 ----
        fav_count = await self._merge_favorites(coll)
        stats["favorite"] = fav_count

        total = await coll.count_documents({"enabled": True})
        stats["total"] = total
        logger.info(f"📊 Universe 刷新完成，合计 {total} 只：{stats}")
        return stats

    async def _index_fresh(self, coll, source_tag: str, days: int) -> bool:
        """判断某指数清单是否新鲜（存在且有更新记录、且未超过 days 天）"""
        doc = await coll.find_one({"sources": source_tag}, {"updated_at": 1}, sort=[("updated_at", -1)])
        if not doc or not doc.get("updated_at"):
            return False
        updated = doc["updated_at"]
        if isinstance(updated, str):
            try:
                updated = datetime.fromisoformat(updated)
            except ValueError:
                return False
        return (datetime.now() - updated) < timedelta(days=days)

    async def _merge_favorites(self, coll) -> int:
        """
        把 users.favorite_stocks 里的港美股自选股并入 universe。

        注意：自选股存储为 `users` 集合的内嵌数组（非独立集合），
        字段含 `stock_code` / `stock_name` / `market`。
        """
        db = get_mongo_db()
        ops = []
        now = datetime.now()
        seen = 0
        async for user in db.users.find({}, {"favorite_stocks": 1}):
            for fav in (user.get("favorite_stocks") or []):
                market = _FAVORITE_MARKET_MAP.get(str(fav.get("market") or "").strip().lower()) \
                    or _FAVORITE_MARKET_MAP.get(str(fav.get("market") or "").strip())
                if not market:
                    continue  # 只处理港美股；A 股由既有 favorites_data_sync 负责
                raw_code = str(fav.get("stock_code") or "").strip()
                if not raw_code:
                    continue
                # 自选股存的是内部格式（00700 / AAPL），补出 Wind 格式
                if market == "HK":
                    # 港股 Wind 用 4 位（00700 → 0700.HK）
                    digits = re.sub(r"\D", "", raw_code)
                    windcode = f"{digits.zfill(5)[-4:]}.HK" if digits else raw_code
                else:
                    windcode = raw_code
                symbol = to_internal_symbol(windcode, market)
                ops.append(UpdateOne(
                    {"market": market, "symbol": symbol},
                    {
                        "$set": {
                            "windcode": windcode,
                            "yahoocode": to_yahoo_ticker(windcode, market),
                            "full_symbol": to_full_symbol(windcode, market),
                            "name": fav.get("stock_name") or "",
                            "enabled": True,
                            "updated_at": now,
                        },
                        "$setOnInsert": {
                            "symbol": symbol, "market": market, "added_at": now,
                        },
                        "$addToSet": {"sources": "favorite"},
                    },
                    upsert=True,
                ))
                seen += 1
        if ops:
            await coll.bulk_write(ops, ordered=False)
        logger.info(f"⭐ 自选股并入 {seen} 条（去重后见 universe 统计）")
        return seen

    # ---------- 指数成分股反查（含递归分批） ----------

    def _fetch_index_members(self, index_name: str, market: str) -> List[Dict[str, str]]:
        """同步：反查指数成分股，自动处理 100 条上限"""
        chars = _HK_CHARS if market == "HK" else _US_CHARS
        collected: Dict[str, Dict[str, str]] = {}
        self._fetch_recursive(index_name, "", chars, collected, 0)
        return list(collected.values())

    def _fetch_recursive(self, index_name: str, prefix: str,
                         chars: Sequence[str], out: Dict[str, Dict[str, str]], depth: int) -> None:
        """递归分批：返回数达到上限（可能被截断）时，按字符集继续细分"""
        if prefix:
            question = f"{index_name}成分股中代码以{prefix}开头的股票"
        else:
            question = f"{index_name}成分股"

        rows = self._search_members(question, index_name)
        if len(rows) < _SEARCH_PAGE_LIMIT or depth >= _MAX_SPLIT_DEPTH:
            for wc, name in rows:
                out.setdefault(wc, {"windcode": wc, "name": name})
            return
        for c in chars:
            self._fetch_recursive(index_name, prefix + c, chars, out, depth + 1)

    def _search_members(self, question: str, index_name: str = "") -> List[Tuple[str, str]]:
        """调用 Wind search_stocks，返回 [(windcode, name)]"""
        try:
            df = self._wind_client().call_table("stock_data", "search_stocks", {"question": question})
        except Exception as e:
            logger.warning(f"⚠️ search_stocks('{question}') 失败: {e}")
            return []
        if df is None or getattr(df, "empty", True):
            return []

        # 列名在不同指数下不一致：优先「指数成份代码」，兼容「Wind代码」
        code_col = None
        for cand in ("指数成份代码", "Wind代码", "代码", "windcode"):
            if cand in df.columns:
                code_col = cand
                break
        if not code_col:
            logger.warning(f"⚠️ search_stocks 返回无法识别代码列: {list(df.columns)}")
            return []

        # ⚠️ Wind 返回的列名语义与内容错位：个股简称可能落在「指数成份简称」
        # 或「证券简称」，而另一列往往装的是指数名本身。故按序取第一个
        # 非空且不等于指数名的值。
        name_cols = [c for c in ("指数成份简称", "证券简称", "名称", "name") if c in df.columns]

        out: List[Tuple[str, str]] = []
        for _, row in df.iterrows():
            wc = str(row.get(code_col) or "").strip()
            # 过滤表头残留 / 空值
            if not wc or wc in ("None", "nan") or not re.search(r"[0-9A-Za-z]", wc):
                continue
            nm = ""
            for c in name_cols:
                v = str(row.get(c) or "").strip()
                if v and v not in ("None", "nan") and v != index_name:
                    nm = v
                    break
            out.append((wc, nm))
        return out

    # ==================== 二、日线同步 ====================

    async def sync_daily(self, symbols: Optional[List[str]] = None,
                         lookback_days: Optional[int] = None,
                         market: Optional[str] = None,
                         progress_every: int = 50) -> Dict[str, Any]:
        """
        同步港美股日线。symbols 为空则处理 universe 中全部（可按 market 过滤 HK/US）。

        返回统计：{total, ok, skipped, failed, rows, failed_symbols, elapsed}
        """
        db = get_mongo_db()
        lookback = int(lookback_days or settings.HK_US_PREHEAT_LOOKBACK_DAYS or 365)
        targets = await self._load_targets(symbols, market)

        stats: Dict[str, Any] = {
            "total": len(targets), "ok": 0, "skipped": 0,
            "failed": 0, "rows": 0, "failed_symbols": [],
        }
        started = datetime.now()
        sleep_s = float(settings.HK_US_PREHEAT_BATCH_SLEEP_SECONDS or 0)

        for i, t in enumerate(targets, 1):
            market, symbol = t["market"], t["symbol"]
            coll = db[_coll_names(market)["daily"]]
            try:
                limit = await self._incremental_limit(coll, symbol, lookback)
                if limit <= 0:
                    stats["skipped"] += 1
                    continue
                items, source = await self._fetch_kline(
                    t["windcode"], market, t["yahoocode"], limit)
                if not items:
                    stats["failed"] += 1
                    stats["failed_symbols"].append(f"{market}:{symbol}")
                    continue
                n = await self._upsert_daily(coll, items, t, source)
                stats["rows"] += n
                stats["ok"] += 1
            except Exception as e:
                stats["failed"] += 1
                stats["failed_symbols"].append(f"{market}:{symbol}")
                logger.warning(f"⚠️ 日线同步失败 {market}:{symbol}: {e}")

            if sleep_s:
                await asyncio.sleep(sleep_s)
            if progress_every and i % progress_every == 0:
                logger.info(f"  …日线进度 {i}/{len(targets)}（成功{stats['ok']} 跳过{stats['skipped']} 失败{stats['failed']}）")

        stats["elapsed"] = round((datetime.now() - started).total_seconds(), 1)
        logger.info(f"✅ 日线同步完成: {stats}")
        return stats

    async def _load_targets(self, symbols: Optional[List[str]],
                            market: Optional[str] = None) -> List[Dict[str, str]]:
        """从 universe 读取目标（可按 market 过滤、按内部 symbol 过滤）"""
        db = get_mongo_db()
        q: Dict[str, Any] = {"enabled": True}
        if market:
            q["market"] = market.upper()
        if symbols:
            q["symbol"] = {"$in": [s.strip() for s in symbols if s.strip()]}
        out = []
        async for d in db[UNIVERSE_COLLECTION].find(q):
            if d.get("windcode"):
                out.append({
                    "market": d.get("market") or "HK",
                    "symbol": d.get("symbol"),
                    "windcode": d.get("windcode"),
                    "yahoocode": d.get("yahoocode") or d.get("windcode"),
                })
        return out

    async def _incremental_limit(self, coll, symbol: str, default_lookback: int) -> int:
        """
        计算本次需要拉取的自然日窗口。
        返回 0 表示已是最新，无需更新。
        """
        doc = await coll.find_one({"symbol": symbol}, {"trade_date": 1}, sort=[("trade_date", -1)])
        if not doc or not doc.get("trade_date"):
            return default_lookback
        try:
            latest = datetime.strptime(str(doc["trade_date"])[:10], "%Y-%m-%d").date()
        except ValueError:
            return default_lookback
        gap = (date.today() - latest).days
        if gap <= 0:
            return 0
        return min(max(gap + 5, 5), default_lookback)

    # ---------- 双源取数 ----------

    async def _fetch_kline(self, windcode: str, market: str, yahoocode: str,
                           limit: int) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
        """主备双源取日线：默认 Yahoo 主 / Wind 备（由配置决定顺序）"""
        primary = (settings.HK_US_PREHEAT_PRIMARY_SOURCE or "yahoo").strip().lower()
        order = ["wind", "yahoo"] if primary == "wind" else ["yahoo", "wind"]

        for src in order:
            try:
                items = await asyncio.to_thread(
                    self._fetch_kline_sync, src, windcode, market, yahoocode, limit)
            except Exception as e:
                logger.warning(f"⚠️ {src} 取 {windcode} 日线异常: {e}")
                continue
            if items:
                return items, src
        return None, None

    def _fetch_kline_sync(self, src: str, windcode: str, market: str,
                          yahoocode: str, limit: int) -> Optional[List[Dict[str, Any]]]:
        if src == "yahoo":
            return self._yahoo_kline(yahoocode, limit)
        if src == "wind":
            return self._wind_kline(windcode, limit)
        return None

    @staticmethod
    def _yahoo_kline(yahoocode: str, limit_natural_days: int) -> Optional[List[Dict[str, Any]]]:
        import yfinance as yf
        end = datetime.now()
        begin = end - timedelta(days=int(limit_natural_days) + 10)
        hist = yf.Ticker(yahoocode).history(
            start=begin.strftime("%Y-%m-%d"),
            end=end.strftime("%Y-%m-%d"),
            auto_adjust=False,
        )
        if hist is None or hist.empty:
            return None
        out = []
        for idx, row in hist.iterrows():
            try:
                d = idx.strftime("%Y-%m-%d")
            except Exception:
                continue
            out.append({
                "date": d,
                "open": _to_float(row.get("Open")),
                "high": _to_float(row.get("High")),
                "low": _to_float(row.get("Low")),
                "close": _to_float(row.get("Close")),
                "volume": _to_float(row.get("Volume")),
            })
        return out or None

    def _wind_kline(self, windcode: str, limit_natural_days: int) -> Optional[List[Dict[str, Any]]]:
        # 自然日 → 交易日近似（A/HK 约 0.68，美股约 0.69），加缓冲
        bars = max(5, int(int(limit_natural_days) * 0.72) + 5)
        items = self._wind_adapter().get_kline(windcode, period="day", limit=bars)
        if not items:
            return None
        out = []
        for it in items:
            t = str(it.get("time") or "")[:10]
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", t):
                continue
            out.append({
                "date": t,
                "open": it.get("open"), "high": it.get("high"),
                "low": it.get("low"), "close": it.get("close"),
                "volume": it.get("volume"), "amount": it.get("amount"),
            })
        return out or None

    async def _upsert_daily(self, coll, items: List[Dict[str, Any]],
                            target: Dict[str, str], source: str) -> int:
        """幂等写入日线（唯一键 symbol+trade_date+data_source+period）"""
        market, symbol = target["market"], target["symbol"]
        full_symbol = to_full_symbol(target["windcode"], market)
        now = datetime.now()

        # 按日期升序，用于计算 change / pct_chg
        items = sorted(items, key=lambda x: x.get("date") or "")
        # 取库中最近一条作为增量首日的前收
        prev_close: Optional[float] = None
        last = await coll.find_one({"symbol": symbol}, {"close": 1}, sort=[("trade_date", -1)])
        if last:
            prev_close = _to_float(last.get("close"))

        ops = []
        for it in items:
            close = _to_float(it.get("close"))
            if close is None:
                continue
            change = round(close - prev_close, 6) if prev_close is not None else None
            pct = round(change / prev_close * 100, 4) if (change is not None and prev_close) else None
            ops.append(UpdateOne(
                {
                    "symbol": symbol,
                    "trade_date": it["date"],
                    "data_source": source,
                    "period": "daily",
                },
                {
                    "$set": {
                        "code": symbol, "full_symbol": full_symbol, "market": market,
                        "open": _to_float(it.get("open")), "high": _to_float(it.get("high")),
                        "low": _to_float(it.get("low")), "close": close,
                        "volume": _to_float(it.get("volume")),
                        "amount": _to_float(it.get("amount")),
                        "pre_close": prev_close, "change": change, "pct_chg": pct,
                        "updated_at": now,
                    },
                    "$setOnInsert": {"created_at": now, "version": 1},
                },
                upsert=True,
            ))
            prev_close = close

        if not ops:
            return 0
        res = await coll.bulk_write(ops, ordered=False)
        return (res.upserted_count or 0) + (res.modified_count or 0)

    # ==================== 三、基础信息同步 ====================

    async def sync_basic_info(self, symbols: Optional[List[str]] = None,
                              market: Optional[str] = None) -> Dict[str, Any]:
        """
        同步基础信息到 stock_basic_info_<hk|us>。
        数据检验读取字段：symbol / code / name / list_date。
        """
        db = get_mongo_db()
        targets = await self._load_targets(symbols, market)
        stats = {"total": len(targets), "ok": 0, "failed": 0, "failed_symbols": []}
        now = datetime.now()

        for t in targets:
            market, symbol = t["market"], t["symbol"]
            coll = db[_coll_names(market)["basic"]]
            try:
                info = await asyncio.to_thread(self._fetch_basic_info, t)
                if not info:
                    stats["failed"] += 1
                    stats["failed_symbols"].append(f"{market}:{symbol}")
                    continue
                await coll.update_one(
                    {"market": market, "symbol": symbol},
                    {
                        "$set": {
                            "code": symbol, "full_symbol": to_full_symbol(t["windcode"], market),
                            "market": market, "name": info.get("name") or "",
                            "list_date": info.get("list_date") or "",
                            "industry": info.get("industry") or "",
                            "market_board": info.get("market_board") or "",
                            "source": info.get("source") or "wind",
                            "updated_at": now,
                        },
                        "$setOnInsert": {"created_at": now},
                    },
                    upsert=True,
                )
                stats["ok"] += 1
            except Exception as e:
                stats["failed"] += 1
                stats["failed_symbols"].append(f"{market}:{symbol}")
                logger.warning(f"⚠️ 基础信息同步失败 {market}:{symbol}: {e}")

        logger.info(f"✅ 基础信息同步完成: {stats}")
        return stats

    def _fetch_basic_info(self, target: Dict[str, str]) -> Optional[Dict[str, Any]]:
        """
        基础信息：用 Wind（含上市板/行业，质量最高）。
        实测返回列名示例：Wind代码 / 证券简称 / **首发上市日期** / 所属WIND行业明细 / 上市板
        """
        market, symbol = target["market"], target["symbol"]
        try:
            df = self._wind_client().call_table(
                "stock_data", "get_stock_basicinfo",
                {"question": f"查询{target['windcode']}的证券简称、上市日期、所属Wind行业、上市板"})
        except Exception as e:
            logger.debug(f"Wind 基础信息失败 {symbol}: {e}")
            return None
        if df is None or getattr(df, "empty", True):
            return None

        def pick(*keys: str) -> str:
            """按子串匹配列名（Wind 列名有「首发上市日期」这类前缀变体）"""
            for c in df.columns:
                if any(k in str(c) for k in keys):
                    v = str(df.iloc[0].get(c) or "").strip()
                    if v and v not in ("None", "nan"):
                        return v
            return ""

        name = pick("证券简称", "简称", "名称")
        list_date = pick("上市日期", "上市日")[:10]
        industry = pick("WIND行业", "行业")
        board = pick("上市板")
        if not (name or list_date):
            return None
        return {
            "name": name, "list_date": list_date,
            "industry": industry, "market_board": board,
            "source": "wind",
        }

    # ==================== 四、财务同步 ====================

    async def sync_financial(self, symbols: Optional[List[str]] = None,
                             market: Optional[str] = None) -> Dict[str, Any]:
        """同步财务数据到 stock_financial_data_<hk|us>（Wind 自然语言查询）"""
        db = get_mongo_db()
        targets = await self._load_targets(symbols, market)
        stats = {"total": len(targets), "ok": 0, "failed": 0, "failed_symbols": []}
        now = datetime.now()

        for t in targets:
            market, symbol = t["market"], t["symbol"]
            coll = db[_coll_names(market)["financial"]]
            try:
                rec = await asyncio.to_thread(self._fetch_financial, t)
                if not rec:
                    stats["failed"] += 1
                    stats["failed_symbols"].append(f"{market}:{symbol}")
                    continue
                await coll.update_one(
                    {"market": market, "symbol": symbol,
                     "report_period": rec["report_period"]},
                    {
                        "$set": {
                            "code": symbol, "full_symbol": to_full_symbol(t["windcode"], market),
                            "market": market, "data_source": "wind",
                            "revenue": rec.get("revenue"), "net_profit": rec.get("net_profit"),
                            "roe": rec.get("roe"), "updated_at": now,
                        },
                        "$setOnInsert": {"created_at": now, "version": 1},
                    },
                    upsert=True,
                )
                stats["ok"] += 1
            except Exception as e:
                stats["failed"] += 1
                stats["failed_symbols"].append(f"{market}:{symbol}")
                logger.warning(f"⚠️ 财务同步失败 {market}:{symbol}: {e}")

        logger.info(f"✅ 财务同步完成: {stats}")
        return stats

    def _fetch_financial(self, target: Dict[str, str]) -> Optional[Dict[str, Any]]:
        """
        Wind 财务查询 → 结构化字段。

        实测 Wind 返回的是**结构化表格**而非自然语言，形如：
            columns: [Wind代码, 证券简称, 最新营业收入(亿元), 最新净利润(亿元), 最新净资产收益率ROE(%)]
            rows:    [["0700.HK","腾讯控股",4012.43,1141.15,9.9664]]
        故用 call_table 解析，并把「亿元」换算为「元」以对齐 A 股集合口径。
        """
        wc = target["windcode"]
        try:
            df = self._wind_client().call_table(
                "stock_data", "get_stock_fundamentals",
                {"question": f"查询{wc}最新报告期的营业收入、净利润、净资产收益率ROE"})
        except Exception as e:
            logger.debug(f"Wind 财务失败 {wc}: {e}")
            return None
        if df is None or getattr(df, "empty", True):
            return None

        row = df.iloc[0]

        def pick(*keys: str) -> Optional[float]:
            for c in df.columns:
                if any(k in str(c) for k in keys):
                    return _to_float(row.get(c))
            return None

        revenue_yi = pick("营业收入", "营业总收入", "营收")
        net_yi = pick("净利润")
        roe = pick("ROE", "净资产收益率")
        if revenue_yi is None and net_yi is None and roe is None:
            return None

        return {
            "report_period": "latest",
            "revenue": revenue_yi * 1e8 if revenue_yi is not None else None,   # 亿元 → 元
            "net_profit": net_yi * 1e8 if net_yi is not None else None,
            "roe": roe,
        }

    @staticmethod
    def _extract_text(raw: Any) -> str:
        """从 Wind 返回结构中提取纯文本（兼容嵌套）"""
        if isinstance(raw, str):
            return raw
        if isinstance(raw, dict):
            for k in ("text", "content", "answer", "message"):
                if isinstance(raw.get(k), str):
                    return raw[k]
            for v in raw.values():
                t = HKUSPreheatService._extract_text(v)
                if t:
                    return t
        if isinstance(raw, list):
            for v in raw:
                t = HKUSPreheatService._extract_text(v)
                if t:
                    return t
        return ""

    # ==================== 五、索引初始化 ====================

    @staticmethod
    async def ensure_indexes() -> Dict[str, int]:
        """创建 4 类集合的索引（幂等）"""
        db = get_mongo_db()
        created = {}
        for market in ("HK", "US"):
            names = _coll_names(market)
            daily, basic, fin = names["daily"], names["basic"], names["financial"]
            await db[daily].create_index(
                [("symbol", 1), ("trade_date", 1), ("data_source", 1), ("period", 1)],
                unique=True, name="symbol_date_source_period_unique")
            await db[daily].create_index([("symbol", 1)], name="symbol_index")
            await db[daily].create_index([("trade_date", -1)], name="trade_date_index")
            await db[daily].create_index([("symbol", 1), ("trade_date", -1)], name="symbol_date_index")

            await db[basic].create_index([("market", 1), ("symbol", 1)], unique=True, name="market_symbol_unique")
            await db[basic].create_index([("code", 1)], name="code_index")

            await db[fin].create_index(
                [("market", 1), ("symbol", 1), ("report_period", 1)],
                unique=True, name="market_symbol_period_unique")
            await db[fin].create_index([("symbol", 1)], name="symbol_index")
            created[f"{market}:{daily},{basic},{fin}"] = 1

        await db[UNIVERSE_COLLECTION].create_index(
            [("market", 1), ("symbol", 1)], unique=True, name="market_symbol_unique")
        await db[UNIVERSE_COLLECTION].create_index([("sources", 1)], name="sources_index")
        created["universe"] = 1
        logger.info(f"✅ 索引创建完成: {list(created)}")
        return created


# ==================== 便捷入口（供定时任务调用） ====================

async def run_hk_preheat(full: bool = False) -> Dict[str, Any]:
    """港股预热：universe → 日线 → 基础信息 → 财务"""
    svc = HKUSPreheatService()
    await svc.ensure_indexes()
    await svc.refresh_universe(force=full)
    result = {"daily": await svc.sync_daily(market="HK")}
    if settings.HK_US_PREHEAT_ENABLE_BASIC_INFO:
        result["basic"] = await svc.sync_basic_info(market="HK")
    if settings.HK_US_PREHEAT_ENABLE_FINANCIAL:
        result["financial"] = await svc.sync_financial(market="HK")
    return result


async def run_us_preheat(full: bool = False) -> Dict[str, Any]:
    """美股预热：universe → 日线 → 基础信息 → 财务"""
    svc = HKUSPreheatService()
    await svc.ensure_indexes()
    await svc.refresh_universe(force=full)
    result = {"daily": await svc.sync_daily(market="US")}
    if settings.HK_US_PREHEAT_ENABLE_BASIC_INFO:
        result["basic"] = await svc.sync_basic_info(market="US")
    if settings.HK_US_PREHEAT_ENABLE_FINANCIAL:
        result["financial"] = await svc.sync_financial(market="US")
    return result


# ==================== CLI（手动执行 / Gate-1 试探） ====================

def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="港美股行情预热缓存")
    p.add_argument("action", choices=["indexes", "universe", "daily", "basic", "financial", "all"],
                   help="执行的动作")
    p.add_argument("--symbols", default="", help="仅处理指定内部代码，逗号分隔（如 00700,AAPL）")
    p.add_argument("--limit", type=int, default=0, help="首次回看的自然日天数，0 用配置默认")
    p.add_argument("--force", action="store_true", help="强制重查指数成分清单")
    return p.parse_args(argv)


async def _main(argv: Optional[List[str]] = None) -> None:
    args = _parse_args(argv)
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()] or None
    svc = HKUSPreheatService()

    if args.action in ("indexes", "all", "universe"):
        await svc.ensure_indexes()
    if args.action in ("universe", "daily", "basic", "financial", "all"):
        # daily/basic/financial 也先确保 universe 存在
        if args.action != "universe":
            await svc.refresh_universe(force=False)

    if args.action == "indexes":
        pass
    elif args.action == "universe":
        print(await svc.refresh_universe(force=args.force))
    elif args.action == "daily":
        print(await svc.sync_daily(symbols=symbols, lookback_days=args.limit or None))
    elif args.action == "basic":
        print(await svc.sync_basic_info(symbols=symbols))
    elif args.action == "financial":
        print(await svc.sync_financial(symbols=symbols))
    elif args.action == "all":
        print(await svc.refresh_universe(force=args.force))
        print(await svc.sync_daily(symbols=symbols, lookback_days=args.limit or None))
        print(await svc.sync_basic_info(symbols=symbols))
        print(await svc.sync_financial(symbols=symbols))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(_main())
