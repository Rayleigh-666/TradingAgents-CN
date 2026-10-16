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

迁移时还顺带查实一个**既存死分支**：``except OSError`` 那段在真实文件系统下不可达，
因为 ``os.walk`` 默认 ``onerror=None`` 会静默吞掉 ``PermissionError`` —— 于是「目录
不可读」被报成「缓存为空」。已改为收集 ``onerror`` 错误，并由 §2b 的用例锁定。
"""

from __future__ import annotations

import os
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
REASON_NOT_A_DIR = "本地 Reddit 缓存路径不是目录"
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


# ==================== 2b. 路径异常 / 读取失败（不得报成「缓存为空」） ====================


def test_reason_when_path_exists_but_is_not_a_dir(tmp_path):
    """``reddit_data`` 存在但是个文件 → 必须与「目录不存在」区分开。

    两种情况的处置完全不同：目录不存在 = 没预热过；路径被文件占了 = 配置/环境写错。
    共用一句「目录不存在」会把后者引向错误的排查方向。
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "reddit_data").write_text("not a dir\n", encoding="utf-8")

    reason = ss._reddit_cache_unavailable_reason(str(data_dir))

    assert reason == REASON_NOT_A_DIR
    assert reason != REASON_NO_DIR


def test_unreadable_cache_dir_is_not_reported_as_empty(tmp_path):
    """目录不可读时必须报「读取失败」，**绝不**报「缓存为空」。

    ``os.walk`` 默认 ``onerror=None`` 会静默吞掉 ``PermissionError``（实测返回 0 个
    条目、不抛异常），于是「读不到」被说成「没有数据」——正是本门控最不该犯的错。
    本用例以 chmod 000 强制该情形，锁定修复后的行为。
    """
    if os.geteuid() == 0:  # root 绕过权限位，chmod 000 不构成障碍
        pytest.skip("以 root 运行，无法用权限位构造不可读目录")

    data_dir = tmp_path / "data"
    cache_dir = _make_cache(data_dir).parent  # data_dir/reddit_data
    os.chmod(cache_dir, 0o000)
    try:
        reason = ss._reddit_cache_unavailable_reason(str(data_dir))
    finally:
        os.chmod(cache_dir, 0o755)  # 让 pytest 能清理 tmp_path

    assert reason.startswith("读取本地 Reddit 缓存目录失败"), reason
    assert reason != REASON_EMPTY


def test_available_even_if_one_category_dir_is_unreadable(tmp_path):
    """单个分类目录坏掉不该拖垮整个维度：别处有 .jsonl 就仍判可用。"""
    if os.geteuid() == 0:
        pytest.skip("以 root 运行，无法用权限位构造不可读目录")

    data_dir = tmp_path / "data"
    cache_dir = _make_cache(data_dir, relpath="global_news")  # 可读，含 .jsonl
    locked = cache_dir.parent / "locked"
    locked.mkdir()
    os.chmod(locked, 0o000)
    try:
        assert ss._reddit_cache_unavailable_reason(str(data_dir)) == ""
    finally:
        os.chmod(locked, 0o755)


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


def test_only_one_module_emits_reddit_cache_verdicts():
    """产出「Reddit 缓存可用性判据」的模块只能有一个。

    判据口径 = 文件里**同时**出现 ``reddit_data`` 与门控的原因文案
    （``本地 Reddit 缓存``）。只扫 ``os.path.join(..., "reddit_data")`` 会把纯数据源
    读者也算进来——``interface.py`` 只是拼路径、不产出任何判据——那样守卫会因无关改动
    误报。扫描范围含 ``cli/`` ``scripts/`` ``migrations/`` ``install/``：初版只扫
    ``core/tradingagents/app``，漏掉了这四个目录。

    ⚠️ 局限（须知）：这是**廉价绊线，不是证明**。改用 ``Path(DATA_DIR) / "reddit_data"``、
    或另起一套原因文案的重复实现不会被捕获——那类漂移仍靠
    ``test_legacy_duplicate_gate_is_gone`` 这类具名断言兜。
    """
    allowed = {
        PROJECT_ROOT / "core" / "tools" / "implementations" / "social" / "stock_sentiment.py",
    }
    found: set[Path] = set()
    for root_name in ("core", "tradingagents", "app", "cli", "scripts", "migrations", "install"):
        root = PROJECT_ROOT / root_name
        if not root.is_dir():
            continue
        for path in root.rglob("*.py"):
            text = path.read_text(encoding="utf-8", errors="ignore")
            if "reddit_data" in text and "本地 Reddit 缓存" in text:
                found.add(path)

    assert found == allowed, (
        "Reddit 缓存判据的来源发生变化："
        f"多出 {sorted(str(p) for p in found - allowed)}，"
        f"缺少 {sorted(str(p) for p in allowed - found)}"
    )


def test_agent_utils_has_no_leftover_reddit_gate_reference():
    """``agent_utils`` 里不留门控的残留引用（含注释掉的旧调用点）。"""
    text = AGENT_UTILS_FILE.read_text(encoding="utf-8")

    assert "check_local_reddit_cache" not in text
    assert "reddit_data" not in text
