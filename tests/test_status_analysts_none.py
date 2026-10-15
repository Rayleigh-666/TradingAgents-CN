#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
「运行中任务状态接口 500」回归测试。

回归背景
--------
通用研究工作流（general）前端不传 `selected_analysts`，生产端
`app/routers/workflows.py:323` 曾把它写成
`task_params["selected_analysts"] = ... if ... else None`
（key 存在、值为 None）。任务「运行中」时，状态接口
`GET /api/analysis/tasks/{task_id}/status` 走 `analysis.py` 的
`else: # 任务进行中` 分支，用 `task_params.get("selected_analysts", [])` 取值
——因 key 存在，`.get(key, default)` 返回的是 `None` 而非默认值 `[]`，随后
把 `analysts=None` 灌进 `RedisProgressTracker`，到
`tracker.py::_get_base_total_time()` 里 `len(self.analysts)` 抛
`TypeError: object of type 'NoneType' has no len()`，冒泡成 HTTP 500，
前端每 3 秒轮询一次就弹一次红色错误横幅（其实分析一直在正常跑）。

为什么「已完成」复现不出来
------------------------
只有 `end_time` 为空（任务进行中）才进 `else` 分支构造 tracker；
任务完成后走 `if end_time:` 分支，根本不构造 tracker，故不崩。
本用例**不经过任务生命周期**，直接把 None 灌进崩溃点，因此不受此限制。

修复三层（A/B/C/D）
------------------
  A. tracker.__init__ 归一化 `self.analysts = list(analysts or [])`（共享工具自身免疫）
     → test_tracker_tolerates_none_analysts、test_len_none_is_the_actual_crash
  B. analysis.py:655 改 `task_params.get("selected_analysts") or []`
  C. workflows.py:323 改「有值才写」
  D. analysis.py:999 / task_analysis_service.py:1539 改 `or []`
     → B/C/D 由源码守卫覆盖：test_no_default_arg_get_trap_for_selected_analysts、
       test_workflows_does_not_write_none_selected_analysts

同源隐患补漏（同一「None 被当成缺失」陷阱的三个旁路 E/F/G）
--------------------------------------------------------
  E. workflows.py 合并 custom_params 时丢弃 None **与四个保留键**
     （否则 `selected_analysts: null` 会经 custom_params 灌回 task_params 绕过第 C 层
      守卫；`market_type` / `symbol` / `stock_code` / `task_category` 保留键回盖还会
      架空参数契约）
     → test_custom_params_merge_drops_none_mechanism（**真调用**生产函数
       `_sanitize_custom_params`）
       test_workflows_custom_params_merge_guard_source（源码守卫）
  F. unified_analysis_engine.py 的三处 `setdefault` 改为显式 None 判断
     （`setdefault` 在「key 存在但值为 None」时同样不生效，与第 B/C 层同源）
     → test_engine_defaults_replace_none_mechanism（机制：显式判空能替换 None）
       test_engine_defaults_keep_legit_zero（合法零值不误替换）
       test_setdefault_does_not_override_none（机制根因：为何不能用 setdefault）
       test_engine_no_setdefault_source_guard（源码守卫）
  G. core/workflow/builder.py 读取 `participants` 改 `(… or [])`
     （DEBATE 节点 config 可能被写成 `{"participants": null}`，下游 len/iter 崩溃；
     对齐 core/workflow/engine.py:337 的既有正确写法）
     → test_identify_debate_configs_tolerates_none_participants（机制：`or []` 兜住 None）
       test_builder_no_default_arg_get_trap_for_participants（源码守卫）

选择：**扩展本文件**而非新建。
理由：E/F/G 与 A–D 是**同一 bug 家族**（把 None 值误当「参数缺失」处理），
放在同一文件里便于日后一次性定位与防回流；源码守卫的 TRAP_FILES 也集中在
一处维护，避免测试碎片化。
"""

import os
import sys

import pytest

# 支持 `python tests/test_status_analysts_none.py` 直接运行（pytest 走 conftest.py）
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from app.services.progress.tracker import RedisProgressTracker  # noqa: E402


# ==================== 1. 直接打崩溃点 ====================

def test_tracker_tolerates_none_analysts():
    """崩溃点：tracker.py::_get_base_total_time() -> len(self.analysts)。

    修复前：analysts=None 被直接写入 self.analysts，本调用抛
            TypeError: object of type 'NoneType' has no len()。
    修复后：__init__ 归一化为 []，正常返回 float。

    注意：这里直接构造 tracker（不经过任务生命周期），所以不受
          「只有运行中才会崩溃」的限制，可稳定复现 / 锁定该路径。
    """
    tracker = RedisProgressTracker(
        task_id="t", analysts=None, research_depth="标准", llm_provider="deepseek"
    )

    # 归一化生效
    assert tracker.analysts == []

    # 崩溃点不再抛异常，且返回有效正数
    total = tracker._get_base_total_time()
    assert isinstance(total, float)
    assert total > 0


def test_len_none_is_the_actual_crash():
    """反向对照：证明「未归一化」的等价手工计算确实是崩溃点。

    修复前 tracker 里就是 `len(self.analysts)`，而 self.analysts 为 None。
    """
    analysts = None
    with pytest.raises(TypeError):
        len(analysts)


# ==================== 2. `.get` 默认值陷阱的语义守卫 ====================

def test_get_default_does_not_apply_when_key_present_with_none():
    """bug 机制：dict.get(key, default) 只在 key **不存在** 时使用 default。

    task_params 里 'selected_analysts' 这个 key 是存在的（只是值为 None），
    所以 `.get("selected_analysts", [])` 返回 None，而不是 []。
    """
    task_params = {"selected_analysts": None}

    # key 存在是关键（不是缺失）
    assert "selected_analysts" in task_params

    # 原写法：默认值不生效，拿到 None —— 这就是崩溃的来源
    assert task_params.get("selected_analysts", []) is None

    # 修复后的写法：`or []` 兜住 None
    assert (task_params.get("selected_analysts") or []) == []

    # 对照：key 真的不存在时，default 才生效
    assert {}.get("selected_analysts", []) == []


# ==================== 3. 源码级守卫：陷阱不得回流 ====================

TRAP_FILES = [
    "app/routers/analysis.py",
    "app/routers/workflows.py",
    "app/services/task_analysis_service.py",
    # 🆕 `selected_analysts=None` 家族的第二现场：简单分析服务里
    # 两处 parameters.get("selected_analysts", ["market", "fundamentals"]) 同属
    # 「key 存在但值为 None ⇒ default 不生效」陷阱，已改为统一走
    # `_resolve_selected_analysts()`。纳入守卫防回流。
    "app/services/simple_analysis_service.py",
]

FORBIDDEN_SNIPPETS = [
    'get("selected_analysts", [])',
    "get('selected_analysts', [])",
]


def test_no_default_arg_get_trap_for_selected_analysts():
    """源码级守卫：以后再把 `get("selected_analysts", [])` 写回来，测试直接失败。"""
    leaked = []
    for rel in TRAP_FILES:
        path = os.path.join(PROJECT_ROOT, rel)
        with open(path, "r", encoding="utf-8") as fh:
            content = fh.read()
        for snippet in FORBIDDEN_SNIPPETS:
            if snippet in content:
                leaked.append(f"{rel}: {snippet}")

    assert not leaked, (
        "不应再用 `get(\"selected_analysts\", [])`（key 存在但值为 None 时默认值不生效）：\n  - "
        + "\n  - ".join(leaked)
    )


def test_workflows_does_not_write_none_selected_analysts():
    """生产端守卫：workflows.py 不得再把 selected_analysts 写成 None。"""
    path = os.path.join(PROJECT_ROOT, "app/routers/workflows.py")
    with open(path, "r", encoding="utf-8") as fh:
        content = fh.read()

    # 旧的 bug 写法必须消失
    assert (
        "data.selected_analysts if hasattr(data, 'selected_analysts') else None" not in content
    ), "workflows.py 不得再用 `else None` 写法写入 selected_analysts"

    # 新的守卫写法应存在（有值才写）
    assert "if data.selected_analysts:" in content


# ==================== 4. 同源隐患 E：custom_params 标签过滤 ====================

def test_custom_params_merge_drops_none_mechanism():
    """E 层：**真调用**生产函数 ``workflows._sanitize_custom_params``。

    断言：None 值键被丢、四个保留键被丢、其余键保留、discarded 列表正确。
    刻意不在测试里重新实现一遍过滤逻辑——那正是「橡皮章」。
    """
    from app.routers.workflows import (
        _RESERVED_TASK_PARAM_KEYS,
        _sanitize_custom_params,
    )

    clean, dropped = _sanitize_custom_params({
        "selected_analysts": None,     # None 值 → 丢（否则会经 custom_params 灌回 None）
        "market_type": "a_share",      # 保留键 → 丢（会回盖归一化结果）
        "symbol": "自由文本",           # 保留键 → 丢（会绕过「真识别才写」守卫）
        "stock_code": "x",             # 保留键 → 丢
        "task_category": "general",    # 保留键 → 丢（会命中 symbol 豁免判据）
        "watch_sectors": "银行",        # 普通键 → 保留
    })

    assert clean == {"watch_sectors": "银行"}
    assert dropped == sorted(_RESERVED_TASK_PARAM_KEYS)
    assert dropped == ["market_type", "stock_code", "symbol", "task_category"]

    # 空输入不炸
    assert _sanitize_custom_params(None) == ({}, [])


def test_workflows_custom_params_merge_guard_source():
    """E 层源码守卫：workflows.py 不得再把 custom_params 原值无过滤 update 回去。

    写法对齐同文件 test_no_default_arg_get_trap_for_selected_analysts。
    """
    path = os.path.join(PROJECT_ROOT, "app/routers/workflows.py")
    with open(path, "r", encoding="utf-8") as fh:
        content = fh.read()

    # 旧的 bug 写法必须消失（会把 None 值与保留键原样盖回 task_params）
    assert (
        "k: v for k, v in data.custom_params.items() if v is not None" not in content
    ), "workflows.py 不得再内联无过滤合并 custom_params"

    # 新的守卫写法应存在（经 _sanitize_custom_params 过滤）
    assert "_sanitize_custom_params(data.custom_params)" in content


# ==================== 5. 同源隐患 F：engine 默认值替换 None ====================

def test_engine_defaults_replace_none_mechanism():
    """F 层机制：**显式判空**才能把「key 存在但值为 None」替换成默认值。

    这里锁的是「为什么不能用 setdefault」这一 Python 语义，不是重复实现业务逻辑：
    engine 对 research_depth 用 ``if not x.get(k)``、对两个数字参数用 ``is None``。
    """
    wi = {"research_depth": None, "lookback_days": None, "max_debate_rounds": None}

    # 复刻 unified_analysis_engine.py 的显式判空写法（生产侧即如此）
    if not wi.get("research_depth"):
        wi["research_depth"] = "标准"
    if wi.get("lookback_days") is None:
        wi["lookback_days"] = 30
    if wi.get("max_debate_rounds") is None:
        wi["max_debate_rounds"] = 3

    assert wi == {
        "research_depth": "标准",
        "lookback_days": 30,
        "max_debate_rounds": 3,
    }


def test_engine_defaults_keep_legit_zero():
    """F 层：合法零值不能被误判为「缺失」而替换成默认值。

    engine 对数字参数用 ``is None``（而非 ``not``），所以 ``lookback_days=0``
    会原样保留。反向证明 ``not 0`` 为真的陷阱确实存在。
    """
    wi = {"lookback_days": 0, "max_debate_rounds": 0}

    if wi.get("lookback_days") is None:
        wi["lookback_days"] = 30
    if wi.get("max_debate_rounds") is None:
        wi["max_debate_rounds"] = 3

    assert wi["lookback_days"] == 0
    assert wi["max_debate_rounds"] == 0

    # 陷阱对照：0 是 falsy —— 若误用 `if not wi.get("lookback_days"):` 会当作缺失
    assert (wi["lookback_days"] or "fallback") == "fallback"


def test_setdefault_does_not_override_none():
    """F 层机制根因：``dict.setdefault`` 在「key 存在但值为 None」时不生效。"""
    d = {"research_depth": None}
    assert "research_depth" in d  # key 存在（不是缺失）

    d.setdefault("research_depth", "标准")
    assert d["research_depth"] is None  # 未被替换 —— 默认值不生效

    # 对照：key 真缺失时 setdefault 才生效
    e = {}
    e.setdefault("research_depth", "标准")
    assert e["research_depth"] == "标准"


def test_engine_no_setdefault_source_guard():
    """F 层源码守卫：engine 不得对这三个参数再用 ``setdefault(``（上一轮已修复）。"""
    path = os.path.join(PROJECT_ROOT, "app/services/unified_analysis_engine.py")
    with open(path, "r", encoding="utf-8") as fh:
        content = fh.read()

    leaked = []
    for key in ("research_depth", "lookback_days", "max_debate_rounds"):
        for snippet in (f'setdefault("{key}"', f"setdefault('{key}'"):
            if snippet in content:
                leaked.append(f"{key}: {snippet}")

    assert not leaked, (
        "engine 不得对 research_depth/lookback_days/max_debate_rounds 用 setdefault"
        "（key 存在但值为 None 时默认值不生效）：\n  - " + "\n  - ".join(leaked)
    )

    # 正向：显式判空写法应存在
    assert 'if not workflow_inputs.get("research_depth"):' in content
    assert 'if workflow_inputs.get("lookback_days") is None:' in content
    assert 'if workflow_inputs.get("max_debate_rounds") is None:' in content


# ==================== 6. 同源隐患 G：builder participants 兜底 ====================

def test_identify_debate_configs_tolerates_none_participants():
    """G 层机制：``config.get("participants") or []`` 能兜住 None。

    锁的是「为什么不能用 ``get(k, default)``」：key 存在但值为 None 时默认值不生效。
    对应 core/workflow/builder.py::_identify_debate_configs 的读法。
    """
    config = {"participants": None}
    assert "participants" in config

    # 陷阱写法：默认值不生效，返回 None → 下游 len/iter 崩溃
    assert config.get("participants", []) is None
    # 正确写法：`or []` 兜住 None
    assert (config.get("participants") or []) == []


def test_builder_no_default_arg_get_trap_for_participants():
    """G 层源码守卫：builder.py 读取 participants 必须是 ``or []`` 形态（上一轮已修复）。"""
    path = os.path.join(PROJECT_ROOT, "core/workflow/builder.py")
    with open(path, "r", encoding="utf-8") as fh:
        content = fh.read()

    assert 'node.config.get("participants") or []' in content
    assert 'node.config.get("participants", [])' not in content


if __name__ == "__main__":
    test_tracker_tolerates_none_analysts()
    test_len_none_is_the_actual_crash()
    test_get_default_does_not_apply_when_key_present_with_none()
    test_no_default_arg_get_trap_for_selected_analysts()
    test_workflows_does_not_write_none_selected_analysts()
    # 同源隐患 E/F/G
    test_custom_params_merge_drops_none_mechanism()
    test_workflows_custom_params_merge_guard_source()
    test_engine_defaults_replace_none_mechanism()
    test_engine_defaults_keep_legit_zero()
    test_setdefault_does_not_override_none()
    test_engine_no_setdefault_source_guard()
    test_identify_debate_configs_tolerates_none_participants()
    test_builder_no_default_arg_get_trap_for_participants()
    print("\n🎉 全部通过")
