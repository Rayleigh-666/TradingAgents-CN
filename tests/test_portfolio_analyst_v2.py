"""
组合分析师 v2.0（``portfolio_analyst_v2``）离线单测。

覆盖：
- ``parse_holdings`` 对多种真实输入格式的解析与容错（逗号/空格/Tab、中文名、``%``、
  空行、``#`` 注释行、无法识别代码的行）；
- P1-1 幻影输入（``not json at all`` / ``aa,600519,30``）：纯字母弱形态不得造出美股幻影标的；
- P1-1 后半句：弱形态候选**必须本地库查得到**才认（``aa,30`` / ``30, 25`` 不再造幻影标的），
  库不可用时 fail-open、强形态不做查库（各有独立用例）；
- P2-8 代码不在行首（``腾讯控股,00700,30``）、美股裸 ticker（``AAPL,30``）仍可解析；
- P2-6 负权重被识别为权重并拒绝；P2-4 重复代码按 code 求和 + 「多行已合并」告警；
- P1-2 一行垃圾不得让有效前沿整体消失；非 cn 标的逐只跳过、「未查询（市场不支持）」；
- 有效前沿失败三路径（标的 <2 / 工具抛错 / 返回非 JSON）各自「不可用 + 原因」且不编造数值；
- ``compute_concentration`` 的 HHI / topN 数值正确性（等权兜底也覆盖）；
- ``execute`` 在 holdings 缺失/为空时返回明确「未提供持仓」提示，且**不含任何数值**；
- ``_count_trading_days`` 的导入源防线（``core.skill_runtime.data_access``）。

跑法（必须在清空环境后执行，否则 import tradingagents 会触发 config.mkdir 的
PermissionError）::

    env -i HOME="$HOME" PATH="$PATH" LANG=zh_CN.UTF-8 \
        .venv/bin/python -m pytest tests/test_portfolio_analyst_v2.py -v
"""

import re

import pytest

import core.agents.adapters.portfolio_analyst_v2 as pa


@pytest.fixture(autouse=True)
def _clear_local_universe_cache():
    """每个用例前清空「本地库存在性」缓存，避免用例间互相污染。"""
    pa._LOCAL_UNIVERSE_CACHE.clear()
    pa._LOCAL_UNIVERSE_USABLE.clear()
    yield
    pa._LOCAL_UNIVERSE_CACHE.clear()
    pa._LOCAL_UNIVERSE_USABLE.clear()


# --------------------------------------------------------------------------- #
# 1. parse_holdings：正常格式
# --------------------------------------------------------------------------- #
def test_parse_comma_code_weight_cost():
    """``600519,30,1500`` → 代码,权重,成本价。"""
    positions, warnings = pa.parse_holdings("600519,30,1500")
    assert warnings == []
    assert len(positions) == 1
    p = positions[0]
    assert p["code"] == "600519"
    assert p["raw_code"] == "600519"
    assert p["market"] == "cn"
    assert p["weight"] == 30.0
    assert p["cost"] == 1500.0
    assert p["name"] is None


def test_parse_space_code_name_weight_pct():
    """``600519 贵州茅台 30%`` → 代码,名称,权重（带 %）。"""
    positions, warnings = pa.parse_holdings("600519 贵州茅台 30%")
    assert warnings == []
    assert len(positions) == 1
    p = positions[0]
    assert p["code"] == "600519"
    assert p["name"] == "贵州茅台"
    assert p["weight"] == 30.0
    assert p["cost"] is None


# --------------------------------------------------------------------------- #
# 2. parse_holdings：混合分隔 / 空行 / # 注释
# --------------------------------------------------------------------------- #
def test_parse_mixed_separators_and_comments():
    text = (
        "# 我的持仓组合\n"
        "600519,30,1500\n"
        "\n"
        "000858\t25\t180\n"
        "300750   20   220\n"
        "   \n"
    )
    positions, warnings = pa.parse_holdings(text)
    assert warnings == []
    assert [p["code"] for p in positions] == ["600519", "000858", "300750"]
    assert [p["weight"] for p in positions] == [30.0, 25.0, 20.0]
    assert [p["cost"] for p in positions] == [1500.0, 180.0, 220.0]


# --------------------------------------------------------------------------- #
# 3. parse_holdings：无法识别的行 → 跳过 + warning（不静默丢弃）
# --------------------------------------------------------------------------- #
def test_parse_unrecognizable_line_produces_warning():
    text = "贵州茅台 30%\n600519,30,1500"
    positions, warnings = pa.parse_holdings(text)
    # 第 1 行没有合法代码 → 跳过并告警；第 2 行正常解析
    assert [p["code"] for p in positions] == ["600519"]
    assert len(warnings) == 1
    assert "第 1 行" in warnings[0]
    assert "无法识别为证券代码" in warnings[0]


def test_parse_invalid_weight_is_skipped_with_warning():
    positions, warnings = pa.parse_holdings("600519,180,1500")
    assert positions == []
    assert len(warnings) == 1
    assert "权重" in warnings[0] and "非法" in warnings[0]


# --------------------------------------------------------------------------- #
# 4. 集中度：HHI / topN 数值正确性（固定输入断言精确值）
# --------------------------------------------------------------------------- #
def _pos(code, weight):
    return {"code": code, "raw_code": code, "name": None, "weight": weight,
            "cost": None, "market": "cn"}


def test_concentration_exact_values():
    positions = [
        _pos("600519", 30.0),
        _pos("000858", 25.0),
        _pos("300750", 20.0),
        _pos("600036", 15.0),
        _pos("000001", 10.0),
    ]
    conc = pa.compute_concentration(positions)
    assert conc["top1_pct"] == 30.0
    assert conc["top3_pct"] == 75.0
    assert conc["top5_pct"] == 100.0
    # 0.30^2 + 0.25^2 + 0.20^2 + 0.15^2 + 0.10^2 = 0.225
    assert conc["hhi"] == 0.225
    assert conc["hhi_points"] == 2250.0
    assert conc["weight_basis"] == "provided"
    assert conc["sum_input_pct"] == 100.0


def test_concentration_equal_weight_fallback():
    positions = [_pos(c, None) for c in ("600519", "000858", "300750", "600036")]
    warnings = []
    conc = pa.compute_concentration(positions, warnings)
    assert conc["weight_basis"] == "equal"
    assert conc["top1_pct"] == 25.0
    assert conc["top3_pct"] == 75.0
    # 4 * 0.25^2 = 0.25
    assert conc["hhi"] == 0.25
    assert any("等权" in w for w in warnings)


# --------------------------------------------------------------------------- #
# 5. execute：缺失/为空 holdings → 明确提示，且不含任何数值
# --------------------------------------------------------------------------- #
def _assert_no_digits(report: str):
    assert re.search(r"\d", report) is None, f"报告不应包含任何数值，实际: {report!r}"


def test_execute_missing_holdings_no_fabricated_numbers():
    agent = pa.PortfolioAnalystV2()
    result = agent.execute({"market_type": "cn"})
    assert "portfolio_report" in result
    report = result["portfolio_report"]
    assert "未提供持仓" in report
    _assert_no_digits(report)


def test_execute_empty_holdings_no_fabricated_numbers():
    agent = pa.PortfolioAnalystV2()
    result = agent.execute({"holdings": "   ", "market_type": "cn"})
    assert "portfolio_report" in result
    assert "未提供持仓" in result["portfolio_report"]
    _assert_no_digits(result["portfolio_report"])


# --------------------------------------------------------------------------- #
# 6. 市场支持范围：非 cn **逐只跳过**，不再整包降级（P1-2）
# --------------------------------------------------------------------------- #
def _patch_offline(monkeypatch, frontier=None):
    monkeypatch.setattr(pa, "_count_trading_days", lambda codes, lb: ({}, None))

    def _fake_ef(positions, lookback_days):
        if frontier is None:
            return None, ["可用 A 股标的不足 2 只，无法计算组合有效前沿。"], []
        return frontier, list(frontier.get("warnings") or []), frontier.get("_excluded", [])

    monkeypatch.setattr(pa, "_run_efficient_frontier", _fake_ef)


def test_non_cn_market_type_does_not_suppress_frontier(monkeypatch):
    """market_type 非 cn 不再触发整包降级：标的自身是 cn 就照常算有效前沿。"""
    _patch_offline(monkeypatch, frontier=_fake_success_frontier())
    positions = [_pos("600519", 50.0), _pos("000858", 50.0)]
    facts = pa.compute_portfolio_facts(positions, market_type="hk")
    assert facts["cn_only"] is False
    assert facts["efficient_frontier"] is not None
    assert not any("检测到非 A 股标的" in w for w in facts["warnings"])


def test_non_cn_position_flags_a_share_only(monkeypatch):
    """market_type=cn 但持仓里出现港股代码（700 → 00700）时逐只跳过并提示。"""
    _patch_offline(monkeypatch)
    positions = [_pos("600519", 50.0), _pos("00700", 50.0)]
    positions[1]["market"] = "hk"
    facts = pa.compute_portfolio_facts(positions, market_type="cn")
    assert facts["cn_only"] is True
    assert any("仅支持 A 股" in w for w in facts["warnings"])


# --------------------------------------------------------------------------- #
# 7. 有效前沿成功路径（打桩）：报告使用真实数值 + 列出被排除标的
# --------------------------------------------------------------------------- #
def _fake_success_frontier():
    return {
        "status": "success",
        "max_sharpe": {
            "return": -0.0554,
            "volatility": 0.2243,
            "sharpe_ratio": -0.359,
            "weights": {"600519": 0.6, "300750": 0.4},
        },
        "min_vol": {
            "return": -0.0612,
            "volatility": 0.2011,
            "weights": {"600519": 0.5, "300750": 0.5},
        },
        "portfolio_stats": {
            "num_assets": 2,
            "valid_symbols": ["600519", "300750"],
            "overlapping_trading_days": 240,
        },
        "warnings": ["000858 数据不足（需要至少30个交易日），已排除。"],
        "_excluded": [
            {"symbol": "000858", "reason": "000858 数据不足（需要至少30个交易日），已排除。"}
        ],
    }


def test_execute_success_path_uses_frontier_data(monkeypatch):
    _patch_offline(monkeypatch, frontier=_fake_success_frontier())
    agent = pa.PortfolioAnalystV2()
    result = agent.execute(
        {
            "holdings": "600519,30,1500\n000858,25,180\n300750,20,220",
            "market_type": "cn",
            "analysis_date": "2026-10-15",
        }
    )
    report = result["portfolio_report"]
    # 真实（打桩）数值必须体现在报告中
    assert "组合风险指标" in report
    assert "-5.54%" in report      # max_sharpe 年化收益
    assert "22.43%" in report      # max_sharpe 年化波动率
    assert "-0.359" in report      # 夏普
    assert "240 个" in report      # 重叠交易日
    # 被排除标的必须显式列出原因
    assert "000858" in report
    assert "数据不足" in report


def test_compute_facts_extracts_excluded_symbols(monkeypatch):
    _patch_offline(monkeypatch, frontier=_fake_success_frontier())
    positions = [_pos("600519", 40.0), _pos("000858", 30.0), _pos("300750", 30.0)]
    facts = pa.compute_portfolio_facts(positions, market_type="cn")
    assert facts["cn_only"] is False
    assert facts["data_quality"]["overlapping_trading_days"] == 240
    excluded = {e["symbol"] for e in facts["excluded_symbols"]}
    assert excluded == {"000858"}


# --------------------------------------------------------------------------- #
# 8. P1-1 幻影输入：纯字母弱形态不得造出美股幻影标的
# --------------------------------------------------------------------------- #
def test_phantom_english_words_produce_no_symbol_with_warning():
    """``not json at all``：任何候选的剩余 token 都没有数字支撑 → 全部跳过 + warning。"""
    positions, warnings = pa.parse_holdings("not json at all")
    assert positions == [], "不得凭空造出 NOT/JSON 之类幻影标的"
    assert warnings, "无法识别为证券代码时应给告警，不得静默"
    assert any("无法识别为证券代码" in w for w in warnings)


def test_strong_form_beats_weak_letter_prefix():
    """
    ``aa,600519,30``：aa 与 not/json 同属纯字母弱形态，强形态 600519 优先命中。

    真代码 600519 不再被 aa 抢占代码位（旧行为会把 600519 当权重而整行丢弃、归因失真）。
    """
    positions, warnings = pa.parse_holdings("aa,600519,30")
    codes = [p["code"] for p in positions]
    assert "AA" not in codes and "NOT" not in codes, "不得造出 aa → AA 幻影标的"
    assert codes == ["600519"]
    assert positions[0]["market"] == "cn"
    assert positions[0]["weight"] == 30.0


# --------------------------------------------------------------------------- #
# 9. P2-8：代码不在行首也要能解析（弱形态 + 数字支撑）
# --------------------------------------------------------------------------- #
def test_hk_code_not_at_line_start_is_parsed(monkeypatch):
    # 显式声明「00700 在本地港股库内」，使本用例不依赖本机数据预热状态。
    monkeypatch.setattr(pa, "_symbol_exists_locally", lambda m, s: (m, s) == ("hk", "00700"))
    positions, warnings = pa.parse_holdings("腾讯控股,00700,30")
    assert warnings == []
    assert [p["code"] for p in positions] == ["00700"]
    assert positions[0]["market"] == "hk"
    assert positions[0]["name"] == "腾讯控股"
    assert positions[0]["weight"] == 30.0


# --------------------------------------------------------------------------- #
# 10. 收紧后仍**保留**美股裸 ticker 支持
# --------------------------------------------------------------------------- #
def test_us_bare_ticker_with_weight_is_parsed(monkeypatch):
    monkeypatch.setattr(pa, "_symbol_exists_locally", lambda m, s: (m, s) == ("us", "AAPL"))
    positions, warnings = pa.parse_holdings("AAPL,30")
    assert warnings == []
    assert [p["code"] for p in positions] == ["AAPL"]
    assert positions[0]["market"] == "us"
    assert positions[0]["weight"] == 30.0


def test_us_bare_ticker_alone_is_skipped_with_warning():
    """``AAPL`` 单独一行是弱形态且无数字支撑 → 有意取舍：跳过 + 提示补权重，不猜。"""
    positions, warnings = pa.parse_holdings("AAPL")
    assert positions == []
    assert warnings


# --------------------------------------------------------------------------- #
# 10b. P1-1 后半句：弱形态候选必须「本地库查得到」
# --------------------------------------------------------------------------- #
def test_weak_form_with_numeric_support_but_absent_locally_is_skipped(monkeypatch):
    """
    ``aa,30`` / ``30, 25`` 都有数字支撑，但形态与「英文单词/权重数字」不可分。

    仅做「数字支撑」仍会造出幻影美股 ``AA`` / 幻影港股 ``00030``；
    库内查无此码 → 必须跳过 + warning，不得造出标的。
    """
    monkeypatch.setattr(pa, "_symbol_exists_locally", lambda m, s: False)
    for text in ("aa,30", "30, 25", "AB,40", "12,34"):
        positions, warnings = pa.parse_holdings(text)
        assert positions == [], f"{text!r} 不得造出幻影标的"
        assert any("无法识别为证券代码" in w for w in warnings), text


def test_weak_form_fails_open_when_universe_unavailable(monkeypatch):
    """
    DB 不可用 / 集合为空时 ``_symbol_exists_locally`` 返回 None → fail-open。

    这是有意的容错：数据库抖动不应把用户的合法输入误杀（宁可保留既有行为）。
    """
    monkeypatch.setattr(pa, "_symbol_exists_locally", lambda m, s: None)
    positions, warnings = pa.parse_holdings("AAPL,30")
    assert [p["code"] for p in positions] == ["AAPL"]


def test_strong_form_is_not_gated_by_local_universe(monkeypatch):
    """强形态（6 位数字 / 带点号 / 带连字符）不查库，避免误杀尚未预热的合法代码。"""
    calls = []
    monkeypatch.setattr(
        pa, "_symbol_exists_locally", lambda m, s: calls.append((m, s)) or False
    )
    positions, warnings = pa.parse_holdings("600519,30\n0700.HK,20\nBRK-B,10,900")
    codes = [p["code"] for p in positions]
    assert "600519" in codes, "6 位数字强形态不得被存在性校验拦住"
    assert "00700" in codes, "带点号强形态不得被存在性校验拦住"
    assert "BRK.B" in codes, "带连字符强形态不得被存在性校验拦住"
    assert calls == [], "强形态路径不应触发任何查库调用"


# --------------------------------------------------------------------------- #
# 11. P2-6：负权重被识别为权重并拒绝（不静默吸收成名称）
# --------------------------------------------------------------------------- #
def test_negative_weight_is_rejected_with_warning():
    positions, warnings = pa.parse_holdings("600519,-30")
    assert positions == []
    assert any("权重" in w and "非法" in w for w in warnings)


# --------------------------------------------------------------------------- #
# 12. P2-4：重复代码权重按 code 求和 + 「多行已合并」告警
# --------------------------------------------------------------------------- #
def test_duplicate_code_weights_merged_and_warned():
    positions, warnings = pa.parse_holdings("600519,30\n600519,20")
    assert [p["code"] for p in positions] == ["600519", "600519"]
    conc = pa.compute_concentration(positions, warnings)
    # 归一化后两行为 60% / 40%，按 code 合并 = 100%（而非后者覆盖前的 40%）
    assert conc["weights_used"] == {"600519": 100.0}
    assert any("多行" in w and "已合并" in w for w in warnings)


# --------------------------------------------------------------------------- #
# 13. P1-2 核心防线：一行垃圾不得让有效前沿整体消失
# --------------------------------------------------------------------------- #
def test_one_garbage_line_does_not_kill_frontier(monkeypatch):
    _patch_offline(monkeypatch, frontier=_fake_success_frontier())
    agent = pa.PortfolioAnalystV2()
    result = agent.execute(
        {
            "holdings": "600519,30,1500\n000858,25,180\nnot json at all",
            "market_type": "cn",
        }
    )
    report = result["portfolio_report"]
    assert "组合风险指标" in report
    assert "不可用：未能计算组合有效前沿" not in report
    assert "检测到非 A 股标的" not in report
    assert "-5.54%" in report  # 有效前沿仍在，真实（打桩）数值


def test_garbage_line_does_not_set_cn_only(monkeypatch):
    _patch_offline(monkeypatch, frontier=_fake_success_frontier())
    positions, _ = pa.parse_holdings("600519,30,1500\n000858,25,180\nnot json at all")
    facts = pa.compute_portfolio_facts(positions, market_type="cn")
    assert facts["cn_only"] is False
    assert facts["efficient_frontier"] is not None


# --------------------------------------------------------------------------- #
# 14. 非 cn 标的逐只跳过（不整包降级）+ 不做 A 股日线查询（P2-5）
# --------------------------------------------------------------------------- #
def test_non_cn_position_skipped_per_item_not_whole_package(monkeypatch):
    _patch_offline(monkeypatch, frontier=_fake_success_frontier())
    positions = [_pos("600519", 40.0), _pos("000858", 40.0), _pos("00700", 20.0)]
    positions[2]["market"] = "hk"
    facts = pa.compute_portfolio_facts(positions, market_type="cn")
    assert facts["cn_only"] is True
    # 每只非 cn 标的各自一条 warning，且不触发整包降级：有效前沿仍用 2 只 A 股算出
    assert any("00700" in w and "hk" in w for w in facts["warnings"])
    assert facts["efficient_frontier"] is not None
    # 报告里非 cn 标的标「未查询（市场不支持）」，绝不展示「00700：0 根」
    report = pa.render_portfolio_report(facts)
    assert "未查询（市场不支持）" in report
    assert "00700：0 根" not in report


def test_non_cn_symbol_not_queried_for_a_share_daily(monkeypatch):
    queried = []

    def _spy(codes, lookback):
        queried.extend(codes)
        return ({c: 240 for c in codes}, None)

    monkeypatch.setattr(pa, "_count_trading_days", _spy)
    monkeypatch.setattr(
        pa,
        "_run_efficient_frontier",
        lambda positions, lb: (
            None,
            ["可用 A 股标的不足 2 只，无法计算组合有效前沿。"],
            [],
        ),
    )
    positions = [_pos("600519", 50.0), _pos("00700", 50.0)]
    positions[1]["market"] = "hk"
    pa.compute_portfolio_facts(positions, market_type="cn")
    assert queried == ["600519"], "只对 cn 标的查本地 A 股日线，绝不查港股"


# --------------------------------------------------------------------------- #
# 15. 有效前沿失败的三条路径：各自「不可用 + 原因」，且不含编造数值
# --------------------------------------------------------------------------- #
class _ToolStub:
    def __init__(self, *, raises=None, returns=None):
        self._raises = raises
        self._returns = returns

    def invoke(self, _args):
        if self._raises is not None:
            raise self._raises
        return self._returns


def test_ef_path_fewer_than_two_symbols():
    frontier, warnings, excluded = pa._run_efficient_frontier([_pos("600519", 100.0)], 252)
    assert frontier is None
    assert any("不足 2 只" in w for w in warnings)


def test_ef_path_tool_raises(monkeypatch):
    import core.tools.implementations.portfolio.quant_tools as quant_tools

    monkeypatch.setattr(
        quant_tools, "get_efficient_frontier", _ToolStub(raises=RuntimeError("boom-xyz"))
    )
    frontier, warnings, excluded = pa._run_efficient_frontier(
        [_pos("600519", 50.0), _pos("000858", 50.0)], 252
    )
    assert frontier is None
    assert any("boom-xyz" in w for w in warnings)


def test_ef_path_non_json(monkeypatch):
    import core.tools.implementations.portfolio.quant_tools as quant_tools

    monkeypatch.setattr(
        quant_tools, "get_efficient_frontier", _ToolStub(returns="not json at all")
    )
    frontier, warnings, excluded = pa._run_efficient_frontier(
        [_pos("600519", 50.0), _pos("000858", 50.0)], 252
    )
    assert frontier is None
    assert any("解析" in w for w in warnings)


def test_ef_failure_report_has_no_fabricated_numbers(monkeypatch):
    import core.tools.implementations.portfolio.quant_tools as quant_tools

    monkeypatch.setattr(pa, "_count_trading_days", lambda codes, lb: ({}, None))
    monkeypatch.setattr(
        quant_tools, "get_efficient_frontier", _ToolStub(raises=RuntimeError("行情库不可用"))
    )
    agent = pa.PortfolioAnalystV2()
    report = agent.execute({"holdings": "600519,50\n000858,50", "market_type": "cn"})[
        "portfolio_report"
    ]
    assert "不可用" in report
    assert "行情库不可用" in report
    assert "年化收益" not in report  # 失败路径绝不展示编造的收益/波动率


# --------------------------------------------------------------------------- #
# 16. _count_trading_days 的**导入源防线**（避开 local_data 日期字典序丢数据 bug）
# --------------------------------------------------------------------------- #
def test_count_trading_days_imports_from_skill_runtime():
    import inspect

    src = inspect.getsource(pa._count_trading_days)
    assert "from core.skill_runtime.data_access import get_stock_daily_quotes" in src


# --------------------------------------------------------------------------- #
# 17. P3：多余数值 token 不静默忽略
# --------------------------------------------------------------------------- #
def test_extra_numeric_token_warns_not_silent():
    positions, warnings = pa.parse_holdings("600519,30,1500,1600")
    assert [p["code"] for p in positions] == ["600519"]
    assert positions[0]["weight"] == 30.0
    assert any("多余数值" in w and "1600" in w for w in warnings)


# --------------------------------------------------------------------------- #
# 18. P2-7：报告头部明示「数据窗口」
# --------------------------------------------------------------------------- #
def test_report_header_shows_data_window(monkeypatch):
    _patch_offline(monkeypatch, frontier=_fake_success_frontier())
    positions = [_pos("600519", 50.0), _pos("000858", 50.0)]
    facts = pa.compute_portfolio_facts(positions, market_type="cn")
    report = pa.render_portfolio_report(facts, analysis_date="2026-01-01")
    assert "数据窗口：截至" in report
    assert "分析日期：2026-01-01" in report


# --------------------------------------------------------------------------- #
# 19. P3：_extract_excluded 用词边界精确匹配，短代码不误配
# --------------------------------------------------------------------------- #
def test_extract_excluded_uses_precise_match():
    # 短代码 '30' 不应子串误配到 '300750' 的告警
    excluded = pa._extract_excluded(["30"], [], ["300750 数据不足，已排除。"])
    assert excluded[0]["reason"] == "数据不足或未参与组合计算（详见工具告警）"
    # 精确命中则取该告警
    excluded2 = pa._extract_excluded(["000858"], [], ["000858 数据不足，已排除。"])
    assert "000858 数据不足" in excluded2[0]["reason"]
