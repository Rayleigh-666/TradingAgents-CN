"""
跨模块共享的证券代码格式常量。

美股「交易所/板块」后缀集合（`US_EXCH_SUFFIXES`）原先在四处各写了一份字面量副本，
只靠注释互相提醒「改动需同步」，任一处漏改都会造成静默失配：路由侧把 `AAPL.O`
认成美股并剥成 `AAPL`，而预热侧若仍把 `.O` 当成代码本体就会写错 symbol、命中不了
缓存。现已收敛到本模块，四处统一引用：

- `app/worker/hk_us_preheat_service.py`   从 Wind 代码剥后缀（`BRK_B.N` → `BRK.B`）
- `app/routers/stocks.py`                标准化用户输入（`AAPL.O` → `AAPL`）
- `app/routers/paper.py`                 同上（模拟交易下单）
- `tradingagents/utils/stock_utils.py`   分析链路的市场识别

由 `tests/test_hk_us_preheat_codes.py` 断言四处引用的是同一集合，防止再次漂移。

本模块刻意保持零依赖（只 import 标准库），因为它会被 `tradingagents` 侧导入，
而 `tradingagents` 的导入链较重（会初始化 ConfigManager）。
"""

import re

# 只认这些**已知**后缀，避免把股份类别的点号（`BRK.B` 里的 `B`）误当后缀剥掉。
# 实测本标的池 517 只美股实际只用到 {O(纳斯达克), N(纽交所), BAT(Cboe BZX)}，
# 其余为容错保留。**刻意不收单字母 A/B/C**——那正是股份类别的写法，
# 收了会把 `BRK.A` 剥成 `BRK`（Yahoo 的 `BRK` 是 A 类行情）。
US_EXCH_SUFFIXES = frozenset({
    "O", "N", "BAT", "OO", "NQ", "OB", "OQ", "PK", "AM", "GI", "HI",
})


# ==================== 内部 symbol 归一化 ====================
# 各市场「项目内部 symbol」的既定格式（与 hk_us_preheat_service.to_internal_symbol
# 的输出一致，也是库内 stock_basic_info_<hk|us> / stock_daily_quotes_<hk|us>、
# universe 的落库口径）：
#   cn → 6 位数字（不足左补 0）      000001 / 600519
#   hk → 5 位数字（不足左补 0）      00700 / 03669   （Wind/Yahoo 用 4 位：0700.HK）
#   us → 裸 ticker（大写、剥交易所后缀、股份类别统一点号）  AAPL / BRK.B
#
# ⚠️ 历史 bug：调用方曾对**所有**市场无条件 `zfill(6)`，把港股 5 位码 03669
#    补成 003669，与库内 5 位码永远匹配不上，导致「基础信息不存在」误报。
#    凡按代码查库/写库，一律先过本模块，不要在业务侧自行 zfill。

# 内部市场标识 → 归一化口径
_MARKET_ALIASES: dict = {
    "cn": "cn", "a股": "cn", "a_shares": "cn", "china": "cn", "etf": "cn",
    "hk": "hk", "hkg": "hk", "港股": "hk", "hongkong": "hk", "hk_stocks": "hk",
    "us": "us", "美股": "us", "usa": "us", "us_stocks": "us",
}


def resolve_market(market: str) -> str:
    """把各种市场写法（cn/A股/港股/USA/hk_stocks…）收敛为 cn/hk/us；未知返回 cn。"""
    key = str(market or "").strip().lower()
    return _MARKET_ALIASES.get(key, "cn")


def guess_market(code: str) -> str:
    """
    仅凭代码形态推断市场 —— 供「只有代码、拿不到 market 参数」的兜底场景使用
    （如 *_resolve_stock_name 的名称解析）。

    规则（与内部 symbol 口径一致，且**显式后缀优先于形态**）：
        *.HK                              → hk
        *.SH / *.SZ / *.SS / *.BJ         → cn
        含字母（AAPL / BRK.B / AAPL.O）    → us   （裸 ticker 是唯一含字母的形态）
        纯数字且位数 ≤ 5（700 / 03669）    → hk   （港股 4/5 位）
        纯数字且位数 ≥ 6（000001 / 003669）→ cn   （A 股/ETF 固定 6 位）
        其余                              → cn

    注意：6 位纯数字（含旧脏值 003669）一律按 A 股处理——无法从码形区分，
    这与 `stocks.py::_detect_market_and_code` 的口径一致（第五轮 Review P3-3）。
    """
    text = str(code or "").strip()
    if not text:
        return "cn"
    upper = text.upper()
    if upper.endswith(".HK"):
        return "hk"
    if upper.endswith((".SH", ".SZ", ".SS", ".BJ")):
        return "cn"
    if re.search(r"[A-Za-z]", text):
        return "us"
    digits = re.sub(r"\D", "", text)
    if not digits:
        return "cn"
    return "hk" if len(digits) <= 5 else "cn"


def basic_info_collection(market: str) -> str:
    """
    按市场返回「基础信息」集合名（名称解析 / 查库共用，避免各处硬编码集合名）：
        cn → stock_basic_info
        hk → stock_basic_info_hk
        us → stock_basic_info_us
    """
    resolved = resolve_market(market)
    return "stock_basic_info" if resolved == "cn" else f"stock_basic_info_{resolved}"


def normalize_hk_symbol(code: str) -> str:
    """
    港股内部 symbol：剥非数字后去前导零、补足 5 位。

    同时兼容 4 位（700）、5 位（00700）、带后缀（0700.HK）以及被误补成 6 位的
    历史脏值（003669 → 03669），保证「同一只股票只有一个内部 symbol」。

    去前导零后**有效数字超过 5 位**（如 `123456` / `1234567`）判为无法识别，
    返回 ""：港股代码有效位数最多 5 位，再长只可能是错误长码；若放行，
    `internal_to_windcode` 会截末 4 位把它注册成**另一只股票**的 universe 记录。
    （第五轮 Review P3-1）
    """
    digits = re.sub(r"\D", "", str(code or ""))
    if not digits:
        return ""
    significant = digits.lstrip("0") or "0"
    if len(significant) > 5:
        return ""
    return significant.zfill(5)


def normalize_cn_symbol(code: str) -> str:
    """A股/ETF 内部 symbol：剥非数字后补足 6 位（不足左补 0）。"""
    digits = re.sub(r"\D", "", str(code or ""))
    if not digits:
        return ""
    return digits.zfill(6)


def normalize_us_symbol(code: str) -> str:
    """
    美股内部 symbol：剥交易所后缀、股份类别分隔符统一为点号。

    AAPL.O → AAPL    BRK_B.N → BRK.B    BRK-B → BRK.B
    （与 hk_us_preheat_service._us_ticker 同源，后者已委托到本函数）
    """
    text = str(code or "").strip().upper()
    if not text:
        return ""
    parts = text.split(".")
    if len(parts) >= 2 and parts[-1] in US_EXCH_SUFFIXES:
        parts = parts[:-1]
    return ".".join(parts).replace("_", ".").replace("-", ".")


def normalize_symbol(code: str, market: str) -> str:
    """
    按市场把任意输入的代码归一化为项目内部 symbol（查库/写库前必须调用）。

    Args:
        code: 用户输入 / 上游返回的代码（000001 / 700 / 0700.HK / AAPL.O / BRK-B）
        market: 市场标识（cn/hk/us，也接受 A股/港股/美股 等写法）

    Returns:
        内部 symbol；输入为空/无法提取代码时返回 ""（调用方须自行处理空值）
    """
    resolved = resolve_market(market)
    if resolved == "hk":
        return normalize_hk_symbol(code)
    if resolved == "us":
        return normalize_us_symbol(code)
    return normalize_cn_symbol(code)


def internal_to_windcode(symbol: str, market: str) -> str:
    """
    内部 symbol → Wind 代码（`ensure_symbols` 注册 universe 用；美股后缀由
    `_resolve_us_windcodes` 反查补全，此处只回裸 ticker）。

        03669 → 3669.HK      （港股 Wind/Yahoo 均为 4 位 + .HK）
        AAPL  → AAPL         （待反查补成 AAPL.O）

    港股先过 `normalize_hk_symbol`：既把历史脏值 `003669` 修回 `03669` 再取末 4 位，
    也让无效长码（`1234567`）在这里就返回 ""，不会被静默截成另一只股票。（P3-1）
    """
    resolved = resolve_market(market)
    if resolved == "hk":
        normalized = normalize_hk_symbol(symbol)
        if not normalized:
            return ""
        return f"{normalized[-4:]}.HK"
    if resolved == "us":
        return normalize_us_symbol(symbol)
    return normalize_cn_symbol(symbol)
