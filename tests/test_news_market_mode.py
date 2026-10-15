"""大盘/全市场新闻兜底模式（未知市场）的确定性单元测试。

背景：通用研究工作流会把自由文本（如「A股市场当日盘面复盘」）当作 ticker 传入
``get_stock_news_headlines_unified``，此时 ``StockUtils.get_market_info`` 判定为未知
市场。修复前未知市场分支直接返回空 ``items``，一个数据源都不碰，导致「当前未获取到
可用新闻标题。」

本测试全部离线可跑：通过 monkeypatch ``akshare.news_cctv`` 打桩，不依赖网络。
"""

import asyncio
import inspect
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import akshare  # noqa: E402

from core.tools.implementations.news import stock_news  # noqa: E402
from app.core.market_codes import resolve_instrument  # noqa: E402

BUNDLE_KEYS = {
    "ticker",
    "market_name",
    "analysis_date",
    "start_date",
    "end_date",
    "company_name",
    "items",
    "footer_note",
}

MARKET_TEXT = "A股市场当日盘面复盘"
ANALYSIS_DATE = "2026-10-14"


def _make_news_cctv(rows_by_date):
    """构造 ak.news_cctv 打桩函数：按 date=YYYYMMDD 返回对应 DataFrame。"""

    def _fake_news_cctv(date=None, **kwargs):
        rows = rows_by_date.get(str(date))
        if rows is None:
            return pd.DataFrame(columns=["date", "title", "content"])
        return pd.DataFrame(rows)

    return _fake_news_cctv


def _make_news_cctv_raising(exc):
    def _fake_news_cctv(date=None, **kwargs):
        raise exc

    return _fake_news_cctv


# ---------------------------------------------------------------------------
# 1. 确定性单测：未知市场走「大盘/全市场」分支
# ---------------------------------------------------------------------------
def test_unknown_market_uses_market_branch(monkeypatch):
    rows_by_date = {
        "20261014": [
            {"date": "20261014", "title": "前三季度我国物价保持平稳运行", "content": "国家统计局数据显示物价总体平稳。"},
            {"date": "20261014", "title": "A股三大指数集体收涨 成交额放大", "content": "沪深两市成交额较前一交易日放大。"},
            # 窗口外（分析日往前 7 天窗口为 20261008~20261014），应被过滤掉
            {"date": "20260901", "title": "窗口外的历史新闻", "content": "不应出现在结果中。"},
        ],
        "20261013": [
            {"date": "20261013", "title": "央行开展逆回购操作 维护市场流动性", "content": "公开市场操作维稳资金面。"},
        ],
        "20261012": [],
    }
    monkeypatch.setattr(akshare, "news_cctv", _make_news_cctv(rows_by_date))

    bundle = stock_news._build_stock_news_bundle(MARKET_TEXT, ANALYSIS_DATE, limit=10)

    # 结构必须与其它 bundle 同构
    assert set(bundle.keys()) == BUNDLE_KEYS

    # 走了大盘分支：有内容、市场名不再是裸的「未知市场」、footer 表明是全市场新闻
    assert bundle["items"], "未知市场应走全市场新闻兜底，而不是返回空 items"
    assert bundle["market_name"] != "未知市场"
    assert "未知市场" in bundle["market_name"] and "全市场" in bundle["market_name"]
    assert "全市场" in bundle["footer_note"]
    assert "ak.news_cctv" in bundle["footer_note"]

    # news_id 必须生成（下游 _select_news_ids_for_details 依赖它）
    assert all(item.get("news_id") for item in bundle["items"])

    # 窗口过滤生效：窗口外条目不应出现
    titles = {item["title"] for item in bundle["items"]}
    assert "窗口外的历史新闻" not in titles
    for item in bundle["items"]:
        publish_time = stock_news._parse_publish_time(item["publish_time"])
        assert publish_time is not None
        assert datetime(2026, 10, 8, 0, 0, 0) <= publish_time <= datetime(2026, 10, 14, 23, 59, 59)

    assert bundle["start_date"] == "2026-10-08"
    assert bundle["end_date"] == "2026-10-14"


def test_market_bundle_items_sorted_by_time_desc(monkeypatch):
    rows_by_date = {
        "20261014": [
            {"date": "20261014", "title": "盘中新闻A", "content": "内容A"},
        ],
        "20261013": [
            {"date": "20261013", "title": "昨日新闻B", "content": "内容B"},
        ],
    }
    monkeypatch.setattr(akshare, "news_cctv", _make_news_cctv(rows_by_date))

    bundle = stock_news._build_stock_news_bundle(MARKET_TEXT, ANALYSIS_DATE, limit=10)
    times = [stock_news._parse_publish_time(i["publish_time"]) for i in bundle["items"]]
    assert times == sorted(times, reverse=True), "大盘模式应按时间倒序"


# ---------------------------------------------------------------------------
# 2. 反向对照：修复前「未知市场直接返回空 items」的短路路径已不存在
# ---------------------------------------------------------------------------
def test_unknown_market_short_circuit_removed(monkeypatch):
    """源码级断言 + 不打桩也不会短路成 0 条。

    选择「源码级断言」为主：功能级反向对照（不带网络时无法区分“查了但为空”与“没查”）
    在离线环境下不可靠，而源码级断言可以精确锁定旧短路分支（`"items": []` 早退）
    不再存在于 unknown-market 分支中，并防止后续被重新引入。
    """
    rows_by_date = {"20261014": [{"date": "20261014", "title": "样本标题", "content": "样本内容"}]}
    monkeypatch.setattr(akshare, "news_cctv", _make_news_cctv(rows_by_date))

    # (a) 功能级：未知市场 + 打桩数据源 -> 必须产出 items（即不再短路）
    bundle = stock_news._build_stock_news_bundle(MARKET_TEXT, ANALYSIS_DATE, limit=10)
    assert bundle["items"], "未知市场分支仍短路成空 items"

    # (b) 源码级：unknown-market 分支不得再直接返回空 items
    source = inspect.getsource(stock_news._build_stock_news_bundle)
    assert "_build_market_news_bundle" in source, "未知市场分支未接入大盘新闻兜底"
    assert '"items": []' not in source, "未知市场分支仍存在直接返回空 items 的短路实现"


# ---------------------------------------------------------------------------
# 3. 降级路径：数据源不可达 vs 数据源可达但无新闻，必须可分辨且不崩
# ---------------------------------------------------------------------------
def test_market_branch_unreachable_is_distinguishable(monkeypatch):
    monkeypatch.setattr(
        akshare,
        "news_cctv",
        _make_news_cctv_raising(ConnectionError("curl: (56) Connection closed abruptly")),
    )

    bundle = stock_news._build_stock_news_bundle(MARKET_TEXT, ANALYSIS_DATE, limit=10)

    assert set(bundle.keys()) == BUNDLE_KEYS
    assert bundle["items"] == []
    assert "数据源不可达" in bundle["footer_note"]
    assert "未知市场" in bundle["market_name"]  # 未识别状态本身仍可读

    _items, status = stock_news._fetch_akshare_market_news_live(
        start_date=datetime(2026, 10, 8),
        end_date=datetime(2026, 10, 14, 23, 59, 59),
        limit=10,
    )
    assert status["reachable"] is False
    assert status["error"]


def test_market_branch_reachable_but_empty_is_distinguishable(monkeypatch):
    monkeypatch.setattr(akshare, "news_cctv", _make_news_cctv({}))

    bundle = stock_news._build_stock_news_bundle(MARKET_TEXT, ANALYSIS_DATE, limit=10)

    assert bundle["items"] == []
    assert "可达" in bundle["footer_note"]
    assert "数据源不可达" not in bundle["footer_note"]


# ---------------------------------------------------------------------------
# 4. _fetch_akshare_news_live 支持 symbol=None（大盘模式入口）
# ---------------------------------------------------------------------------
def test_fetch_akshare_news_live_supports_none_symbol(monkeypatch):
    rows_by_date = {"20261014": [{"date": "20261014", "title": "大盘样本", "content": "内容"}]}
    monkeypatch.setattr(akshare, "news_cctv", _make_news_cctv(rows_by_date))

    items = stock_news._fetch_akshare_news_live(
        symbol=None,
        start_date=datetime(2026, 10, 8),
        end_date=datetime(2026, 10, 14, 23, 59, 59),
        limit=10,
    )
    assert items
    assert items[0]["title"] == "大盘样本"
    assert items[0]["data_source"] == "akshare"


def test_market_relevance_does_not_require_symbol(monkeypatch):
    """大盘排序不应依赖 symbol/company_name。"""
    rows_by_date = {
        "20261014": [
            {"date": "20261014", "title": "A股指数与板块资金流向", "content": "板块资金流入。"},
        ]
    }
    monkeypatch.setattr(akshare, "news_cctv", _make_news_cctv(rows_by_date))

    ranked = stock_news._rank_market_news_items(
        [{"title": "A股指数与板块资金流向", "importance": "medium"}], limit=5
    )
    assert ranked
    assert 0 <= ranked[0]["relevance_score"] <= 100


# ---------------------------------------------------------------------------
# 5. 收口 1：空结果时 footer_note 必须出现在「用户可见输出」里
# ---------------------------------------------------------------------------
def test_empty_result_renders_unreachable_footer(monkeypatch):
    """数据源不可达 -> items 为空 -> 输出必须能分辨「数据源挂了」而非「确实没新闻」。

    注意：``get_stock_news_headlines_unified`` 被 langchain 的 ``@tool`` 装饰成
    ``StructuredTool``，因此通过 ``.func`` 调用底层函数。
    """
    monkeypatch.setattr(
        akshare,
        "news_cctv",
        _make_news_cctv_raising(ConnectionError("curl: (56) Connection closed abruptly")),
    )

    out = stock_news.get_stock_news_headlines_unified.func(MARKET_TEXT, ANALYSIS_DATE, 10)

    assert "当前未获取到可用新闻标题。" in out
    assert "数据源不可达" in out
    assert "不代表" in out


def test_empty_result_appends_nothing_when_footer_empty(monkeypatch):
    """footer_note 为空时，空结果输出必须与改动前逐字一致（不追加任何内容）。"""
    empty_bundle = {
        "ticker": "600519",
        "market_name": "中国A股",
        "analysis_date": ANALYSIS_DATE,
        "start_date": "2026-10-08",
        "end_date": "2026-10-14",
        "company_name": "贵州茅台",
        "items": [],
        "footer_note": "",
    }
    monkeypatch.setattr(stock_news, "_build_stock_news_bundle", lambda *a, **k: empty_bundle)

    out = stock_news.get_stock_news_headlines_unified.func("600519", ANALYSIS_DATE, 10)

    expected = (
        "# 600519 新闻标题候选\n\n"
        "**股票类型**: 中国A股\n"
        f"**分析日期**: {ANALYSIS_DATE}\n"
        "**新闻时间范围**: 2026-10-08 至 2026-10-14\n\n"
        "## 新闻标题候选\n当前未获取到可用新闻标题。"
    )
    assert out == expected


# ---------------------------------------------------------------------------
# 6. 收口 2：provider 大盘分支——透传 date、不传 limit、不静默返回旧日期数据
# ---------------------------------------------------------------------------
class _RecordingNewsCctv:
    """记录每次调用参数的 ``ak.news_cctv`` 打桩。"""

    def __init__(self, df):
        self._df = df
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append({"args": args, "kwargs": dict(kwargs)})
        return self._df


def _make_provider(monkeypatch, fake_news_cctv):
    from tradingagents.dataflows.providers.china.akshare import AKShareProvider

    monkeypatch.setattr(akshare, "news_cctv", fake_news_cctv)
    provider = AKShareProvider()
    monkeypatch.setattr(provider, "is_available", lambda: True)
    return provider


def test_provider_market_branch_passes_date_and_not_limit(monkeypatch):
    df = pd.DataFrame([{"date": "20261014", "title": "大盘样本新闻", "content": "内容"}])
    fake = _RecordingNewsCctv(df)
    provider = _make_provider(monkeypatch, fake)

    # 同步版：必须传 date=YYYYMMDD，且绝不传 limit=
    out_df = provider.get_stock_news_sync(symbol=None, market_date="2026-10-14")
    assert fake.calls, "同步大盘分支未调用 ak.news_cctv"
    call = fake.calls[-1]
    assert call["kwargs"].get("date") == "20261014"
    assert "limit" not in call["kwargs"]
    assert "limit" not in call["args"]
    assert out_df is not None and not out_df.empty

    # 异步版：行为一致（修复前这里会抛 TypeError 并被静默吞掉）
    fake.calls.clear()
    out_list = asyncio.run(provider.get_stock_news(symbol=None, market_date="2026-10-14"))
    assert fake.calls, "异步大盘分支未调用 ak.news_cctv"
    call = fake.calls[-1]
    assert call["kwargs"].get("date") == "20261014"
    assert "limit" not in call["kwargs"]
    assert "limit" not in call["args"]
    assert out_list and out_list[0]["title"] == "大盘样本新闻"


def test_provider_market_branch_without_date_no_stale_data(monkeypatch):
    """无日期时不得回落到 ak.news_cctv 写死的 20240424，也不得静默返回旧闻。"""
    df = pd.DataFrame([{"date": "20240424", "title": "2024年旧闻", "content": "内容"}])
    fake = _RecordingNewsCctv(df)
    provider = _make_provider(monkeypatch, fake)

    assert provider.get_stock_news_sync(symbol=None) is None
    assert asyncio.run(provider.get_stock_news(symbol=None)) is None
    assert fake.calls == [], "无明确日期时不应调用 ak.news_cctv（其 date 默认写死为 20240424）"


# ---------------------------------------------------------------------------
# 7. 回归：个股路径输出不得包含大盘 footer 文案
# ---------------------------------------------------------------------------
def test_individual_stock_output_has_no_market_footer(monkeypatch):
    a_share_footer = "---\n*A股优先路径: 数据库缓存 → akshare*"
    item = {
        "news_id": "n1",
        "title": "贵州茅台发布三季报",
        "source": "akshare",
        "publish_time": "2026-10-14 10:30:00",
        "relevance_score": 88,
        "source_signal": "high",
        "detail_action": "建议拉取明细",
        "detail_reason": "标题含财务事件",
    }
    bundle = {
        "ticker": "600519",
        "market_name": "中国A股",
        "analysis_date": ANALYSIS_DATE,
        "start_date": "2026-10-08",
        "end_date": "2026-10-14",
        "company_name": "贵州茅台",
        "items": [item],
        "footer_note": a_share_footer,
    }
    monkeypatch.setattr(stock_news, "_build_stock_news_bundle", lambda *a, **k: bundle)

    out = stock_news.get_stock_news_headlines_unified.func("600519", ANALYSIS_DATE, 10)

    assert "全市场/大盘级新闻" not in out
    assert "数据源不可达" not in out
    assert "未匹配到可用的全市场新闻条目" not in out
    assert a_share_footer in out


# ---------------------------------------------------------------------------
# 8. P1-5：市场识别改走 resolve_instrument（铁律口径）
# ---------------------------------------------------------------------------
def _patch_bundle_builders(monkeypatch, calls):
    """把四个市场级 bundle 构造器替换为记录器，返回 (市场名, 归一化 symbol)。"""

    def recorder(name):
        def _fake(ticker, curr_date, limit=20):
            calls.append((name, ticker))
            return {
                "ticker": ticker,
                "market_name": name,
                "analysis_date": curr_date,
                "start_date": "2026-10-08",
                "end_date": "2026-10-14",
                "company_name": ticker,
                "items": [],
                "footer_note": "",
            }

        return _fake

    monkeypatch.setattr(stock_news, "_build_a_share_news_bundle", recorder("cn"))
    monkeypatch.setattr(stock_news, "_build_hk_news_bundle", recorder("hk"))
    monkeypatch.setattr(stock_news, "_build_us_news_bundle", recorder("us"))
    monkeypatch.setattr(stock_news, "_build_market_news_bundle", recorder("market"))


def test_low_digit_hk_code_uses_hk_chain_not_market_fallback(monkeypatch):
    """「700」（腾讯最常见写法）必须走港股个股链路，而不是大盘兜底。"""
    calls = []
    _patch_bundle_builders(monkeypatch, calls)

    bundle = stock_news._build_stock_news_bundle("700", ANALYSIS_DATE, limit=10)

    assert bundle["market_name"] == "hk", "700 应走港股个股链路"
    assert calls == [("hk", "00700")], "既不能走大盘兜底，也不能用未归一化的 700"


def test_hk_code_forms_yield_same_normalized_symbol(monkeypatch):
    """「0700.HK」与「700」必须得到同一个归一化 symbol「00700」。"""
    calls = []
    _patch_bundle_builders(monkeypatch, calls)

    stock_news._build_stock_news_bundle("0700.HK", ANALYSIS_DATE, limit=10)
    stock_news._build_stock_news_bundle("700", ANALYSIS_DATE, limit=10)

    assert [name for name, _ in calls] == ["hk", "hk"]
    assert [symbol for _, symbol in calls] == ["00700", "00700"]


def test_free_text_still_uses_market_fallback(monkeypatch):
    """回归保护（事实 A 的坑）：含拉丁字母 A 的中文自由文本必须走大盘兜底，
    绝不能被 ``guess_market`` 的「含字母 → us」规则判成美股。"""
    calls = []
    _patch_bundle_builders(monkeypatch, calls)

    # 直接锁定事实：resolve_instrument 对自由文本返回 ("", "")
    assert resolve_instrument(MARKET_TEXT) == ("", "")

    bundle = stock_news._build_stock_news_bundle(MARKET_TEXT, ANALYSIS_DATE, limit=10)

    assert calls == [("market", MARKET_TEXT)], "自由文本必须走大盘兜底而非美股分支"
    assert bundle["market_name"] == "market"


# ---------------------------------------------------------------------------
# 9. P3-4a：大盘窗口内每一天都必须被尝试请求（不得静默截断）
# ---------------------------------------------------------------------------
class _RecordingCctvDates:
    """记录每次 ``ak.news_cctv`` 调用日期的打桩，返回空 DataFrame。"""

    def __init__(self):
        self.dates = []

    def __call__(self, date=None, **kwargs):
        self.dates.append(str(date))
        return pd.DataFrame(columns=["date", "title", "content"])


def test_market_news_window_attempts_every_day(monkeypatch):
    recorder = _RecordingCctvDates()
    monkeypatch.setattr(akshare, "news_cctv", recorder)

    _items, status = stock_news._fetch_akshare_market_news_live(
        start_date=datetime(2026, 10, 8, 0, 0, 0),
        end_date=datetime(2026, 10, 14, 23, 59, 59),
        limit=10,
    )

    expected = [
        "20261014", "20261013", "20261012", "20261011",
        "20261010", "20261009", "20261008",
    ]
    assert status["fetched_dates"] == expected, "窗口内每一天都必须被请求"
    assert recorder.dates == expected
    # 请求数上限不得小于窗口天数（否则更早的日子永远取不到且不报错）
    assert stock_news.MARKET_NEWS_MAX_DATE_FETCHES >= stock_news.A_SHARE_NEWS_LOOKBACK_DAYS


# ---------------------------------------------------------------------------
# 10. P3-4b：非 DataFrame 返回值不得让 iterrows() 抛 AttributeError
# ---------------------------------------------------------------------------
class _TruthyNoIterrows:
    """truthy 且 empty 为假、但没有 ``.iterrows`` 的对象。"""

    empty = False


def test_market_news_non_dataframe_does_not_crash(monkeypatch):
    monkeypatch.setattr(akshare, "news_cctv", lambda date=None, **kwargs: _TruthyNoIterrows())

    items, status = stock_news._fetch_akshare_market_news_live(
        start_date=datetime(2026, 10, 8, 0, 0, 0),
        end_date=datetime(2026, 10, 14, 23, 59, 59),
        limit=10,
    )

    assert items == []
    assert status["reachable"] is True


# ---------------------------------------------------------------------------
# 11. P3-4c：大盘新闻不再自造 importance 字段（避免评分被统一虚高 +7）
# ---------------------------------------------------------------------------
def test_market_news_items_have_no_self_invented_importance(monkeypatch):
    rows_by_date = {
        "20261014": [{"date": "20261014", "title": "大盘样本新闻", "content": "内容"}]
    }
    monkeypatch.setattr(akshare, "news_cctv", _make_news_cctv(rows_by_date))

    items, _status = stock_news._fetch_akshare_market_news_live(
        start_date=datetime(2026, 10, 8, 0, 0, 0),
        end_date=datetime(2026, 10, 14, 23, 59, 59),
        limit=10,
    )

    assert items, "样本应产出至少一条大盘新闻"
    assert all("importance" not in item for item in items), "大盘新闻不应自造 importance"

    # 缺失 importance 时评分函数必须「不加分」而非异常：base 50 + 大盘语境/事件关键词
    baseline = stock_news._calculate_market_news_relevance({"title": "纯标题无关键词"})
    with_medium = stock_news._calculate_market_news_relevance(
        {"title": "纯标题无关键词", "importance": "medium"}
    )
    assert baseline == 50
    assert with_medium == 57  # 证明「硬编码 medium」会凭空 +7（本修复要消除的来源）
