"""
通用研究工作流的 market_type / symbol 解析契约单测。

覆盖 ``app.routers.workflows._resolve_market_and_symbol``（签名已去掉死参数
``workflow_type``，改为 ``(ticker, custom_params)``）：

- general 场景 ticker 是自由文本时，market_type 回落 ``cn`` 且 symbol 必须为 ``None``；
- 真正识别到代码时才写 symbol，且 symbol 必须是**归一化后**的内部 symbol；
- ``resolve_instrument`` 能识别历史误判用例（``"700"`` 曾被 ``StockUtils`` 判为
  UNKNOWN 而丢 symbol）；
- 显式 ``custom_params.market_type`` 与识别结果冲突时（P2-2）以识别结果为准；
- 非法 market_type 被忽略并回落。

跑法（必须在清空环境后执行，否则 import tradingagents 会触发 config.mkdir 的
PermissionError）::

    env -i HOME="$HOME" PATH="$PATH" LANG=zh_CN.UTF-8 \
        .venv/bin/python -m pytest tests/test_market_symbol_resolution.py -v
"""

from app.routers.workflows import _resolve_market_and_symbol


# --------------------------------------------------------------------------- #
# 核心用例：自由文本（本次修复的根因）
# --------------------------------------------------------------------------- #
def test_free_text_without_market_type_defaults_to_cn_none():
    """中文自由文本 + 无 market_type → ("cn", None)。"""
    assert _resolve_market_and_symbol("A股市场当日盘面复盘", None) == ("cn", None)
    assert _resolve_market_and_symbol("A股市场当日盘面复盘", {}) == ("cn", None)


def test_other_free_text_forms_never_become_symbol():
    """其他自由文本形态（含拉丁字母的「A股…」绝不能因含字母被判成美股代码）。"""
    assert _resolve_market_and_symbol("以下持仓组合的健康度体检", None) == ("cn", None)
    assert _resolve_market_and_symbol("MARKET", None) == ("cn", None)
    assert _resolve_market_and_symbol("__GENERAL__", None) == ("cn", None)


# --------------------------------------------------------------------------- #
# 真实代码识别 + 归一化（业务查库按内部 symbol）
# --------------------------------------------------------------------------- #
def test_a_share_code():
    assert _resolve_market_and_symbol("600519", None) == ("cn", "600519")


def test_a_share_code_with_exchange_suffix_is_normalized():
    """带沪深后缀必须落库为 6 位纯数字内部 symbol。"""
    assert _resolve_market_and_symbol("600519.SS", None) == ("cn", "600519")
    assert _resolve_market_and_symbol("000001.SZ", None) == ("cn", "000001")


def test_hong_kong_code_is_normalized():
    """Wind/Yahoo 4 位 + .HK → 内部 5 位 symbol（历史落库成 '0700.HK' 会查库 miss）。"""
    assert _resolve_market_and_symbol("0700.HK", None) == ("hk", "00700")


def test_bare_hk_digits_are_recognized():
    """纯 1-5 位数字 → 港股；'700' 曾被 StockUtils 判成 UNKNOWN 而丢掉 symbol。"""
    assert _resolve_market_and_symbol("700", None) == ("hk", "00700")
    assert _resolve_market_and_symbol("00700", None) == ("hk", "00700")


def test_us_code_is_normalized():
    """美股必须大写、剥交易所后缀、股份类别统一成点号。"""
    assert _resolve_market_and_symbol("AAPL", None) == ("us", "AAPL")
    assert _resolve_market_and_symbol("aapl", None) == ("us", "AAPL")
    assert _resolve_market_and_symbol("AAPL.O", None) == ("us", "AAPL")
    assert _resolve_market_and_symbol("BRK-B", None) == ("us", "BRK.B")


# --------------------------------------------------------------------------- #
# 显式 market_type
# --------------------------------------------------------------------------- #
def test_free_text_with_explicit_market_type_returns_symbol_none():
    """自由文本 + market_type="hk" → ("hk", None)：显式市场优先，但不写 symbol。"""
    assert _resolve_market_and_symbol("A股市场当日盘面复盘", {"market_type": "hk"}) == ("hk", None)


def test_free_text_with_explicit_us_market_returns_symbol_none():
    """自由文本 + market_type="us" → ("us", None)：尊重显式市场，仍不写 symbol。"""
    assert _resolve_market_and_symbol("A股市场当日盘面复盘", {"market_type": "us"}) == ("us", None)


def test_conflict_between_explicit_market_and_recognized_code_prefers_recognized():
    """P2-2：显式 market_type 与代码识别结果冲突时，以**识别结果**为准（防跨市场落库）。"""
    assert _resolve_market_and_symbol("600519", {"market_type": "us"}) == ("cn", "600519")
    assert _resolve_market_and_symbol("0700.HK", {"market_type": "cn"}) == ("hk", "00700")


def test_invalid_market_type_ignored_and_falls_back():
    """非法 market_type → 忽略并回落（自由文本 → cn/None）。"""
    assert _resolve_market_and_symbol("A股市场当日盘面复盘", {"market_type": "xxx"}) == ("cn", None)


# --------------------------------------------------------------------------- #
# 别名 / 大小写
# --------------------------------------------------------------------------- #
def test_market_type_aliases_and_case_insensitive():
    assert _resolve_market_and_symbol(None, {"market_type": "A_SHARE"}) == ("cn", None)
    assert _resolve_market_and_symbol(None, {"market_type": "CN"}) == ("cn", None)
    assert _resolve_market_and_symbol(None, {"market_type": "china"}) == ("cn", None)
    assert _resolve_market_and_symbol(None, {"market_type": "HONGKONG"}) == ("hk", None)
    assert _resolve_market_and_symbol(None, {"market_type": "USA"}) == ("us", None)


# --------------------------------------------------------------------------- #
# 空 ticker
# --------------------------------------------------------------------------- #
def test_empty_ticker_without_market_type_defaults_to_cn():
    assert _resolve_market_and_symbol(None, None) == ("cn", None)
    assert _resolve_market_and_symbol("", {}) == ("cn", None)
