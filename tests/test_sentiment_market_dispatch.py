"""情绪工具「市场分派」与 stale import 冒烟测试。

背景（本测试锁定修复后的行为契约）：
通用研究工作流不带个股代码时，用户填的自由文本（如「A股市场当日盘面复盘」）会被
复用成 ``ticker``，``StockUtils.identify_stock_market`` 识别为 ``UNKNOWN``。
旧代码的 ``if is_china or is_hk: ... else: ...`` 让 UNKNOWN 落进「美股 Reddit」分支，
去 import 一个并不存在的 ``get_reddit_sentiment``，于是输出
``## 美股Reddit情绪 / 获取失败: cannot import name 'get_reddit_sentiment'``。

另：两处 stale import 都写在**函数体**内，所以单纯的「import 工具模块」冒烟测试在修复前
并**不会**失败（import 不执行函数体）。因此这里额外做一层**静态符号解析检查**：
扫描每个工具模块 AST 里的 ``from <模块> import a, b``，断言 a/b 真实存在于目标模块。

网络可能不可用，涉及真实数据源的断言一律宽容（只断言「不崩 + 文案正确」）。

离线约束（本轮新增）：所有用例**不得**真连 Mongo / AKShare / Reddit。
因此把三类外部依赖全部打桩：
  * ``app.core.database.get_mongo_db_sync`` → 内存替身（``fake_mongo``）
  * ``tradingagents.dataflows.providers.china.akshare`` → sys.modules 假模块
    （``akshare_tripwire`` 一访问就失败 / ``akshare_empty_stub`` 返回空表）
  * ``tradingagents.dataflows.interface`` → sys.modules 假模块，提供可配置 ``DATA_DIR``
    且 Reddit 取数函数一调用就失败（``reddit_env``）
"""

import ast
import importlib
import pkgutil
import sys
import types
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]

TOOLS_PKG = "core.tools.implementations"
AGENT_UTILS_FILE = PROJECT_ROOT / "tradingagents" / "agents" / "utils" / "agent_utils.py"

FREE_TEXT_TICKER = "A股市场当日盘面复盘"
CURR_DATE = "2026-10-14"

AKSHARE_MODULE = "tradingagents.dataflows.providers.china.akshare"
INTERFACE_MODULE = "tradingagents.dataflows.interface"
REDDIT_MODULE = "tradingagents.dataflows.news.reddit"


# --------------------------------------------------------------------------- #
# 外部依赖打桩（离线）
# --------------------------------------------------------------------------- #
class _RecordingCursor(list):
    """最小 Mongo 游标替身：支持 ``.sort(...).limit(...)`` 链式调用。"""

    def sort(self, *args, **kwargs):
        return self

    def limit(self, *args, **kwargs):
        return self


class _FakeCollection:
    def __init__(self, messages=None):
        self.messages = list(messages or [])
        self.queries = []  # 记录每次 find 的条件，供断言查询用的 symbol

    def find(self, query=None, *args, **kwargs):
        self.queries.append(dict(query or {}))
        return _RecordingCursor(self.messages)


class _FakeDB:
    def __init__(self, messages=None):
        self.social_media_messages = _FakeCollection(messages)


@pytest.fixture
def fake_mongo(monkeypatch):
    """把 ``get_mongo_db_sync`` 换成返回空结果的内存替身，避免真连 Mongo。"""
    import app.core.database as database

    db = _FakeDB()
    monkeypatch.setattr(database, "get_mongo_db_sync", lambda: db)
    return db


@pytest.fixture
def akshare_tripwire(monkeypatch):
    """在 sys.modules 注入假 akshare 模块：任何对 A 股备用源的访问都会失败。"""
    calls = []

    class _Provider:
        def __init__(self):
            calls.append("AKShareProvider")
            raise AssertionError("非 A 股路径不应访问 A 股备用源（AKShareProvider）")

    fake = types.ModuleType(AKSHARE_MODULE)
    fake.AKShareProvider = _Provider
    monkeypatch.setitem(sys.modules, AKSHARE_MODULE, fake)
    return calls


@pytest.fixture
def akshare_empty_stub(monkeypatch):
    """假 akshare 模块：返回空表，避免真实网络请求（仅供 A 股路径使用）。"""
    import pandas as pd

    class _Provider:
        def get_stock_news_sync(self, symbol=None, **kwargs):
            return pd.DataFrame()

    fake = types.ModuleType(AKSHARE_MODULE)
    fake.AKShareProvider = _Provider
    monkeypatch.setitem(sys.modules, AKSHARE_MODULE, fake)
    return fake


@pytest.fixture
def reddit_env(monkeypatch, tmp_path):
    """用假 interface 模块替换真实数据源模块。

    提供可配置的 ``DATA_DIR``（默认指向一个不存在的目录 = 本地缓存不可用），
    并把两个 Reddit 取数函数换成「记录了调用即失败」的哨兵。
    返回 ``(fake_interface, calls)``；断 ``calls == []`` 即证明链路没走 Reddit。
    """
    calls = []

    def _boom(*args, **kwargs):
        calls.append("reddit")
        raise AssertionError("本维度不应调用 Reddit 数据源")

    fake = types.ModuleType(INTERFACE_MODULE)
    fake.DATA_DIR = str(tmp_path / "data")  # 刻意不存在 → 本地缓存不可用
    fake.get_reddit_company_news = _boom
    fake.get_reddit_global_news = _boom
    monkeypatch.setitem(sys.modules, INTERFACE_MODULE, fake)
    return fake, calls


# --------------------------------------------------------------------------- #
# 工具调用辅助
# --------------------------------------------------------------------------- #
def _invoke_sentiment(ticker, curr_date=CURR_DATE, market_type=None):
    """调用统一情绪工具。

    注意：``@tool`` 装饰后返回的是 langchain ``StructuredTool``，**不可直接调用**
    （``StructuredTool object is not callable``），必须用 ``.invoke({...})``。
    """
    from core.tools.implementations.social.stock_sentiment import (
        get_stock_sentiment_unified,
    )

    payload = {"ticker": ticker, "curr_date": curr_date}
    if market_type is not None:
        payload["market_type"] = market_type
    return get_stock_sentiment_unified.invoke(payload)


# --------------------------------------------------------------------------- #
# 行为测试
# --------------------------------------------------------------------------- #
def test_unknown_free_text_falls_back_to_chinese_not_reddit(fake_mongo):
    """自由文本标的（未识别）走中文兜底，绝不落进美股 Reddit 分支。"""
    out = _invoke_sentiment(FREE_TEXT_TICKER)

    assert "cannot import name" not in out
    assert "Reddit" not in out
    # 兜底必须显式标注，不静默
    assert "兜底" in out
    assert "中文市场数据源" in out


def test_market_type_cn_forces_chinese_path(fake_mongo):
    """显式 market_type='cn' 时走中文路径。"""
    out = _invoke_sentiment(FREE_TEXT_TICKER, market_type="cn")

    assert "cannot import name" not in out
    assert "Reddit" not in out
    assert "中国A股" in out


def test_market_type_us_unrecognized_ticker_reports_unavailable(reddit_env):
    """market_type='us' + 非代码形态标的：明确说明 Reddit 不适用。

    注：旧断言 ``未配置有效凭证`` 随「凭证门控」一起被移除（Reddit 数据源读的是
    本地缓存目录，根本不使用凭证，凭证门控会误报不可用并误导用户去配凭证）。
    这里保留其**语义**：不崩 + 不可用/不适用须显式说明 + 不得静默成「情绪中性」。
    """
    _fake_iface, calls = reddit_env
    out = _invoke_sentiment(FREE_TEXT_TICKER, market_type="us")

    assert "cannot import name" not in out
    assert calls == [], "非代码形态标的不得触发 Reddit 取数"
    assert "非证券代码形态" in out
    # 「不适用/不可用」不能被静默成「中性」
    assert "不代表情绪中性" in out


def test_invalid_market_type_is_ignored_and_falls_back(fake_mongo, akshare_empty_stub):
    """非法 market_type 被忽略，回落到按 ticker 识别（600519 → 中国A股）。"""
    out = _invoke_sentiment("600519", market_type="xxx")

    assert "cannot import name" not in out
    assert "Reddit" not in out
    assert "中国A股" in out


def test_resolve_market_maps_digit_only_700_to_hk():
    """铁律：纯数字 ≤5 位 → 港股。"700"（腾讯最常见写法）必须得到 hk，而非未识别。

    （旧实现走 ``StockUtils.get_market_info``，其纯数字港股正则只认 ``\\d{4,5}``，
      "700" 会落 UNKNOWN，进而被舆情工具当成中文兜底并落到 A 股接口。）
    """
    from core.tools.implementations.social.stock_sentiment import _resolve_market

    market_key, market_name, is_china, is_hk, is_us = _resolve_market("", "700")
    assert market_key == "hk"
    assert market_name == "港股"
    assert (is_china, is_hk, is_us) == (False, True, False)

    # 显式 market_type 优先于形态识别（保留既有语义）
    assert _resolve_market("us", "700")[0] == "us"
    # 自由文本不是代码形态 → 未识别（""），绝不误判为 us
    assert _resolve_market("", FREE_TEXT_TICKER)[0] == ""
    assert _resolve_market("", "MARKET")[0] == ""


def test_hk_symbol_variants_query_same_internal_symbol(fake_mongo, akshare_tripwire):
    """"0700.HK" 与 "700" 查库时必须用同一个内部 symbol "00700"（港股 5 位）。

    （旧实现手工剥后缀，"0700.HK" → "0700"（4 位），与库内 5 位口径永远匹配不上。）
    """
    out_a = _invoke_sentiment("0700.HK")
    out_b = _invoke_sentiment("700")

    symbols = [q.get("symbol") for q in fake_mongo.social_media_messages.queries]
    assert symbols == ["00700", "00700"]

    for out in (out_a, out_b):
        assert "港股舆情" in out
        assert "不主动拉取 A 股新闻接口" in out
    assert akshare_tripwire == [], "港股路径不得访问 A 股备用源"


def test_hk_never_calls_a_share_backup(fake_mongo, akshare_tripwire):
    """港股本地无数据时，明确说明「暂无本地数据源」，且不触发 A 股备用源。"""
    out = _invoke_sentiment("700")

    assert akshare_tripwire == [], "港股路径不得调用 A 股备用源（akshare）"
    assert "不主动拉取 A 股新闻接口" in out
    # 「不可用」不能被静默成「中性」
    assert "不代表情绪中性" in out


def test_us_market_type_free_text_skips_reddit(reddit_env):
    """market_type='us' + 自由文本：不得拿中文文本去查 Reddit（否则 KeyError）。"""
    _fake_iface, calls = reddit_env
    out = _invoke_sentiment(FREE_TEXT_TICKER, market_type="us")

    assert calls == [], "自由文本不得触发 Reddit 取数"
    assert "Reddit 个股情绪不适用" in out


def test_reddit_cache_unavailable_is_reported_not_neutral(reddit_env):
    """本地 Reddit 缓存不可用时，明确输出「本维度不可用」，且不得静默成「中性」。"""
    _fake_iface, calls = reddit_env
    out = _invoke_sentiment("AAPL")

    assert calls == [], "缓存不可用时不得触发 Reddit 取数"
    assert "本维度不可用" in out
    assert "本地 Reddit 缓存" in out
    assert "不代表情绪中性" in out
    # 门控必须落到「本地缓存」而非「凭证」（凭证从未被该数据源使用）
    assert "不是凭证" in out


def test_us_reddit_receives_normalized_symbol(monkeypatch):
    """P2-1：美股 Reddit 取数必须传**归一化后的裸 ticker**，而非原始 ticker。

    数据层（``reddit.py`` 的 ``ticker_to_company``）只以裸 ticker 为键，形态闸门
    接受的 ``AAPL.O`` / ``BRK-B`` 若原样传入，会在 ``ticker_to_company[query]``
    抛 KeyError（表现为「个股新闻获取失败」）。
    """
    from core.tools.implementations.social import stock_sentiment as ss

    recorded = []

    def _fake_company_news(ticker, curr_date, days, limit):
        recorded.append(ticker)
        return f"OK:{ticker}"

    fake = types.ModuleType(INTERFACE_MODULE)
    fake.DATA_DIR = "/nonexistent"  # 本用例不经过缓存门控
    fake.get_reddit_company_news = _fake_company_news
    fake.get_reddit_global_news = lambda *a, **k: "global"
    monkeypatch.setitem(sys.modules, INTERFACE_MODULE, fake)
    monkeypatch.setattr(ss, "_reddit_cache_unavailable_reason", lambda: "")

    ss._build_us_sentiment("AAPL.O", CURR_DATE)
    ss._build_us_sentiment("BRK-B", CURR_DATE)

    assert recorded == ["AAPL", "BRK.B"]


def test_legacy_toolkit_sentiment_delegates_to_core(monkeypatch):
    """P2-2：legacy ``Toolkit.get_stock_sentiment_unified`` 必须委托给 core 实现。

    防止再次出现第二套「市场分派 + Reddit 门控 + 免责文案」与 core 漂移。
    """
    import core.tools.implementations.social.stock_sentiment as core_mod
    from tradingagents.agents.utils.agent_utils import Toolkit

    calls = []

    class _Stub:
        def invoke(self, payload):
            calls.append(payload)
            return "DELEGATED"

    monkeypatch.setattr(core_mod, "get_stock_sentiment_unified", _Stub())

    out = Toolkit.get_stock_sentiment_unified.invoke(
        {"ticker": "600519", "curr_date": CURR_DATE}
    )

    assert out == "DELEGATED"
    assert calls == [{"ticker": "600519", "curr_date": CURR_DATE}]


# --------------------------------------------------------------------------- #
# import 冒烟测试（根因治理：不再让 stale import 静默通过）
# --------------------------------------------------------------------------- #
def _discover_tool_files():
    return sorted(
        p
        for p in (PROJECT_ROOT / "core" / "tools" / "implementations").rglob("*.py")
        if p.name != "__init__.py"
    )


def _discover_tool_modules():
    pkg = importlib.import_module(TOOLS_PKG)
    errors = []
    modules = [
        m.name
        for m in pkgutil.walk_packages(
            pkg.__path__, prefix=TOOLS_PKG + ".", onerror=errors.append
        )
    ]
    return modules, errors


def test_tool_modules_import_cleanly():
    """core/tools/implementations 下所有模块必须能干净 import（动态发现，非硬编码）。"""
    modules, walk_errors = _discover_tool_modules()
    assert modules, "未发现任何工具模块，动态发现逻辑可能失效"

    failures = [f"{e}" for e in walk_errors]
    for name in modules:
        try:
            importlib.import_module(name)
        except Exception as e:  # noqa: BLE001 - 任何导入异常都算失败
            failures.append(f"{name}: {type(e).__name__}: {e}")

    assert not failures, "以下工具模块导入失败:\n" + "\n".join(failures)


def test_agent_utils_imports_cleanly():
    """顺带覆盖 tradingagents/agents/utils/agent_utils.py（它含同一处 stale import）。"""
    importlib.import_module("tradingagents.agents.utils.agent_utils")


# 历史遗留 stale import 白名单（保留机制，**当前为空**）。
# 曾经登记的 3 处历史欠账已全部清理：
#   1) technical_indicators.py 的 OptimizedUSProvider
#      → 符号已改名，改为真实存在的 OptimizedUSDataProvider（选 a，已实证可实例化 + 可调用）
#   2) market_snapshot.py 的 get_stock_data_unified
#      → 符号全仓从不存在且无可验证的等价替代，删除死 import 与死 K 线分支（选 b）
#   3) agent_utils.py 的 get_tushare_adapter
#      → 符号全仓从不存在且其返回值从未被使用，删除死 import 与死 try/except（选 b）
# ⚠️ 现在白名单为空：任何（含新增）stale import 都会让测试失败。
#    如未来确有需要临时登记的合理场景，可在此追加 (模块, 符号) 元组。
_KNOWN_PREEXISTING_STALE_IMPORTS: set[tuple[str, str]] = set()


def _iter_absolute_import_from(py_file: Path):
    """产出文件 AST 中所有「绝对路径」的 ``from X import ...`` 节点（含函数体内）。"""
    tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node


def test_no_stale_symbol_imports_in_tool_modules():
    """函数体内的 stale import 静态解析检查（这一步才真正能在修复前失败）。

    对每个工具模块（含 agent_utils.py）里的 ``from <模块> import a, b``：
    若 ``<模块>`` 可导入，则 a/b 必须真实存在于该模块，否则判为 stale import。
    """
    failures = []
    targets = _discover_tool_files() + [AGENT_UTILS_FILE]

    for py_file in targets:
        for node in _iter_absolute_import_from(py_file):
            try:
                target = importlib.import_module(node.module)
            except Exception:
                # 目标模块本身不可导入（可能缺可选依赖 / 需要网络），
                # 交由 test_tool_modules_import_cleanly 兜底，这里不误报。
                continue
            for alias in node.names:
                if alias.name == "*":
                    continue
                if (node.module, alias.name) in _KNOWN_PREEXISTING_STALE_IMPORTS:
                    continue  # 历史遗留，见白名单说明
                if not hasattr(target, alias.name):
                    failures.append(
                        f"{py_file.relative_to(PROJECT_ROOT)}: "
                        f"from {node.module} import {alias.name} —— 目标模块无此符号"
                    )

    assert not failures, "发现 stale import（引用了不存在的符号）:\n" + "\n".join(failures)
