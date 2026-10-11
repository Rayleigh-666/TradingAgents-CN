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
- Wind `search_stocks` 单次最多返回 100 条，标普500(502)/港股通(667) 会被截断
  → 采用「达上限即细分」的递归分批策略（美股按 A-Z，港股按 0-9）
- 美股后缀有两种：`.O`(纳斯达克) / `.N`(纽交所)，必须使用 Wind 返回值，不可自拼
- Wind 传无后缀 `AAPL` 返回空，必须带后缀
- 港股 Wind 代码为 **4 位**（`0002.HK`），与 Yahoo 格式一致；内部 symbol 为 5 位（`00002`）
- 自选股存储在 `users.favorite_stocks` 内嵌数组（字段含 `market`），非独立集合
- ⚠️ Wind 代码解析器会把**已退市**实体模糊匹配回来（`AMZN.O`→`AMAZ.OO`、
  `COR.N`→`COR!.N`、`DD.N`→`DD!.N`、`FOXA.O`→`TFCFA.O`），
  `!` 是 Wind 的退市标记。标普500 中 COR/DD/DOW/FOX 等「代码被复用」的标的最易中招。
  故所有 Wind 返回都必须过 `windcode_matches()` 校验，不匹配一律弃用；
  基础信息另加「证券简称含『退市』」判定（实测 AVB 在市却被 Wind 标为退市），
  命中即转 Yahoo 兜底；财务则计入失败清单，宁缺勿错。

吞吐优化（本期）
----------------
标的约 1190 只（HK 673 / US 517），逐只串行需 1.5–2 小时，故两处批量化：

1. **日线**：`yfinance.download()` 一次请求多只（`HK_US_PREHEAT_BATCH_SIZE`，默认 60），
   HTTP 次数从 N 降到 N/批大小；随后仍逐只按「> 库内最新 trade_date」过滤，
   保证增量语义与幂等性不变。主源非 yahoo 或关闭开关时自动回退逐只。
2. **基础信息 / 财务**：Wind `get_stock_basicinfo` / `get_stock_fundamentals`
   支持一次传多个代码（`HK_US_PREHEAT_WIND_BATCH_SIZE`，默认 40，实测 20 只约 2.5s，
   而逐只 1.3s/只），批内未覆盖的代码再逐只兜底重试。

**`--force-full`**：忽略增量窗口，按完整 lookback 重取全历史，并从本批第一根 K 线
重置 `pre_close/change/pct_chg` 起算点（`from_scratch`）。用于修正「首次只跑了短窗口
（如 Gate-1 的 30/90 天）导致历史深度不足」的标的；仍是幂等 upsert，不会产生重复数据。

设计文档：docs/design/hk-us-preheat-cache.md
"""

import argparse
import asyncio
import logging
import re
from datetime import date, datetime, timedelta, timezone
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
# Yahoo 返回行数低于该值即视为「上游数据缺口」，尝试用 Wind 补齐。
# 实测 AVB 在 Yahoo 侧整段只剩 1 根，而 Wind 有 222 根。
_MIN_YAHOO_ROWS = 5

_US_CHARS = list("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
_HK_CHARS = list("0123456789")

UNIVERSE_COLLECTION = "hk_us_preheat_universe"

# Wind 用于标记「已退市/已失效」证券的后缀字符
_DELISTED_MARK = "!"
# Wind 证券简称中的退市标记（如「艾芙隆海湾社区(AVALONBAY)(退市)」）。
# 本项目的标的池来自指数成分与自选股，均为在市证券；命中该标记说明 Wind 侧数据陈旧。
_DELISTED_NAME_MARK = "退市"

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


def windcode_matches(queried: str, returned: str) -> bool:
    """
    校验 Wind 返回的代码是否确为所查询的标的（防止把「另一家公司」的数据写进来）。

    实测 Wind 自然语言解析器会把**已退市/已失效**的代码模糊匹配回来：
        AMZN.O → AMAZ.OO  （AMAZING ENE OIL & GAS，前缀相似误配）
        COR.N  → COR!.N   （`!` 是 Wind 的退市标记；DD.N→DD!.N、DOW.N→DOW!.N 同理）
        FOXA.O → TFCFA.O  （21世纪福克斯-A，退市）
    标普500 成分清单里有若干「代码被复用」的标的（COR/DD/DOW/FOX…），
    不校验就会把退市旧实体的财务/基础信息写到当前 symbol 名下。

    returned 为空（未返回代码列）时不阻断，交由调用方决定。
    """
    if not returned:
        return True
    q = (queried or "").strip().upper()
    r = (returned or "").strip().upper()
    if not q:
        return True
    if _DELISTED_MARK in r:
        return False
    if q == r:
        return True
    q_base, _, _ = q.partition(".")
    r_base, _, _ = r.partition(".")
    if q_base == r_base:
        return True                       # 同一标的，交易所后缀可能不同（.O / .N）
    if q_base.isdigit() and r_base.isdigit() and q_base.lstrip("0") == r_base.lstrip("0"):
        return True                       # 港股前导零差异：0002 vs 00002
    return False


def match_requested(requested: Sequence[str], returned: str) -> Optional[str]:
    """在 requested 里找出与 returned 匹配的那个代码；无匹配返回 None"""
    if not returned:
        return None
    for wc in requested:
        if windcode_matches(wc, returned):
            return wc
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
                         progress_every: int = 50,
                         force_full: bool = False) -> Dict[str, Any]:
        """
        同步港美股日线。symbols 为空则处理 universe 中全部（可按 market 过滤 HK/US）。

        两条路径：
        - **批量**（默认）：主源为 yahoo 且 `HK_US_PREHEAT_USE_BATCH=True` 时，
          用 `yfinance.download()` 一次请求拉多只，HTTP 次数从 N 降到 N/批大小，
          全量 ~1100 只从 1 小时级压到 10 分钟级。逐只增量窗口过滤保证幂等与正确性。
        - **逐只**：主源为 wind 或显式关闭批量时，退化为逐只取数（原实现）。

        force_full=True 时忽略增量窗口，按完整 lookback 重取并重算
        `pre_close/change/pct_chg`（修正历史深度不足的标的）。与增量模式一样是
        幂等 upsert，不会产生重复数据。

        返回统计：{total, ok, skipped, failed, rows, failed_symbols, elapsed}
        """
        db = get_mongo_db()
        lookback = int(lookback_days or settings.HK_US_PREHEAT_LOOKBACK_DAYS or 365)
        targets = await self._load_targets(symbols, market)

        primary = (settings.HK_US_PREHEAT_PRIMARY_SOURCE or "yahoo").strip().lower()
        use_batch = bool(settings.HK_US_PREHEAT_USE_BATCH) and primary == "yahoo"
        if use_batch and targets:
            return await self._sync_daily_batch(targets, lookback, force_full)
        return await self._sync_daily_per_symbol(
            targets, lookback, progress_every, force_full)

    # ---------- 路径 A：逐只（wind 主源 / 批量关闭） ----------

    async def _sync_daily_per_symbol(self, targets: List[Dict[str, str]],
                                     lookback: int, progress_every: int = 50,
                                     force_full: bool = False) -> Dict[str, Any]:
        db = get_mongo_db()
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
                limit = lookback if force_full else \
                    await self._incremental_limit(coll, symbol, lookback)
                if limit <= 0:
                    stats["skipped"] += 1
                    continue
                items, source = await self._fetch_kline(
                    t["windcode"], market, t["yahoocode"], limit)
                if not items:
                    stats["failed"] += 1
                    stats["failed_symbols"].append(f"{market}:{symbol}")
                    continue
                n = await self._upsert_daily(coll, items, t, source,
                                             from_scratch=force_full)
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

    # ---------- 路径 B：Yahoo 批量下载（默认） ----------

    async def _sync_daily_batch(self, targets: List[Dict[str, str]],
                                lookback: int,
                                force_full: bool = False) -> Dict[str, Any]:
        """
        批量日线同步：先逐只算增量窗口 → 按市场汇总统一时间窗 → 分批 yf.download
        → 逐只按「> 库内最新 trade_date」过滤后再幂等 upsert。

        force_full=True 时跳过「> 最新交易日」过滤并按完整窗口重算涨跌（重建历史深度）。
        Yahoo 无数据的标的自动回退 Wind 单只；仍无数据才计入失败清单。
        """
        db = get_mongo_db()
        stats: Dict[str, Any] = {
            "total": len(targets), "ok": 0, "skipped": 0, "failed": 0,
            "rows": 0, "failed_symbols": [], "source": {"yahoo": 0, "wind": 0},
        }
        if force_full:
            stats["mode"] = "force_full"
        started = datetime.now()

        # 1. 逐只算增量窗口（一次 find_one / 只，可忽略）
        plan: List[Dict[str, Any]] = []
        for t in targets:
            coll = db[_coll_names(t["market"])["daily"]]
            last = await self._last_trade_date(coll, t["symbol"])
            limit = lookback if force_full else self._window_from_last(last, lookback)
            if limit <= 0:
                stats["skipped"] += 1
                continue
            plan.append({"t": t, "coll": coll, "last": last, "limit": limit})

        if not plan:
            stats["elapsed"] = round((datetime.now() - started).total_seconds(), 1)
            logger.info(f"✅ 日线同步完成（无需更新）: {stats}")
            return stats

        chunk_size = int(settings.HK_US_PREHEAT_BATCH_SIZE or 60)
        sleep_s = float(settings.HK_US_PREHEAT_BATCH_SLEEP_SECONDS or 0)

        # 2. 按市场分批下载
        for mk in ("HK", "US"):
            group = [p for p in plan if p["t"]["market"] == mk]
            if not group:
                continue

            # 统一时间窗 = 覆盖本组最早需要的起点（后续逐只再裁）
            end_day = date.today() + timedelta(days=1)
            start_day = min(end_day - timedelta(days=p["limit"] + 10) for p in group)

            tickers: List[str] = []
            for p in group:
                tk = p["t"].get("yahoocode") or ""
                if tk and tk not in tickers:
                    tickers.append(tk)

            logger.info(f"  ⬇️ [{mk}] Yahoo 批量下载 {len(tickers)} 只"
                        f"（{start_day} ~ {end_day}，{chunk_size} 只/批，共 "
                        f"{-(-len(tickers)//chunk_size)} 批）…")
            data_map = await asyncio.to_thread(
                self._yahoo_batch, tickers, start_day, end_day, chunk_size, sleep_s)
            logger.info(f"  ✅ [{mk}] Yahoo 有数据 {len(data_map)}/{len(tickers)} 只，开始写库…")

            # 3. 逐只过滤 + 写库
            for i, p in enumerate(group, 1):
                t, coll, last = p["t"], p["coll"], p["last"]
                symbol = t["symbol"]
                try:
                    raw = data_map.get(t.get("yahoocode") or "") or []
                    rows_written = 0
                    used: Optional[str] = None

                    # (a) Yahoo 覆盖过薄（上游缺口）→ 试用 Wind 补齐，谁多听谁。
                    #     按**完整 lookback** 取 Wind（而非增量小窗口），否则补不全；
                    #     且只在「Wind 行数 > Yahoo 行数 且 > 库内现有行数」时切换，
                    #     保证任何情况下都不会让已存历史变短。
                    if len(raw) < _MIN_YAHOO_ROWS:
                        existing = await coll.count_documents({"symbol": symbol})
                        w_items = await asyncio.to_thread(
                            self._wind_kline, t["windcode"], max(p["limit"], lookback))
                        if w_items and len(w_items) > max(len(raw), existing):
                            rows_written = await self._write_single_source(
                                coll, t, w_items, "wind", from_scratch=True)
                            used = "wind"

                    if used is None:
                        new_rows = raw if force_full else \
                            [it for it in raw if not last or (it.get("date") or "") > last]
                        if new_rows:
                            rows_written = await self._upsert_daily(
                                coll, new_rows, t, "yahoo", from_scratch=force_full)
                            used = "yahoo"
                        elif raw:
                            # Yahoo 有历史数据但无新增（已是库内最新）
                            stats["skipped"] += 1
                        else:
                            # (b) Yahoo 完全没数据 → Wind 单只兜底
                            w_items = await asyncio.to_thread(
                                self._wind_kline, t["windcode"], p["limit"])
                            if w_items and last and not force_full:
                                w_items = [it for it in w_items
                                           if (it.get("date") or "") > last]
                            if w_items:
                                rows_written = await self._write_single_source(
                                    coll, t, w_items, "wind", from_scratch=force_full)
                                used = "wind"
                            elif last and not force_full:
                                stats["skipped"] += 1
                            else:
                                stats["failed"] += 1
                                stats["failed_symbols"].append(f"{mk}:{symbol}")

                    if used:
                        stats["rows"] += rows_written
                        stats["ok"] += 1
                        stats["source"][used] += 1
                except Exception as e:
                    stats["failed"] += 1
                    stats["failed_symbols"].append(f"{mk}:{symbol}")
                    logger.warning(f"⚠️ 日线入库失败 {mk}:{symbol}: {e}")

                if i % 100 == 0:
                    logger.info(f"  …[{mk}] 已写 {i}/{len(group)}"
                                f"（ok{stats['ok']} skip{stats['skipped']} fail{stats['failed']}）")

        stats["elapsed"] = round((datetime.now() - started).total_seconds(), 1)
        logger.info(f"✅ 日线同步完成（批量）: {stats}")
        return stats

    def _yahoo_batch(self, tickers: List[str], start_day: date, end_day: date,
                     chunk_size: int, sleep_s: float) -> Dict[str, List[Dict[str, Any]]]:
        """
        分批调用 yf.download，返回 {ticker: [日线 item, ...]}。

        单批内的 ticker 由 yfinance 自行并发（threads=True），
        任一只无数据/退市不会影响同批其它标的。
        """
        import time as _time
        import yfinance as yf

        out: Dict[str, List[Dict[str, Any]]] = {}
        s = start_day.strftime("%Y-%m-%d")
        e = end_day.strftime("%Y-%m-%d")
        chunks = [tickers[i:i + chunk_size] for i in range(0, len(tickers), chunk_size)]

        for idx, chunk in enumerate(chunks, 1):
            try:
                df = yf.download(
                    chunk, start=s, end=e, auto_adjust=False,
                    group_by="ticker", threads=True, progress=False, timeout=30)
            except Exception as ex:
                logger.warning(f"⚠️ Yahoo 批量下载第 {idx}/{len(chunks)} 批失败"
                               f"（{len(chunk)} 只，将逐只回退）: {ex}")
                df = None

            if df is not None and not df.empty:
                try:
                    is_multi = getattr(df.columns, "nlevels", 1) > 1
                    if is_multi:
                        level0 = set(df.columns.get_level_values(0))
                        for tk in chunk:
                            if tk not in level0:
                                continue
                            rows = self._yahoo_frame_to_items(df[tk])
                            if rows:
                                out[tk] = rows
                    elif len(chunk) == 1:
                        rows = self._yahoo_frame_to_items(df)
                        if rows:
                            out[chunk[0]] = rows
                except Exception as ex:
                    logger.warning(f"⚠️ Yahoo 批量结果解析失败（第 {idx} 批）: {ex}")

            if sleep_s:
                _time.sleep(sleep_s)
            if idx % 5 == 0 or idx == len(chunks):
                logger.info(f"  …Yahoo 下载进度 {idx}/{len(chunks)} 批，累计有数据 "
                            f"{len(out)}/{len(tickers)} 只")

        return out

    @staticmethod
    def _yahoo_frame_to_items(frame: Any) -> List[Dict[str, Any]]:
        """单只标的的 yfinance DataFrame → 日线 item 列表（与 `_yahoo_kline` 同构）"""
        if frame is None or getattr(frame, "empty", True):
            return []
        try:
            # 单只下载时可能多一层 ticker 索引，拍平到字段层
            if hasattr(frame.columns, "nlevels") and frame.columns.nlevels > 1:
                frame = frame.droplevel(0, axis=1)
            if "Close" not in frame.columns:
                return []
        except Exception:
            return []

        out: List[Dict[str, Any]] = []
        for idx, row in frame.iterrows():
            try:
                d = idx.strftime("%Y-%m-%d")
            except Exception:
                continue
            close = _to_float(row.get("Close"))
            if close is None:
                continue  # 停牌 / 无成交行
            out.append({
                "date": d,
                "open": _to_float(row.get("Open")),
                "high": _to_float(row.get("High")),
                "low": _to_float(row.get("Low")),
                "close": close,
                "volume": _to_float(row.get("Volume")),
            })
        return out

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

    async def _last_trade_date(self, coll, symbol: str) -> Optional[str]:
        """库内该 symbol 的最新交易日（"YYYY-MM-DD"），无数据返回 None"""
        doc = await coll.find_one({"symbol": symbol}, {"trade_date": 1},
                                  sort=[("trade_date", -1)])
        if doc and doc.get("trade_date"):
            return str(doc["trade_date"])[:10]
        return None

    @staticmethod
    def _window_from_last(last: Optional[str], default_lookback: int) -> int:
        """
        由「库内最新交易日」推出本次需拉取的自然日窗口。
        返回 0 表示已是最新（当天或未来），无需更新。
        """
        if not last:
            return default_lookback
        try:
            latest = datetime.strptime(str(last)[:10], "%Y-%m-%d").date()
        except ValueError:
            return default_lookback
        gap = (date.today() - latest).days
        if gap <= 0:
            return 0
        # 多留 5 天缓冲，避免因停牌/假期导致漏掉最新交易日
        return min(max(gap + 5, 5), default_lookback)

    async def _incremental_limit(self, coll, symbol: str, default_lookback: int) -> int:
        """
        计算本次需要拉取的自然日窗口。
        返回 0 表示已是最新，无需更新。
        """
        last = await self._last_trade_date(coll, symbol)
        return self._window_from_last(last, default_lookback)

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
                            target: Dict[str, str], source: str,
                            from_scratch: bool = False) -> int:
        """
        幂等写入日线（唯一键 symbol+trade_date+data_source+period）。

        from_scratch=True 时不读取库内最近收盘作为首日前收，而是从本批第一根 K 线
        重新起算 pre_close/change/pct_chg（用于 force_full 重建完整历史）。
        """
        now = datetime.now()
        prev_close = None
        if not from_scratch:
            last = await coll.find_one({"symbol": target["symbol"]}, {"close": 1},
                                       sort=[("trade_date", -1)])
            if last:
                prev_close = _to_float(last.get("close"))
        ops = self._build_daily_ops(items, target, source, prev_close, now)
        if not ops:
            return 0
        res = await coll.bulk_write(ops, ordered=False)
        return (res.upserted_count or 0) + (res.modified_count or 0)

    async def _write_single_source(self, coll, target: Dict[str, str],
                                   items: List[Dict[str, Any]], source: str,
                                   from_scratch: bool = False) -> int:
        """
        把该 symbol 的日线整体切换为**单一来源**后写入。

        唯一键含 data_source，若同一标的先后由 Yahoo / Wind 写入，同一交易日会出现
        两条记录，K 线接口会返回重复日期。故切换来源时先清掉其它来源的旧行。
        前收在删除前先取出，保证增量链式涨跌不被破坏。
        """
        now = datetime.now()
        prev_close = None
        if not from_scratch:
            last = await coll.find_one({"symbol": target["symbol"]}, {"close": 1},
                                       sort=[("trade_date", -1)])
            if last:
                prev_close = _to_float(last.get("close"))
        await coll.delete_many({"symbol": target["symbol"],
                                "data_source": {"$ne": source}})
        ops = self._build_daily_ops(items, target, source, prev_close, now)
        if not ops:
            return 0
        res = await coll.bulk_write(ops, ordered=False)
        return (res.upserted_count or 0) + (res.modified_count or 0)

    @staticmethod
    def _build_daily_ops(items: List[Dict[str, Any]], target: Dict[str, str],
                         source: str, prev_close: Optional[float],
                         now: datetime) -> List[UpdateOne]:
        """按日期升序链式计算 change/pct_chg 并生成 upsert 操作"""
        market, symbol = target["market"], target["symbol"]
        full_symbol = to_full_symbol(target["windcode"], market)
        ops: List[UpdateOne] = []
        for it in sorted(items, key=lambda x: x.get("date") or ""):
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
        return ops

    # ==================== 三、基础信息同步 ====================

    async def sync_basic_info(self, symbols: Optional[List[str]] = None,
                              market: Optional[str] = None) -> Dict[str, Any]:
        """
        同步基础信息到 `stock_basic_info_<hk|us>`。
        数据检验读取字段：symbol / code / name / list_date。

        批量策略：Wind `get_stock_basicinfo` 支持一次查询多个代码
        （实测 20 只 2.5s，逐只 1.3s/只），故按 `WIND_BATCH_SIZE` 分批，
        批内未覆盖的代码再逐只兜底重试。
        """
        db = get_mongo_db()
        targets = await self._load_targets(symbols, market)
        stats = {"total": len(targets), "ok": 0, "failed": 0,
                 "failed_symbols": [], "batches": 0}
        now = datetime.now()
        batch_size = int(settings.HK_US_PREHEAT_WIND_BATCH_SIZE or 40)

        info_map = await self._collect_basic_info(targets, batch_size, stats)
        await self._retry_missing_basic(targets, info_map, stats)

        for t in targets:
            market_, symbol = t["market"], t["symbol"]
            coll = db[_coll_names(market_)["basic"]]
            info = info_map.get(t["windcode"])
            if not info:
                stats["failed"] += 1
                stats["failed_symbols"].append(f"{market_}:{symbol}")
                continue
            try:
                await coll.update_one(
                    {"market": market_, "symbol": symbol},
                    {
                        "$set": {
                            "code": symbol, "full_symbol": to_full_symbol(t["windcode"], market_),
                            "market": market_, "name": info.get("name") or "",
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
                stats["failed_symbols"].append(f"{market_}:{symbol}")
                logger.warning(f"⚠️ 基础信息写入失败 {market_}:{symbol}: {e}")

        logger.info(f"✅ 基础信息同步完成: {stats}")
        return stats

    async def _collect_basic_info(self, targets: List[Dict[str, str]],
                                  batch_size: int, stats: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        """按批拉取基础信息，返回 {windcode: info}"""
        info_map: Dict[str, Dict[str, Any]] = {}
        for chunk in self._chunks(targets, batch_size):
            got = await asyncio.to_thread(
                self._fetch_basic_batch, [t["windcode"] for t in chunk])
            info_map.update(got)
            stats["batches"] += 1
            logger.info(f"  …基础信息批量 {stats['batches']} 批，累计命中 "
                        f"{len(info_map)}/{len(targets)} 只")
        return info_map

    async def _retry_missing_basic(self, targets: List[Dict[str, str]],
                                   info_map: Dict[str, Dict[str, Any]],
                                   stats: Dict[str, Any]) -> None:
        """批量未覆盖的代码逐只兜底重试"""
        missing = [t for t in targets if t["windcode"] not in info_map]
        if not missing:
            return
        if len(missing) > 50 and len(missing) > len(targets) * 0.5:
            logger.warning(f"⚠️ 基础信息批量命中率偏低（缺 {len(missing)}/{len(targets)}），"
                           f"判定 Wind 批量查询异常，逐只兜底重试…")
        for t in missing:
            info = await asyncio.to_thread(self._fetch_basic_info, t)
            if not info:
                # Wind 无有效命中（含代码误配到退市旧实体）→ Yahoo 兜底
                info = await asyncio.to_thread(self._fetch_basic_info_yahoo, t)
            if info:
                info_map[t["windcode"]] = info

    @staticmethod
    def _chunks(items: List[Any], size: int) -> List[List[Any]]:
        size = max(1, int(size or 1))
        return [items[i:i + size] for i in range(0, len(items), size)]

    def _fetch_basic_batch(self, windcodes: List[str]) -> Dict[str, Dict[str, Any]]:
        """Wind 一次查询多只基础信息 → {windcode: info}"""
        if not windcodes:
            return {}
        q = "查询" + "、".join(windcodes) + "的证券简称、上市日期、所属Wind行业、上市板"
        try:
            df = self._wind_client().call_table(
                "stock_data", "get_stock_basicinfo", {"question": q})
        except Exception as e:
            logger.warning(f"⚠️ Wind 基础信息批量查询失败（{len(windcodes)} 只）: {e}")
            return {}
        if df is None or getattr(df, "empty", True):
            return {}
        code_col = next((c for c in ("Wind代码", "代码", "windcode") if c in df.columns), None)
        if not code_col:
            logger.warning(f"⚠️ 基础信息批量结果无代码列: {list(df.columns)}")
            return {}
        out: Dict[str, Dict[str, Any]] = {}
        for _, row in df.iterrows():
            ret = str(row.get(code_col) or "").strip()
            if not ret or ret in ("None", "nan"):
                continue
            # 只接受确实命中「本批请求代码」的行，避免退市旧实体串号
            wc = match_requested(windcodes, ret)
            if not wc:
                logger.info(f"⏭️ 基础信息跳过不匹配返回：请求批内无 {ret}")
                continue
            info = self._parse_basic_row(df, row)
            if info:
                out[wc] = info
        return out

    @staticmethod
    def _parse_basic_row(df: Any, row: Any) -> Optional[Dict[str, Any]]:
        """
        DataFrame 某行 → 基础信息 dict。
        实测返回列名：Wind代码 / 证券简称 / **首发上市日期** / 所属WIND行业明细 / 上市板
        """
        def pick(*keys: str) -> str:
            for c in df.columns:
                if "级别" in str(c):      # 跳过「所属WIND行业明细.行业级别」这类派生列
                    continue
                if any(k in str(c) for k in keys):
                    v = str(row.get(c) or "").strip()
                    if v and v not in ("None", "nan"):
                        return v
            return ""

        name = pick("证券简称", "简称", "名称")
        if _DELISTED_NAME_MARK in name:
            # 标的池均为在市证券；命中退市标记说明 Wind 侧为陈旧数据（实测 AVB 中招），
            # 返回 None 让调用方转 Yahoo 兜底。
            return None
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

    def _fetch_basic_info(self, target: Dict[str, str]) -> Optional[Dict[str, Any]]:
        """单只兜底：Wind 查询基础信息（含代码匹配校验）"""
        try:
            df = self._wind_client().call_table(
                "stock_data", "get_stock_basicinfo",
                {"question": f"查询{target['windcode']}的证券简称、上市日期、所属Wind行业、上市板"})
        except Exception as e:
            logger.debug(f"Wind 基础信息失败 {target['symbol']}: {e}")
            return None
        if df is None or getattr(df, "empty", True):
            return None
        code_col = next((c for c in ("Wind代码", "代码", "windcode") if c in df.columns), None)
        if code_col is not None:
            ret = str(df.iloc[0].get(code_col) or "").strip()
            if not windcode_matches(target["windcode"], ret):
                logger.info(f"⏭️ Wind 基础信息代码不匹配，弃用 {target['windcode']} ← {ret}")
                return None
        return self._parse_basic_row(df, df.iloc[0])

    @staticmethod
    def _fetch_basic_info_yahoo(target: Dict[str, str]) -> Optional[Dict[str, Any]]:
        """
        Yahoo 兜底基础信息。

        用于 Wind 把代码误配到「已退市同名/相似标的」的场景：
        标普500 里 COR(Cencora)/DD(DuPont)/DOW(Dow)/FOX(Fox Corp) 等
        「代码被复用」的标的，Wind 会解析回退市旧实体，此时改用 Yahoo。
        """
        import yfinance as yf
        tk = target.get("yahoocode") or target["windcode"]
        try:
            info = yf.Ticker(tk).info or {}
        except Exception as e:
            logger.debug(f"Yahoo 基础信息失败 {tk}: {e}")
            return None

        name = str(info.get("longName") or info.get("shortName") or "").strip()
        list_date = ""
        # yfinance 1.7.0 实际字段是 firstTradeDateMilliseconds（毫秒），
        # 旧版本为 firstTradeDateEpochUtc（秒），两者都兼容。
        ftd_ms = info.get("firstTradeDateMilliseconds")
        ftd_s = info.get("firstTradeDateEpochUtc")
        try:
            if ftd_ms:
                list_date = datetime.fromtimestamp(
                    int(ftd_ms) / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d")
            elif ftd_s:
                list_date = datetime.fromtimestamp(
                    int(ftd_s), tz=timezone.utc).strftime("%Y-%m-%d")
        except (TypeError, ValueError, OSError, OverflowError):
            list_date = ""
        industry = str(info.get("industry") or info.get("sector") or "").strip()
        board = str(info.get("exchange") or info.get("fullExchangeName") or "").strip()
        if not (name or list_date):
            return None
        return {
            "name": name, "list_date": list_date,
            "industry": industry, "market_board": board,
            "source": "yahoo",
        }

    # ==================== 四、财务同步 ====================

    async def sync_financial(self, symbols: Optional[List[str]] = None,
                             market: Optional[str] = None) -> Dict[str, Any]:
        """
        同步财务数据到 `stock_financial_data_<hk|us>`（Wind 多代码批量查询）。

        批量：`get_stock_fundamentals` 支持一次查询多只（实测 15 只 2.8s），
        批内未覆盖的代码再逐只兜底。
        """
        db = get_mongo_db()
        targets = await self._load_targets(symbols, market)
        stats = {"total": len(targets), "ok": 0, "failed": 0,
                 "failed_symbols": [], "batches": 0}
        now = datetime.now()
        batch_size = int(settings.HK_US_PREHEAT_WIND_BATCH_SIZE or 40)

        rec_map = await self._collect_financial(targets, batch_size, stats)
        missing = [t for t in targets if t["windcode"] not in rec_map]
        for t in missing:
            rec = await asyncio.to_thread(self._fetch_financial, t)
            if rec:
                rec_map[t["windcode"]] = rec

        for t in targets:
            market_, symbol = t["market"], t["symbol"]
            coll = db[_coll_names(market_)["financial"]]
            rec = rec_map.get(t["windcode"])
            if not rec:
                stats["failed"] += 1
                stats["failed_symbols"].append(f"{market_}:{symbol}")
                continue
            try:
                await coll.update_one(
                    {"market": market_, "symbol": symbol,
                     "report_period": rec["report_period"]},
                    {
                        "$set": {
                            "code": symbol, "full_symbol": to_full_symbol(t["windcode"], market_),
                            "market": market_, "data_source": "wind",
                            "revenue": rec.get("revenue"), "net_profit": rec.get("net_profit"),
                            "roe": rec.get("roe"),
                            "report_period_label": rec.get("report_period_label") or "",
                            "updated_at": now,
                        },
                        "$setOnInsert": {"created_at": now, "version": 1},
                    },
                    upsert=True,
                )
                stats["ok"] += 1
            except Exception as e:
                stats["failed"] += 1
                stats["failed_symbols"].append(f"{market_}:{symbol}")
                logger.warning(f"⚠️ 财务写入失败 {market_}:{symbol}: {e}")

        logger.info(f"✅ 财务同步完成: {stats}")
        return stats

    async def _collect_financial(self, targets: List[Dict[str, str]],
                                 batch_size: int, stats: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        """按批拉取财务，返回 {windcode: rec}"""
        rec_map: Dict[str, Dict[str, Any]] = {}
        for chunk in self._chunks(targets, batch_size):
            got = await asyncio.to_thread(
                self._fetch_financial_batch, [t["windcode"] for t in chunk])
            rec_map.update(got)
            stats["batches"] += 1
            logger.info(f"  …财务批量 {stats['batches']} 批，累计命中 "
                        f"{len(rec_map)}/{len(targets)} 只")
        return rec_map

    def _fetch_financial_batch(self, windcodes: List[str]) -> Dict[str, Dict[str, Any]]:
        """Wind 一次查询多只财务 → {windcode: rec}"""
        if not windcodes:
            return {}
        q = ("查询" + "、".join(windcodes)
             + "最新报告期的营业收入、净利润、净资产收益率ROE")
        try:
            df = self._wind_client().call_table(
                "stock_data", "get_stock_fundamentals", {"question": q})
        except Exception as e:
            logger.warning(f"⚠️ Wind 财务批量查询失败（{len(windcodes)} 只）: {e}")
            return {}
        if df is None or getattr(df, "empty", True):
            return {}
        code_col = next((c for c in ("Wind代码", "代码", "windcode") if c in df.columns), None)
        if not code_col:
            logger.warning(f"⚠️ 财务批量结果无代码列: {list(df.columns)}")
            return {}
        out: Dict[str, Dict[str, Any]] = {}
        for _, row in df.iterrows():
            ret = str(row.get(code_col) or "").strip()
            if not ret or ret in ("None", "nan"):
                continue
            wc = match_requested(windcodes, ret)
            if not wc:
                logger.info(f"⏭️ 财务跳过不匹配返回：请求批内无 {ret}")
                continue
            rec = self._parse_financial_row(df, row)
            if rec:
                out[wc] = rec
        return out

    @staticmethod
    def _parse_financial_row(df: Any, row: Any) -> Optional[Dict[str, Any]]:
        """
        DataFrame 某行 → 财务 dict（亿元 → 元，对齐 A 股集合口径）。

        实测列名：Wind代码 / 证券简称 / 日期 / 最新营业收入 / 最新净利润 / 最新净资产收益率ROE
        注：单只与批量查询列名一致、数值一致（已验证 0002.HK=428.56 两种方式相同），
        单位为「亿元」（腾讯 0700.HK = 4012.43，即 4012.43 亿元）。
        """
        cols = list(df.columns)

        def pick(*keys: str) -> Optional[float]:
            for c in cols:
                if any(k in str(c) for k in keys):
                    return _to_float(row.get(c))
            return None

        revenue_yi = pick("营业收入", "营业总收入", "营收")
        net_yi = pick("净利润")
        roe = pick("ROE", "净资产收益率")
        if revenue_yi is None and net_yi is None and roe is None:
            return None

        label = ""
        for c in cols:
            if "日期" in str(c) or "报告期" in str(c):
                label = str(row.get(c) or "").strip()
                if label and label not in ("None", "nan"):
                    break
                label = ""

        return {
            "report_period": "latest",
            "report_period_label": label,
            "revenue": revenue_yi * 1e8 if revenue_yi is not None else None,
            "net_profit": net_yi * 1e8 if net_yi is not None else None,
            "roe": roe,
        }

    def _fetch_financial(self, target: Dict[str, str]) -> Optional[Dict[str, Any]]:
        """单只兜底：Wind 查询财务（含代码匹配校验，拒收退市旧实体）"""
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
        code_col = next((c for c in ("Wind代码", "代码", "windcode") if c in df.columns), None)
        if code_col is not None:
            ret = str(df.iloc[0].get(code_col) or "").strip()
            if not windcode_matches(wc, ret):
                logger.info(f"⏭️ Wind 财务代码不匹配，弃用 {wc} ← {ret}")
                return None
        return self._parse_financial_row(df, df.iloc[0])

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
    p.add_argument("--force-full", dest="force_full", action="store_true",
                   help="忽略增量窗口，按完整 lookback 重建日线历史（修正历史深度不足）")
    return p.parse_args(argv)


async def _main(argv: Optional[List[str]] = None) -> None:
    args = _parse_args(argv)
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()] or None
    # 独立命令行运行时需自行初始化 MongoDB 连接（定时任务路径已由应用初始化）
    from app.core.database import init_database
    await init_database()
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
        print(await svc.sync_daily(symbols=symbols, lookback_days=args.limit or None,
                                   force_full=args.force_full))
    elif args.action == "basic":
        print(await svc.sync_basic_info(symbols=symbols))
    elif args.action == "financial":
        print(await svc.sync_financial(symbols=symbols))
    elif args.action == "all":
        print(await svc.refresh_universe(force=args.force))
        print(await svc.sync_daily(symbols=symbols, lookback_days=args.limit or None,
                                   force_full=args.force_full))
        print(await svc.sync_basic_info(symbols=symbols))
        print(await svc.sync_financial(symbols=symbols))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(_main())
