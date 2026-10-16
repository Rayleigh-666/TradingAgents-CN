"""美股情绪维度的本地缓存门控 —— ``stock_sentiment._reddit_cache_unavailable_reason``。

数据源真相：Reddit 情绪走的 ``interface.get_reddit_company_news`` /
``get_reddit_global_news`` 最终调用 ``dataflows/news/reddit.py::fetch_top_from_category``，
**只读本地缓存目录** ``<DATA_DIR>/reddit_data/<category>/*.jsonl``：不发网络请求，
也**不读** ``REDDIT_CLIENT_ID`` / ``REDDIT_CLIENT_SECRET``。所以唯一的可用性门槛
只能是「本地缓存里有没有 ``.jsonl``」，按凭证判是错的。

本文件的存在理由（死代码清剿）：历史上存在**第二份**同名实现
``tradingagents.agents.utils.agent_utils.check_local_reddit_cache``，它**没有任何生产
调用方**，却独占了四条分支测试；而真正在跑的 ``_reddit_cache_unavailable_reason``
当时只有一个「目录不存在」分支被间接覆盖（``test_sentiment_market_dispatch.py``）。
现删副本，把四条分支 + 可用分支全部落到生产实现上，并加「唯一真源」守卫。
"""

from __future__ import annotations

import re
import sys
import types
from pathlib import Path

import pytest

from core.tools.implementations.social import stock_sentiment as ss

PROJECT_ROOT = Path(__file__).resolve().parents[3]
INTERFACE_MODULE = "tradingagents.dataflows.interface"
AGENT_UTILS_FILE = (
    PROJECT_ROOT / "tradingagents" / "agents" / "utils" / "agent_utils.py"
)

# 生产实现的判据文案（会出现在报告正文里，故不含文件系统绝对路径）
REASON_NO_DIR = "本地 Reddit 缓存目录不存在"
REASON_EMPTY = "本地 Reddit 缓存为空（无 .jsonl 文件）"
REASON_NO_DATA_DIR = "数据目录未配置"


def _make_cache(data_dir: Path, *, relpath: str = "global_news", name: str = "posts.jsonl") -> Path:
    """在 ``data_dir/reddit_data/<relpath>/`` 下放一个 ``.jsonl`` 文件。"""
    category_dir = data_dir / "reddit_data" / relpath
    category_dir.mkdir(parents=True, exist_ok=True)
    (category_dir / name).write_text("{}\n", encoding="utf-8")
    return category_dir


# ==================== 1. 四个「不可用」分支 ====================


def test_reason_when_cache_dir_missing(tmp_path):
    """缓存目录不存在 → 返回原因（供下游输出显式降级说明）。"""
    assert ss._reddit_cache_unavailable_reason(str(tmp_path / "nope")) == REASON_NO_DIR


def test_reason_when_dir_exists_but_no_jsonl(tmp_path):
    """目录存在但没有 .jsonl → 仍判不可用（空目录不算可用）。"""
    data_dir = tmp_path / "data"
    (data_dir / "reddit_data" / "global_news").mkdir(parents=True)
    assert ss._reddit_cache_unavailable_reason(str(data_dir)) == REASON_EMPTY


def test_reason_when_jsonl_is_not_in_category_dir(tmp_path):
    """``.jsonl`` 必须落在 ``reddit_data`` 之下 —— 放在别处不算缓存。"""
    data_dir = tmp_path / "data"
    (data_dir / "reddit_data" / "global_news").mkdir(parents=True)
    (data_dir / "posts.jsonl").write_text("{}\n", encoding="utf-8")
    assert ss._reddit_cache_unavailable_reason(str(data_dir)) == REASON_EMPTY


def test_reason_when_data_dir_is_empty_string():
    """显式注入空串（等价于「配置没给目录」）→ 不可用，且原因可读。"""
    assert ss._reddit_cache_unavailable_reason("") == REASON_NO_DATA_DIR


# ==================== 2. 可用分支 ====================


def test_available_when_jsonl_present(tmp_path):
    """存在 .jsonl → 可用（返回空串）。"""
    data_dir = tmp_path / "data"
    _make_cache(data_dir)
    assert ss._reddit_cache_unavailable_reason(str(data_dir)) == ""


def test_available_when_jsonl_in_nested_subdir(tmp_path):
    """``os.walk`` 需递归 —— 缓存按 ``reddit_data/<category>/<sub>`` 嵌套存放。"""
    data_dir = tmp_path / "data"
    _make_cache(data_dir, relpath="company_news/2026-10", name="AAPL.jsonl")
    assert ss._reddit_cache_unavailable_reason(str(data_dir)) == ""


# ==================== 3. 门槛不是凭据 ====================


@pytest.mark.parametrize("has_creds", [True, False])
def test_gate_ignores_reddit_credentials(tmp_path, monkeypatch, has_creds):
    """可用性只取决于本地缓存；``REDDIT_*`` 凭证配与不配都不影响结论。

    历史实现按凭据判断是**错的门槛**：有凭证但缓存为空时照样空转，缓存存在却没配
    凭证时反被判「不可用」。两种组合都必须与「缓存是否存在」保持一致。
    """
    if has_creds:
        monkeypatch.setenv("REDDIT_CLIENT_ID", "fake-id")
        monkeypatch.setenv("REDDIT_CLIENT_SECRET", "fake-secret")
    else:
        monkeypatch.delenv("REDDIT_CLIENT_ID", raising=False)
        monkeypatch.delenv("REDDIT_CLIENT_SECRET", raising=False)

    # 缓存为空：无论有没有凭证，都不可用
    empty_dir = tmp_path / "empty"
    (empty_dir / "reddit_data").mkdir(parents=True)
    assert ss._reddit_cache_unavailable_reason(str(empty_dir)) == REASON_EMPTY

    # 缓存存在：无论有没有凭证，都可用
    full_dir = tmp_path / "full"
    _make_cache(full_dir)
    assert ss._reddit_cache_unavailable_reason(str(full_dir)) == ""


# ==================== 4. 与数据源路径同源（data_dir=None 分支） ====================


def test_none_reads_interface_data_dir(tmp_path, monkeypatch):
    """``data_dir=None`` → 读 ``interface.DATA_DIR``（而非另取一次配置）。

    这条锁定「门控与数据源用同一个目录常量」：数据源在 interface.py 里读
    ``os.path.join(DATA_DIR, "reddit_data")``，门控必须复用同一个 ``DATA_DIR``。
    以假模块替换 ``sys.modules`` 条目，全程离线（不 import 真实数据源）。
    """
    fake = types.ModuleType(INTERFACE_MODULE)
    data_dir = tmp_path / "data"
    _make_cache(data_dir)
    fake.DATA_DIR = str(data_dir)
    monkeypatch.setitem(sys.modules, INTERFACE_MODULE, fake)

    assert ss._reddit_cache_unavailable_reason() == ""

    # 目录常量一改，门控结论必须随之改变 —— 证明它真的读的是这个常量
    fake.DATA_DIR = str(tmp_path / "moved")
    assert ss._reddit_cache_unavailable_reason() == REASON_NO_DIR


def test_reason_when_data_dir_cannot_be_resolved(monkeypatch):
    """取 ``interface.DATA_DIR`` 失败 → 按不可用处理，并把原因吐出来（不抛异常）。"""
    monkeypatch.setitem(sys.modules, INTERFACE_MODULE, types.ModuleType(INTERFACE_MODULE))

    reason = ss._reddit_cache_unavailable_reason()

    assert reason.startswith("无法解析 Reddit 缓存目录")
    # 必须带原因，否则下游无法输出「本维度不可用」的降级说明
    assert reason != ""


# ==================== 5. 唯一真源守卫（防再次漂移） ====================


def test_legacy_duplicate_gate_is_gone():
    """``agent_utils`` 不得再出现第二份门控实现。

    副本的代价不是「多几行」，而是**测试覆盖落在不跑的那一份上**：本轮修复前，四条
    分支测试全在无人调用的 ``check_local_reddit_cache`` 上，生产实现只有一条分支被
    间接覆盖。若确有必要在别处读取 Reddit 缓存，应调用 ``stock_sentiment`` 的门控
    而不是重写一份。
    """
    import tradingagents.agents.utils.agent_utils as agent_utils

    assert not hasattr(agent_utils, "check_local_reddit_cache")


def test_reddit_data_path_is_read_only_where_expected():
    """生产代码里读取 ``reddit_data`` 的位置仅限「数据源」与「门控」两处。

    静态扫描 ``os.path.join(..., "reddit_data")``：允许出现于
    ``dataflows/interface.py``（数据源）与 ``stock_sentiment.py``（门控）——
    若第三个文件冒出来，先确认它不是又一份重复实现。
    """
    allowed = {
        PROJECT_ROOT / "tradingagents" / "dataflows" / "interface.py",
        PROJECT_ROOT / "core" / "tools" / "implementations" / "social" / "stock_sentiment.py",
    }
    pattern = re.compile(r'os\.path\.join\([^)\n]*["\']reddit_data["\']')
    found: set[Path] = set()
    for root_name in ("core", "tradingagents", "app"):
        for path in (PROJECT_ROOT / root_name).rglob("*.py"):
            text = path.read_text(encoding="utf-8", errors="ignore")
            if pattern.search(text):
                found.add(path)

    assert found == allowed, (
        "reddit_data 路径读取点发生变化："
        f"多出 {sorted(str(p) for p in found - allowed)}，"
        f"缺少 {sorted(str(p) for p in allowed - found)}"
    )


def test_agent_utils_has_no_leftover_reddit_gate_reference():
    """``agent_utils`` 里不留门控的残留引用（含注释掉的旧调用点）。"""
    text = AGENT_UTILS_FILE.read_text(encoding="utf-8")

    assert "check_local_reddit_cache" not in text
    assert "reddit_data" not in text
