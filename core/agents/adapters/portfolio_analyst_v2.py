"""
组合分析师 v2.0（组合健康度体检）

与单股分析师（fundamentals/market/valuation/sector）不同，本适配器是**组合级**分析师：
它的输入是一份持仓明细文本，而不是单个股票代码。

设计要点（为什么自己实现 execute、不走 super().execute()）：

1. ``holdings`` 是自由文本（多行持仓明细），不是 ticker。基类 ``AnalystAgent.execute``
   走的是「拿 ticker → 调 get_stock_market_data_unified(symbol=ticker)」的单股路径，
   把持仓文本当代码去查必然查不到，LLM 只能靠常识编出伪组合报告。
2. 让 LLM 自主决定调哪个工具不可靠（可能根本不调），也无法做确定性验证。
   因此本适配器在 ``execute`` 里**直接读 state 并直接调用工具函数**，
   用真实数据 + 纯 Python 计算产出报告。这条路径也绕开了 prompt 模板变量层
   （base_vars 只注入 7 个固定变量，模板里也没有 holdings），是本方案降本的关键。

数据来源：
- 日线行情：``core.skill_runtime.data_access.get_stock_daily_quotes``（读本地 MongoDB，不联网）
- 组合有效前沿：``core.tools.implementations.portfolio.quant_tools.get_efficient_frontier``

（注：任务书点名 ``core.tools.external.local_data.get_stock_daily_quotes``，但该函数
对库内带连字符的 trade_date 做字典序范围过滤会静默丢行，见 ``_count_trading_days``
注释；故改用与有效前沿工具同源的、格式兼容的 data_access 版本。）

⚠️ 支持范围：``get_stock_daily_quotes`` 内部有 zfill(6)，只对 A 股 6 位代码正确。
第一版**只对 ``market == "cn"`` 的标的做组合计算**；遇到非 A 股标的时**逐只跳过**
并给出各自 warning，其余 A 股标照常计算（**不做整包降级**——一行非 A 股标的不会让
整个有效前沿消失）。
"""

import json
import logging
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from app.core.market_codes import resolve_instrument, resolve_market
from core.agents.analyst import AnalystAgent
from core.agents.config import (
    AgentCategory,
    AgentInput,
    AgentMetadata,
    AgentOutput,
    LicenseTier,
)
from core.agents.registry import register_agent

logger = logging.getLogger(__name__)


# 分隔符：英文/中文逗号、Tab、普通/全角空格
_TOKEN_SPLIT_RE = re.compile(r"[,\uFF0C\t\u3000\s]+")
# 纯数字 token（可选负号、可选 %、可选小数）。
# 支持负号是为了让 `-30` 这类**负权重**被识别为「权重」而非被当成名称，
# 从而落入下面 `0 < weight <= 100` 的校验 → warning + 跳过（而不是静默吸收成名称）。
_NUM_RE = re.compile(r"^-?\d+(?:\.\d+)?%?$")
# 6 位纯数字（A 股/ETF 固定 6 位）
_SIX_DIGIT_RE = re.compile(r"^\d{6}$")


def _to_float(token: str) -> Optional[float]:
    """把 '30' / '30%' / '1500.5' 这类 token 转成 float，失败返回 None。"""
    if token is None:
        return None
    text = str(token).strip().rstrip("%")
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _is_strong_code_form(token: str) -> bool:
    """
    判断 token 是否「强代码形态」——强到不会与普通英文单词/权重数字混淆。

    命中：
        - 6 位纯数字（``600519``）
        - 含点号（``0700.HK`` / ``600519.SS`` / ``AAPL.O``）
        - 含连字符（``BRK-B``）
    不命中：
        - 纯字母单词（``not`` / ``json`` / ``aa`` / **``AAPL`` —— 纯字母一律视为弱形态**）
        - 1-5 位纯数字（``30`` / ``00700`` —— 也可能是权重/港股代码，需数字支撑才认）

    刻意把纯字母（含 ``AAPL``）归为弱形态：``not`` / ``json`` / ``aa`` 与 ``AAPL``
    在形态上无法区分，而「误造幻影标的」的代价远高于「要求用户补一个权重数字」。
    """
    if not token:
        return False
    if _SIX_DIGIT_RE.match(token):
        return True
    if "." in token or "-" in token:
        return True
    return False


# 弱形态候选的「本地库存在性」缓存：(market, symbol) -> bool
_LOCAL_UNIVERSE_CACHE: Dict[Tuple[str, str], bool] = {}
# 该市场的基础信息集合是否「可判定」：False 表示集合为空或查询异常，
# 此时对弱形态 fail-open（不误杀用户输入）。
_LOCAL_UNIVERSE_USABLE: Dict[str, bool] = {}


def _symbol_exists_locally(market: str, symbol: str) -> Optional[bool]:
    """
    弱形态候选的「本地库存在性」校验 —— 第 8 轮报告 P1-1 修法的后半句。

    弱形态（1-5 位数字、纯字母）与「权重数字」「普通英文单词」在**形态上不可分**，
    仅靠「数字支撑」仍会把备注行解析成幻影标的（实测 ``aa,30`` → 幻影美股 ``AA``、
    ``30, 25`` → 幻影港股 ``00030``）。因此弱形态候选必须**在本市场的基础信息集合里
    查得到**才认；强形态（6 位数字 / 带点号 / 带连字符）不做此校验。

    Returns:
        - ``True`` ：命中
        - ``False``：集合可查但未命中（确定的「不存在」）
        - ``None`` ：无法判定（未持有 DB / 集合为空 / 查询异常）→ 调用方 fail-open，
          保持既有行为，避免因数据库抖动把用户的合法输入误杀。
    """
    if not market or not symbol:
        return None
    key = (market, symbol)
    if key in _LOCAL_UNIVERSE_CACHE:
        return _LOCAL_UNIVERSE_CACHE[key]
    if _LOCAL_UNIVERSE_USABLE.get(market) is False:
        return None

    try:
        from app.core.database import get_mongo_db_sync
        from app.core.market_codes import basic_info_collection

        coll = get_mongo_db_sync()[basic_info_collection(market)]
        if coll.estimated_document_count() == 0:
            # 该市场基础信息集合为空（尚未预热）→ 不可判定，fail-open。
            _LOCAL_UNIVERSE_USABLE[market] = False
            return None
        result = coll.find_one({"code": symbol}, {"_id": 0, "code": 1}) is not None
    except Exception as exc:  # pragma: no cover - 依赖 DB 可用性
        logger.warning(
            "⚠️ [组合分析师 v2] 基础信息集合查询失败，弱形态代码校验 fail-open：%s", exc
        )
        _LOCAL_UNIVERSE_USABLE[market] = False
        return None

    _LOCAL_UNIVERSE_CACHE[key] = result
    return result


def parse_holdings(text: str) -> Tuple[List[Dict[str, Any]], List[str]]:
    """
    把持仓明细文本解析成结构化列表。

    支持的真实输入格式（用户输入并不统一，必须容错）：
        ``600519,30,1500``        （代码,权重%,成本价）
        ``600519 贵州茅台 30%``   （代码 名称 权重%）
    容忍逗号/空格/Tab 混合分隔、中文名称夹在中间、权重带 ``%``、
    空行、以 ``#`` 开头的注释行。

    ⚠️ 弱形态代码（1-5 位数字、纯字母）必须同时具备「数字支撑」与「本地库存在性」
    才被认定为代码，避免把备注行/英文单词解析成幻影标的（见 ``_symbol_exists_locally``）。

    Args:
        text: 持仓明细文本

    Returns:
        ``(positions, warnings)``。
        positions 每项形如::

            {"code": 内部 symbol, "raw_code": 原始输入, "name": 名称或 None,
             "weight": float 或 None, "cost": float 或 None, "market": "cn"/"hk"/"us"}

        解析失败的行（无法识别代码、权重非法等）**不静默丢弃**，
        会以「第 N 行 ...」的形式写入 warnings。
    """
    positions: List[Dict[str, Any]] = []
    warnings: List[str] = []

    if text is None:
        return positions, warnings
    raw_text = str(text)
    if not raw_text.strip():
        return positions, warnings

    for idx, raw_line in enumerate(raw_text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        tokens = [t for t in _TOKEN_SPLIT_RE.split(line) if t]
        if not tokens:
            continue

        # ---- 1. 定位代码 token（强形态优先；弱形态需「数字支撑」） ----
        code_token: Optional[str] = None
        code_symbol = ""
        code_market = ""

        # 1a. 强形态优先：遍历全部 token，第一个解析出 symbol 的即为代码
        for tok in tokens:
            if not _is_strong_code_form(tok):
                continue
            market, symbol = resolve_instrument(tok)
            if symbol:
                code_token, code_symbol, code_market = tok, symbol, market
                break

        # 1b. 弱形态兜底：强形态没命中时，遍历**全部** token（不是只取 tokens[0]），
        #     且同时满足两个条件才认：
        #       (a) 「数字支撑」——除该 token 外剩余 token 中至少有一个是数字；
        #       (b) 「本地库查得到」——_symbol_exists_locally() 不为 False。
        #     只做 (a) 是不够的：`aa,30` 与 `30, 25` 都有数字支撑，却会造出
        #     幻影美股 `AA` / 幻影港股 `00030`（第 8 轮 P1-1 实测复现）。
        #     典型输入：
        #       not json at all  → 任何候选都没有数字支撑 → 全部跳过 → warning（不造幻影标的）
        #       腾讯控股,00700,30 → 00700 有数字支撑且港股库内存在 → 命中（修 P2-8：代码不在行首）
        #       AAPL,30           → 美股库内存在 → 保留美股裸 ticker 支持
        #       aa,30 / 30, 25    → 库内查无此码 → 跳过 + warning（不再造幻影标的）
        #       AAPL（单独一行）   → 弱形态但无数字支撑 → 跳过 + warning（有意取舍，宁可让用户补权重）
        if not code_symbol:
            for i, tok in enumerate(tokens):
                others = tokens[:i] + tokens[i + 1:]
                if not any(_NUM_RE.match(o) for o in others):
                    continue
                market, symbol = resolve_instrument(tok)
                if not symbol:
                    continue
                if _symbol_exists_locally(market, symbol) is False:
                    continue
                code_token, code_symbol, code_market = tok, symbol, market
                break

        if not code_symbol:
            warnings.append(f"第 {idx} 行 '{line}' 无法识别为证券代码，已跳过")
            continue

        # ---- 2. 拆分剩余 token：数字（权重/成本）与名称 ----
        remainder = list(tokens)
        remainder.remove(code_token)
        numeric_tokens: List[str] = []
        name_parts: List[str] = []
        for tok in remainder:
            if _NUM_RE.match(tok):
                numeric_tokens.append(tok)
            else:
                name_parts.append(tok)

        pct_tokens = [t for t in numeric_tokens if t.endswith("%")]
        plain_tokens = [t for t in numeric_tokens if not t.endswith("%")]

        weight: Optional[float] = None
        cost: Optional[float] = None
        extra_numeric: List[str] = []
        if pct_tokens:
            # 显式带 % 的优先当权重；其余纯数字第一个当成本价
            weight = _to_float(pct_tokens[0])
            if plain_tokens:
                cost = _to_float(plain_tokens[0])
                extra_numeric = pct_tokens[1:] + plain_tokens[1:]
            else:
                extra_numeric = pct_tokens[1:]
        elif plain_tokens:
            # 无 % 时按位置：第一个数字=权重，第二个=成本价
            weight = _to_float(plain_tokens[0])
            if len(plain_tokens) >= 2:
                cost = _to_float(plain_tokens[1])
                extra_numeric = plain_tokens[2:]
            else:
                extra_numeric = plain_tokens[1:]

        if weight is not None and not (0 < weight <= 100):
            warnings.append(
                f"第 {idx} 行 '{line}' 权重 {weight:g} 非法（应在 0~100 之间），已跳过"
            )
            continue

        if cost is not None and cost <= 0:
            warnings.append(
                f"第 {idx} 行 '{line}' 成本价 {cost:g} 非法（应大于 0），已忽略成本价"
            )
            cost = None

        # 第 3 个及以后的数字 token 不静默丢弃
        if extra_numeric:
            warnings.append(
                f"第 {idx} 行存在多余数值 token（{'、'.join(extra_numeric)}），已忽略"
            )

        name = " ".join(name_parts).strip() or None
        positions.append(
            {
                "code": code_symbol,
                "raw_code": code_token,
                "name": name,
                "weight": weight,
                "cost": cost,
                "market": code_market,
            }
        )

    return positions, warnings


def _hhi_judgement(hhi: float) -> str:
    """HHI（0~1 小数口径）的常用分档定性判断。"""
    if hhi < 0.15:
        return "较为分散（单一标的影响力有限）"
    if hhi <= 0.25:
        return "中度集中（需关注前几大标的的共振风险）"
    return "高度集中（组合表现高度依赖少数标的）"


def compute_concentration(
    positions: List[Dict[str, Any]],
    warnings: Optional[List[str]] = None,
) -> Optional[Dict[str, Any]]:
    """
    纯 Python 计算集中度指标（不依赖 app.services.portfolio_service 的 PositionResponse）。

    - 权重缺失时按**等权**处理，并在 warnings 说明；
    - 部分缺失时用已提供权重的均值填充，并在 warnings 说明；
    - HHI = Σ w_i^2（w_i 为 0~1 的小数权重），同时给出 ×10000 的「点数」口径。

    Returns:
        ``{"top1_pct","top3_pct","top5_pct","hhi","hhi_points","judgement",
           "weight_basis","weights_used","sum_input_pct"}``；positions 为空时返回 None。
    """
    warn = warnings if warnings is not None else []
    n = len(positions)
    if n == 0:
        return None

    provided = [float(p["weight"]) for p in positions if p.get("weight") is not None]

    if not provided:
        weights = [100.0 / n] * n
        weight_basis = "equal"
        warn.append(f"持仓未提供权重，集中度按等权（各 {100.0 / n:.2f}%）计算。")
    else:
        avg = sum(provided) / len(provided)
        missing = 0
        weights = []
        for p in positions:
            if p.get("weight") is None:
                weights.append(avg)
                missing += 1
            else:
                weights.append(float(p["weight"]))
        if missing:
            weight_basis = "mixed"
            warn.append(
                f"有 {missing} 只标的未提供权重，已按已提供权重的均值（{avg:.2f}%）填充。"
            )
        else:
            weight_basis = "provided"

    total = sum(weights)
    if total <= 0:
        weights = [100.0 / n] * n
        total = 100.0
        weight_basis = "equal"
        warn.append("权重合计非正，已回退为等权计算集中度。")

    norm = [w / total * 100.0 for w in weights]
    ordered = sorted(norm, reverse=True)
    fracs = [w / 100.0 for w in norm]
    hhi = sum(f * f for f in fracs)

    # weights_used 按 code 求和：同一 code 出现多行时，后者不得覆盖前者（否则展示值
    # 会与 HHI 使用的多行权重口径自相矛盾）。出现多行时显式告警「权重已合并」。
    weights_used: Dict[str, float] = {}
    for p, w in zip(positions, norm):
        weights_used[p["code"]] = weights_used.get(p["code"], 0.0) + w
    weights_used = {code: round(w, 2) for code, w in weights_used.items()}

    dup_codes = sorted(
        {c for c in (p["code"] for p in positions)
         if sum(1 for p in positions if p["code"] == c) > 1}
    )
    if dup_codes:
        warn.append(f"标的 {'、'.join(dup_codes)} 出现多行，权重已合并。")

    return {
        "top1_pct": round(ordered[0], 2),
        "top3_pct": round(sum(ordered[:3]), 2),
        "top5_pct": round(sum(ordered[:5]), 2),
        "hhi": round(hhi, 4),
        "hhi_points": round(hhi * 10000, 1),
        "judgement": _hhi_judgement(hhi),
        "weight_basis": weight_basis,
        "weights_used": weights_used,
        "sum_input_pct": round(sum(provided), 2) if provided else None,
    }


def _normalize_trade_date(value: Any) -> str:
    """把库内 ``trade_date`` 的两种存储形态（``YYYYMMDD`` / ``YYYY-MM-DD``）统一成 ``YYYY-MM-DD``。

    库内两种形态并存（见 ``data_access.get_stock_daily_quotes`` 的日期过滤器同时匹配
    两种写法）。无法识别时返回空串——宁可判为「不可用」，也不要猜一个日期出来。
    """
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(digits) != 8:
        return ""
    return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"


def _count_trading_days(
    codes: List[str], lookback_days: int
) -> Tuple[Dict[str, Any], Dict[str, str], Optional[str]]:
    """
    从本地行情库读取每只标的的日线根数（数据可用性证据）与**最后一根 K 线日期**。

    Returns:
        ``(symbol_counts, symbol_last_dates, error_message)``；
        error_message 为 None 表示正常；``symbol_last_dates`` 只包含真正读到数据的标的。
    """
    if not codes:
        return {}, {}, None
    try:
        # 用 data_access 版本（与 get_efficient_frontier 内部一致）：
        # 它同时兼容库内 YYYYMMDD 与 YYYY-MM-DD 两种 trade_date 存储，而
        # core.tools.external.local_data.get_stock_daily_quotes 会把入参日期去掉
        # 连字符后对库内「带连字符」的字符串做字典序范围过滤（local_data.py:102-109），
        # 实测会静默丢掉 2025-10~12 的行（600519 只返回 183 根，正确应为 241 根）。
        # 若沿用它会得到一个「真实但被截断」的错误根数，与工具报告的重叠交易日自相矛盾。
        from core.skill_runtime.data_access import get_stock_daily_quotes
    except Exception as exc:  # pragma: no cover - 导入失败属环境异常
        return {}, {}, f"本地行情库工具不可用：{exc}"

    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=int(lookback_days) * 2 + 50)).strftime("%Y-%m-%d")

    counts: Dict[str, Any] = {}
    last_dates: Dict[str, str] = {}
    for code in codes:
        try:
            rows = get_stock_daily_quotes(
                code, start, end, limit=int(lookback_days) + 100
            ) or []
            counts[code] = len(rows)
            # 用 max() 而不是取末行：不依赖函数是否真的按 trade_date 升序返回，
            # 归一化成 YYYY-MM-DD 后字典序最大即最新交易日。
            dates = [_normalize_trade_date((r or {}).get("trade_date")) for r in rows]
            dates = [d for d in dates if d]
            if dates:
                last_dates[code] = max(dates)
        except Exception as exc:  # 单只失败不影响其他
            counts[code] = None
            logger.warning("读取 %s 日线失败：%s", code, exc)
    return counts, last_dates, None


def _warning_mentions_code(text: str, code: str) -> bool:
    """
    按**词边界**判断告警文本是否提及某代码。

    不能用 ``code in str(w)`` 直接子串匹配：短代码（如 ``30``）会误配到别的告警文本
    （例如 ``"300 数据不足"`` 里含有子串 ``30``），把归因挂到错误的标的上。
    """
    if not code:
        return False
    pattern = rf"(?<![0-9A-Za-z]){re.escape(code)}(?![0-9A-Za-z])"
    return re.search(pattern, str(text)) is not None


def _extract_excluded(
    requested: List[str], valid: List[str], ef_warnings: List[str]
) -> List[Dict[str, str]]:
    """从有效前沿结果里推断「因数据不足被排除的标的及原因」。"""
    valid_set = set(valid or [])
    excluded: List[Dict[str, str]] = []
    for code in requested:
        if code in valid_set:
            continue
        reason: Optional[str] = None
        for w in ef_warnings:
            if _warning_mentions_code(str(w), code):
                reason = str(w)
                break
        if reason is None:
            reason = "数据不足或未参与组合计算（详见工具告警）"
        excluded.append({"symbol": code, "reason": reason})
    return excluded


def _run_efficient_frontier(
    positions: List[Dict[str, Any]], lookback_days: int
) -> Tuple[Optional[Dict[str, Any]], List[str], List[Dict[str, str]]]:
    """
    调用组合有效前沿工具，返回 ``(frontier, warnings, excluded)``。

    frontier 为 None 表示指标不可用（标的不足 / 工具报错 / 解析失败），
    此时 warnings 里会写明原因，报告只写「不可用 + 原因」，绝不编造数值。
    """
    warnings: List[str] = []
    excluded: List[Dict[str, str]] = []

    codes: List[str] = []
    for p in positions:
        code = p.get("code")
        if code and code not in codes:
            codes.append(code)

    if len(codes) < 2:
        warnings.append("可用 A 股标的不足 2 只，无法计算组合有效前沿。")
        return None, warnings, excluded

    try:
        from core.tools.implementations.portfolio.quant_tools import get_efficient_frontier
        raw = get_efficient_frontier.invoke(
            {"symbols": ",".join(codes), "lookback_days": int(lookback_days)}
        )
    except Exception as exc:
        warnings.append(f"组合有效前沿计算失败：{exc}")
        return None, warnings, excluded

    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except Exception as exc:
        warnings.append(f"组合有效前沿返回无法解析：{exc}")
        return None, warnings, excluded

    if not isinstance(data, dict):
        warnings.append("组合有效前沿返回格式异常，指标不可用。")
        return None, warnings, excluded

    ef_warnings = [str(w) for w in (data.get("warnings") or [])]

    if data.get("status") != "success":
        message = str(data.get("message") or "未知错误")
        warnings.append(f"组合有效前沿不可用：{message}")
        warnings.extend(ef_warnings)
        excluded = _extract_excluded(codes, [], ef_warnings)
        return (
            {"status": data.get("status"), "message": message, "warnings": ef_warnings},
            warnings,
            excluded,
        )

    stats = data.get("portfolio_stats") or {}
    valid = stats.get("valid_symbols") or []
    warnings.extend(ef_warnings)
    excluded = _extract_excluded(codes, valid, ef_warnings)

    frontier = {
        "status": "success",
        "max_sharpe": data.get("max_sharpe"),
        "min_vol": data.get("min_vol"),
        "portfolio_stats": stats,
        "warnings": ef_warnings,
    }
    return frontier, warnings, excluded


def compute_portfolio_facts(
    positions: List[Dict[str, Any]],
    market_type: str = "cn",
    lookback_days: int = 252,
) -> Dict[str, Any]:
    """
    用**真实数据**计算组合指标。

    Args:
        positions: ``parse_holdings`` 的输出
        market_type: 市场类型（当前仅 cn 参与组合计算；非 cn 标的逐只跳过，不做整包降级）
        lookback_days: 回看交易日数

    Returns:
        facts 字典，含 ``positions`` / ``concentration`` / ``efficient_frontier`` /
        ``excluded_symbols`` / ``data_quality`` / ``warnings`` / ``cn_only``。
    """
    warnings: List[str] = []
    facts: Dict[str, Any] = {
        "market_type": market_type,
        "positions": list(positions or []),
        "cn_only": False,
        "concentration": None,
        "efficient_frontier": None,
        "excluded_symbols": [],
        "data_quality": {},
        "warnings": warnings,
    }

    if not positions:
        warnings.append("未提供有效持仓，无法计算组合指标。")
        facts["data_quality"] = {
            "num_positions": 0,
            "overlapping_trading_days": None,
            "num_excluded": 0,
            "cn_only": False,
            "symbol_trading_days": {},
        }
        return facts

    resolved_market = resolve_market(market_type)

    # 逐个标的判定市场：只让 cn 标的参与组合计算，非 cn 标的**单独跳过**（不整包降级）。
    # 有效前沿是本报告的核心价值，绝不能被一行非 A 股标的整体一票否决。
    cn_positions: List[Dict[str, Any]] = []
    skipped_non_cn: Dict[str, str] = {}  # code -> market（去重保序）
    for p in positions:
        code = p.get("code")
        market = p.get("market") or resolved_market
        if market == "cn":
            cn_positions.append(p)
        elif code and code not in skipped_non_cn:
            skipped_non_cn[code] = market

    excluded: List[Dict[str, str]] = []
    if skipped_non_cn:
        facts["cn_only"] = True
        for code, mkt in skipped_non_cn.items():
            msg = (
                f"标的 {code} 属于 {mkt} 市场，本版本仅支持 A 股持仓，该项已跳过。"
            )
            warnings.append(msg)
            excluded.append({"symbol": code, "reason": msg})

    # 集中度：纯 Python，不需要行情数据
    facts["concentration"] = compute_concentration(positions, warnings)

    # 数据可用性：只对 cn 标的查本地 A 股日线集合。
    # 非 cn 代码（如港股 00700）会被 zfill(6) 成 000700 去查 A 股集合，必然 0 行，
    # 报告展示「00700：0 根」会被误读成「数据缺失」——故非 cn 标的在报告里标注
    # 「未查询（市场不支持）」，而不是 0 根。
    cn_codes: List[str] = []
    for p in cn_positions:
        code = p.get("code")
        if code and code not in cn_codes:
            cn_codes.append(code)
    symbol_days, symbol_last_dates, quotes_error = _count_trading_days(
        cn_codes, lookback_days
    )
    if quotes_error:
        warnings.append(f"读取本地行情库失败：{quotes_error}")

    # 数据窗口：取本地库内**真实存在**的最后一根 K 线日期，而不是 datetime.now()。
    # 用 now() 会出现「标注截至今天、实际数据只到上周」的误导（本地日线未同步时）。
    # 无任何可用日线时留空，由渲染层显式标注「不可用」而不是硬填一个日期。
    data_window_end = max(symbol_last_dates.values()) if symbol_last_dates else ""
    facts["data_window_end"] = data_window_end
    facts["symbol_last_dates"] = symbol_last_dates

    # 本地库日线明显落后于今天时显式提示。阈值取 20 个自然日，避开周末与长假误报。
    if data_window_end:
        try:
            _stale_days = (
                datetime.now().date()
                - datetime.strptime(data_window_end, "%Y-%m-%d").date()
            ).days
            if _stale_days > 20:
                warnings.append(
                    f"本地库日线最新一根为 {data_window_end}，距今日已 {_stale_days} 天，"
                    "数据可能未同步至最新交易日。"
                )
        except (TypeError, ValueError):  # pragma: no cover - 日期形态已在上游归一化
            pass

    # 有效前沿：始终用剩余的 cn 标的计算（不足 2 只或工具失败/解析失败时不可用）
    overlapping = None
    frontier, ef_warnings, ef_excluded = _run_efficient_frontier(
        cn_positions, lookback_days
    )
    warnings.extend(ef_warnings)
    excluded.extend(ef_excluded)
    facts["efficient_frontier"] = frontier
    facts["excluded_symbols"] = excluded
    if frontier and frontier.get("status") == "success":
        overlapping = (frontier.get("portfolio_stats") or {}).get(
            "overlapping_trading_days"
        )

    facts["data_quality"] = {
        "num_positions": len(positions),
        "overlapping_trading_days": overlapping,
        "num_excluded": len(excluded),
        "cn_only": facts["cn_only"],
        "symbol_trading_days": symbol_days,
    }
    return facts


def _fmt_pct(value: Optional[float]) -> str:
    """小数收益率/波动率 → 百分比字符串。"""
    if value is None:
        return "—"
    try:
        return f"{float(value) * 100:.2f}%"
    except (TypeError, ValueError):
        return "—"


def render_portfolio_report(
    facts: Dict[str, Any],
    analysis_date: Optional[str] = None,
    parse_warnings: Optional[List[str]] = None,
    total_position_pct: Optional[Any] = None,
    risk_preference: Optional[str] = None,
    benchmark: Optional[str] = None,
) -> str:
    """把 facts（+ 解析告警）渲染成 Markdown 组合体检报告。"""
    lines: List[str] = []
    positions = facts.get("positions") or []
    warnings = list(facts.get("warnings") or [])
    for w in parse_warnings or []:
        if w not in warnings:
            warnings.insert(0, w)

    lines.append("# 持仓组合健康度体检")
    lines.append("")
    # 数据窗口：回显本地库内**真实**的最后一根 K 线日期，而不是 datetime.now()。
    # 用 now() 会出现「标注截至今天、实际数据只到上周」的误导（本地日线未同步时）；
    # 取不到任何可用日线时如实写「不可用」，绝不硬填一个日期。
    data_window_end = str(facts.get("data_window_end") or "").strip()
    meta_bits = []
    if analysis_date:
        meta_bits.append(f"分析日期：{analysis_date}")
    meta_bits.append(f"市场：{facts.get('market_type', 'cn')}")
    if data_window_end:
        meta_bits.append(f"数据窗口：截至 {data_window_end}（本地库最后一根）")
    else:
        meta_bits.append("数据窗口：不可用（本地库无可用日线）")
    if total_position_pct not in (None, ""):
        meta_bits.append(f"当前总仓位：{total_position_pct}%")
    if risk_preference:
        meta_bits.append(f"风险偏好：{risk_preference}")
    if benchmark:
        meta_bits.append(f"业绩基准：{benchmark}")
    lines.append("　|　".join(meta_bits))
    lines.append("")
    lines.append(
        "> 本报告仅包含由本地行情库真实数据计算得到的指标；任何无法计算的项"
        "均明确标注为「不可用」并给出原因，不含推测数值。"
    )
    lines.append("")

    # ---- 1. 持仓明细 ----
    lines.append("## 一、持仓明细")
    lines.append("")
    if not positions:
        lines.append("未提供可识别的持仓明细。")
    else:
        lines.append("| 代码 | 名称 | 权重(%) | 成本价 |")
        lines.append("| --- | --- | --- | --- |")
        for p in positions:
            name = p.get("name") or "—"
            weight = "—" if p.get("weight") is None else f"{float(p['weight']):g}"
            cost = "—" if p.get("cost") is None else f"{float(p['cost']):g}"
            lines.append(f"| {p.get('code', '—')} | {name} | {weight} | {cost} |")
    lines.append("")

    # ---- 2. 集中度 ----
    lines.append("## 二、集中度分析")
    lines.append("")
    conc = facts.get("concentration")
    if not conc:
        lines.append("不可用：没有可用的持仓权重，无法计算集中度。")
    else:
        lines.append(f"- 第一大标的权重：{conc['top1_pct']:.2f}%")
        lines.append(f"- 前三大合计：{conc['top3_pct']:.2f}%")
        lines.append(f"- 前五大合计：{conc['top5_pct']:.2f}%")
        lines.append(
            f"- 赫芬达尔指数 HHI：{conc['hhi']:.4f}（{conc['hhi_points']:.1f} 点，"
            f"0~1 小数口径）"
        )
        lines.append(f"- 定性判断：{conc['judgement']}")
        basis_label = {
            "provided": "按你提供的权重计算",
            "equal": "按等权计算（未提供权重）",
            "mixed": "部分权重缺失，缺失项按均值填充后计算",
        }.get(conc.get("weight_basis"), "按归一化权重计算")
        lines.append(f"- 权重口径：{basis_label}（已归一化到合计 100%）")
        if conc.get("sum_input_pct") is not None:
            lines.append(f"- 原始权重合计：{conc['sum_input_pct']:.2f}%")
    lines.append("")

    # ---- 3. 组合风险指标 ----
    lines.append("## 三、组合风险指标（有效前沿）")
    lines.append("")
    frontier = facts.get("efficient_frontier")
    if not frontier:
        lines.append("不可用：未能计算组合有效前沿（标的不足或数据不足，详见下方数据完整性声明）。")
    elif frontier.get("status") != "success":
        lines.append(
            f"不可用：{frontier.get('message', '组合有效前沿计算失败')}"
            "（详见下方数据完整性声明）。"
        )
    else:
        max_sharpe = frontier.get("max_sharpe") or {}
        min_vol = frontier.get("min_vol") or {}
        stats = frontier.get("portfolio_stats") or {}
        lines.append("**最大夏普比率组合**")
        lines.append("")
        lines.append(f"- 年化收益：{_fmt_pct(max_sharpe.get('return'))}")
        lines.append(f"- 年化波动率：{_fmt_pct(max_sharpe.get('volatility'))}")
        sr = max_sharpe.get("sharpe_ratio")
        lines.append(f"- 夏普比率：{'—' if sr is None else f'{float(sr):.3f}'}")
        lines.append("")
        lines.append("**最小波动组合**")
        lines.append("")
        lines.append(f"- 年化收益：{_fmt_pct(min_vol.get('return'))}")
        lines.append(f"- 年化波动率：{_fmt_pct(min_vol.get('volatility'))}")
        lines.append("")
        valid = stats.get("valid_symbols") or []
        lines.append(
            f"- 参与计算的标的：{', '.join(valid) if valid else '（无）'}"
        )
        od = stats.get("overlapping_trading_days")
        lines.append(f"- 重叠交易日：{'—' if od is None else od} 个")
    lines.append("")

    # ---- 4. 数据完整性声明 ----
    lines.append("## 四、数据完整性声明")
    lines.append("")
    dq = facts.get("data_quality") or {}
    lines.append(f"- 持仓标的数：{dq.get('num_positions', len(positions))}")
    lines.append(
        f"- 重叠交易日：{'不可用' if dq.get('overlapping_trading_days') is None else str(dq.get('overlapping_trading_days')) + ' 个'}"
    )
    lines.append(f"- 被排除标的数：{dq.get('num_excluded', len(facts.get('excluded_symbols') or []))}")

    symbol_days = dq.get("symbol_trading_days") or {}
    # 逐只列出数据可用性。非 cn 标的**未查询**本地 A 股日线（港股 00700 会被 zfill 成
    # 000700，必然 0 行），故标注「未查询（市场不支持）」而不是「0 根」。
    seen_codes: List[str] = []
    day_rows: List[str] = []
    for p in positions:
        code = p.get("code")
        if not code or code in seen_codes:
            continue
        seen_codes.append(code)
        if (p.get("market") or "cn") != "cn":
            day_rows.append(f"    - {code}：未查询（市场不支持）")
        elif code in symbol_days:
            cnt = symbol_days[code]
            day_rows.append(
                f"    - {code}：{'读取失败' if cnt is None else str(cnt) + ' 根'}"
            )
    for code, cnt in symbol_days.items():
        if code in seen_codes:
            continue
        day_rows.append(f"    - {code}：{'读取失败' if cnt is None else str(cnt) + ' 根'}")
    if day_rows:
        lines.append("- 各标的本地日线根数（数据可用性证据）：")
        lines.extend(day_rows)

    excluded = facts.get("excluded_symbols") or []
    if excluded:
        lines.append("")
        lines.append("**因数据不足被排除的标的：**")
        lines.append("")
        for item in excluded:
            lines.append(f"- {item.get('symbol')}：{item.get('reason')}")

    if warnings:
        lines.append("")
        lines.append("**告警与降级说明：**")
        lines.append("")
        for w in warnings:
            lines.append(f"- {w}")

    lines.append("")
    lines.append("## 五、说明")
    lines.append("")
    if facts.get("cn_only"):
        skipped_codes: List[str] = []
        for p in positions:
            code = p.get("code")
            mkt = p.get("market")
            if code and mkt and mkt != "cn" and code not in skipped_codes:
                skipped_codes.append(code)
        detail = "、".join(skipped_codes) if skipped_codes else "—"
        lines.append(
            f"- ⚠️ 当前版本**仅支持 A 股持仓**；已跳过 {len(skipped_codes)} 只非 A 股标的"
            f"（{detail}），其余 A 股标的正常计算。"
        )
    if not positions:
        lines.append("- 未提供任何持仓，未查询任何行情数据，也未输出任何组合指标。")
    lines.append(
        "- 组合风险指标基于马科维茨均值-方差模型（最大夏普/最小波动），"
        "由本地历史日线计算得到，不含前瞻或推测数值。"
    )
    lines.append("")

    return "\n".join(lines)


_NO_HOLDINGS_REPORT = """# 持仓组合健康度体检

**未提供持仓明细（holdings 为空）**：本次工作流输入中不包含可用的持仓信息，
因此**未查询任何行情数据，也不输出任何组合指标**（集中度、波动率、夏普等一律留空）。

请在流程输入中提供持仓明细：每行一只股票，支持如下格式：

- `代码,权重%,成本价`（例如「代码 逗号 权重 逗号 成本价」）
- `代码 名称 权重%`

其中权重可带 `%`，成本价为可选。解析失败的行会在报告中显式列出，不会被静默丢弃。
"""


@register_agent
class PortfolioAnalystV2(AnalystAgent):
    """
    组合分析师 v2.0

    面向「持仓组合健康度体检」这类**组合级**研究工作流：输入是持仓明细文本，
    输出是基于本地行情真实数据计算得到的组合体检报告。

    与单股分析师的区别：本适配器直接读 ``state["holdings"]`` 并直接调用工具函数，
    不依赖 prompt 模板变量传递，也不让 LLM 自主决定调工具。
    """

    metadata = AgentMetadata(
        id="portfolio_analyst_v2",
        name="组合分析师 v2.0",
        description="解析持仓明细并从本地行情真实数据计算组合集中度与风险收益特征，产出组合健康度体检报告",
        category=AgentCategory.ANALYST,
        version="1.0.0",
        license_tier=LicenseTier.FREE,
        default_tools=["get_efficient_frontier"],
        inputs=[
            AgentInput(
                name="holdings",
                type="string",
                description="持仓明细，每行一只：代码,权重%,成本价",
            ),
            AgentInput(name="analysis_date", type="string", description="分析日期"),
            AgentInput(
                name="market_type",
                type="string",
                description="市场类型，当前仅支持 cn",
            ),
            AgentInput(
                name="total_position_pct",
                type="number",
                description="当前总仓位(%)",
                required=False,
            ),
            AgentInput(
                name="risk_preference",
                type="string",
                description="风险偏好",
                required=False,
            ),
            AgentInput(
                name="benchmark",
                type="string",
                description="业绩基准",
                required=False,
            ),
        ],
        outputs=[
            AgentOutput(
                name="portfolio_report",
                type="string",
                description="组合健康度体检报告",
            ),
        ],
        requires_tools=True,
        output_field="portfolio_report",
        report_label="【组合体检】",
        workflow_stage="analyst",
    )

    # 分析师类型
    analyst_type = "portfolio"

    # 输出字段名
    output_field = "portfolio_report"

    def execute(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """
        执行组合健康度体检。

        自己实现（不走 ``super().execute()``，那是单股 ticker 路径）：
        直接读 state 的 ``holdings``，解析 → 真实数据计算 → 渲染报告。
        """
        logger.info("[组合分析师v2] 开始执行组合健康度体检")

        try:
            raw = state.get("holdings")
            if raw is None or not str(raw).strip():
                logger.warning("[组合分析师v2] state 中未提供 holdings，返回未提供持仓提示")
                return {self.output_field: _NO_HOLDINGS_REPORT}

            positions, parse_warnings = parse_holdings(str(raw))
            if not positions:
                logger.warning("[组合分析师v2] holdings 无法解析出任何有效标的")
                detail = "\n".join(f"- {w}" for w in parse_warnings) or "- （无更多细节）"
                return {
                    self.output_field: (
                        _NO_HOLDINGS_REPORT
                        + "\n**解析详情：**\n\n"
                        + detail
                        + "\n"
                    )
                }

            market_type = state.get("market_type") or "cn"
            analysis_date = (
                state.get("analysis_date")
                or state.get("trade_date")
                or state.get("end_date")
            )
            if analysis_date and hasattr(analysis_date, "strftime"):
                analysis_date = analysis_date.strftime("%Y-%m-%d")

            facts = compute_portfolio_facts(positions, market_type=market_type)
            report = render_portfolio_report(
                facts,
                analysis_date=analysis_date,
                parse_warnings=parse_warnings,
                total_position_pct=state.get("total_position_pct"),
                risk_preference=state.get("risk_preference"),
                benchmark=state.get("benchmark"),
            )
            logger.info(
                "[组合分析师v2] 报告生成完成，持仓 %d 只，长度 %d 字符",
                len(positions),
                len(report),
            )
            return {self.output_field: report}

        except Exception as exc:  # 兜底：明确报错，绝不编造
            logger.error("[组合分析师v2] 执行失败: %s", exc, exc_info=True)
            return {
                self.output_field: (
                    "# 持仓组合健康度体检\n\n"
                    f"**执行失败**：{exc}\n\n"
                    "未能完成组合指标计算，报告不包含任何推测数值。"
                )
            }

    # ------------------------------------------------------------------ #
    # 基类抽象方法的最小实现。
    # 本适配器的 execute 不构建提示词、不调用 LLM，但 AnalystAgent 用
    # @abstractmethod 声明了这两个方法，不实现则类无法实例化。
    # ------------------------------------------------------------------ #
    def _build_system_prompt(
        self, market_type: str = None, context=None, state: Dict[str, Any] = None
    ) -> str:
        """组合分析师不走 LLM 路径，此方法仅为满足基类抽象接口。"""
        return "组合分析师（确定性计算，不调用 LLM）"

    def _build_user_prompt(
        self,
        ticker: str,
        analysis_date: str,
        tool_data: Dict[str, Any],
        state: Dict[str, Any],
    ) -> str:
        """组合分析师不走 LLM 路径，此方法仅为满足基类抽象接口。"""
        return "组合分析师（确定性计算，不调用 LLM）"
