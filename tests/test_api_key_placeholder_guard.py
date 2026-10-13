#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
占位符 API Key 守卫回归测试。

回归背景
--------
`.env` 里的模板值 `DASHSCOPE_API_KEY=your_dashscope_api_key_here` 曾被
「不等于字面量 'your-api-key' 且不以 'sk-xxx' 开头」这种**窄判断**放行，
被当成有效 Key 发给阿里云百炼，收到 401 invalid_api_key，进而导致整份
港股分析报告各板块全部降级为占位文案（用户可见「错误摘要: ValueError」）。

修复口径
--------
全项目判断「Key 是否可用」统一走 `app.utils.api_key_utils.is_valid_api_key`；
新增 `is_placeholder_api_key` 做**通用**占位符识别（your_*、*_here、sk-xxx、
<YOUR_API_KEY>、changeme、含 '...' 等）。分析链路（unified_analysis_engine /
workflow builder / workflows router）里的窄判断已全部替换。

后续补漏：**Embedding 链路**（`core/llm/embedding_manager.py`、
`core/memory/config.py`）当时漏掉了——它只判 `if api_key:`（空值），占位符照样
放行，导致「Embedding API 不可用，跳过索引构建」。现已一并接入统一口径。

本用例纯函数级，不依赖 MongoDB / 网络。
"""

import os
import sys

# 支持 `python tests/test_api_key_placeholder_guard.py` 直接运行（pytest 走 conftest.py）
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from app.utils.api_key_utils import (  # noqa: E402
    is_placeholder_api_key,
    is_valid_api_key,
    should_skip_api_key_update,
)


# 真实形态的密钥（合成值，仅用于验证「不会被误判」）
REAL_LOOKING_KEY = "sk-" + "a1b2c3d4e5f60718293a4b5c6d7e8f90"


# ==================== 通用占位符识别 ====================

PLACEHOLDER_CASES = [
    ("your_dashscope_api_key_here", "历史 bug 原文：.env 模板值"),
    ("your-api-key", "旧窄判断唯一挡住的字面量"),
    ("your_api_key", "your_ 前缀"),
    ("sk-xxx", "sk-xxx 字面量"),
    ("xxxxxxxxxxxxxxxx", "纯 x 占位"),
    ("<YOUR_API_KEY>", "尖括号模板"),
    ("{API_KEY}", "花括号模板"),
    ("placeholder", "placeholder"),
    ("changeme", "changeme"),
    ("change_me_now", "change_me"),
    ("replace_me", "replace_me"),
    ("example_key_123456", "example"),
    ("dummy-key", "dummy"),
    ("sk-abc...def", "省略号截断"),
    ("sk-abc…def", "中文省略号截断"),
    ("你的密钥", "中文模板"),
    ("YOUR-KEY-HERE", "大小写不敏感"),
]


def test_placeholder_cases_are_detected():
    for value, why in PLACEHOLDER_CASES:
        assert is_placeholder_api_key(value) is True, f"应判为占位符但漏判: {value!r} ({why})"
    print(f"✅ 占位符识别：{len(PLACEHOLDER_CASES)}/{len(PLACEHOLDER_CASES)} 命中")


def test_empty_is_not_a_placeholder():
    # 空值/None 语义是「未配置」而非「占位符」，由 is_valid_api_key 处理
    for value in (None, "", "   "):
        assert is_placeholder_api_key(value) is False, f"空值不应判为占位符: {value!r}"
    print("✅ 空值不被判为占位符（由 is_valid_api_key 负责判空）")


def test_real_key_is_not_a_placeholder():
    assert is_placeholder_api_key(REAL_LOOKING_KEY) is False
    assert is_placeholder_api_key("d1el869r01qghj41hahgd1el869r01qghj41hai0") is False
    print("✅ 正常密钥不被误判为占位符")


# ==================== is_valid_api_key 综合判定 ====================

def test_is_valid_api_key_rejects_placeholders():
    for value, why in PLACEHOLDER_CASES:
        assert is_valid_api_key(value) is False, f"占位符应判为无效: {value!r} ({why})"

    for value in (None, "", "   ", "abc", "1234"):
        assert is_valid_api_key(value) is False, f"空/过短应判为无效: {value!r}"
    print("✅ is_valid_api_key：占位符与空/过短值一律判为无效")


def test_is_valid_api_key_accepts_real_key():
    assert is_valid_api_key(REAL_LOOKING_KEY) is True
    # 环境变量里常见的带引号写法应能通过（去空白后判定）
    assert is_valid_api_key(f"  {REAL_LOOKING_KEY}  ") is True
    print("✅ is_valid_api_key：正常密钥判为有效")


def test_old_narrow_guard_would_have_accepted_the_env_placeholder():
    """非空转：证明旧窄判断确实会放行历史 bug 的模板值。"""
    value = "your_dashscope_api_key_here"

    def old_narrow_guard(key: str) -> bool:
        return bool(key) and key.strip() != "your-api-key" and not key.startswith("sk-xxx")

    assert old_narrow_guard(value) is True, "旧窄判断本应放行该模板值（这才是 bug 所在）"
    assert is_valid_api_key(value) is False, "新判定必须拒绝该模板值"
    print("✅ 非空转：旧窄判断放行 your_dashscope_api_key_here，新判定拒绝")


# ==================== should_skip_api_key_update 口径统一 ====================

def test_should_skip_matches_placeholder_semantics():
    for value, _ in PLACEHOLDER_CASES:
        assert should_skip_api_key_update(value) is True, f"占位符应跳过更新: {value!r}"

    assert should_skip_api_key_update(None) is False
    assert should_skip_api_key_update("") is False
    assert should_skip_api_key_update(REAL_LOOKING_KEY) is False
    print("✅ should_skip_api_key_update 与占位符口径一致")


# ==================== 源码级守卫：窄判断不得回流 ====================

ANALYSIS_PATH_FILES = [
    "app/services/unified_analysis_engine.py",
    "core/workflow/builder.py",
    "app/routers/workflows.py",
]

FORBIDDEN_SNIPPETS = [
    '!= "your-api-key"',
    ".startswith(\"sk-xxx\")",
    ".startswith('sk-xxx')",
]


def test_analysis_paths_do_not_use_narrow_guard():
    leaked = []
    for rel in ANALYSIS_PATH_FILES:
        path = os.path.join(PROJECT_ROOT, rel)
        with open(path, "r", encoding="utf-8") as fh:
            content = fh.read()
        for snippet in FORBIDDEN_SNIPPETS:
            if snippet in content:
                leaked.append(f"{rel}: {snippet}")

    assert not leaked, (
        "分析链路不应再出现窄判断（应统一用 is_valid_api_key）：\n  - "
        + "\n  - ".join(leaked)
    )
    print("✅ 源码级守卫：3 个分析链路文件已无窄判断残留")


# ==================== 源码级守卫：Embedding 链路必须走统一口径 ====================

EMBEDDING_PATH_FILES = [
    "core/llm/embedding_manager.py",
    "core/memory/config.py",
]


def test_embedding_paths_use_unified_key_guard():
    """Embedding 链路曾漏掉守卫：只判 `if api_key:` 空值，不识别占位符，
    导致 `.env` 模板值被当成有效 Key → 每次调用 401 → 连续 3 次后熔断 →
    日志出现「Embedding API 不可用，跳过索引构建」。此处锁定必须接入统一口径。
    """
    leaked = []
    for rel in EMBEDDING_PATH_FILES:
        path = os.path.join(PROJECT_ROOT, rel)
        with open(path, "r", encoding="utf-8") as fh:
            content = fh.read()

        if "is_valid_api_key" not in content:
            leaked.append(f"{rel}: 未接入 is_valid_api_key（仍在自判空 / 自写窄判断）")

        for snippet in FORBIDDEN_SNIPPETS:
            if snippet in content:
                leaked.append(f"{rel}: {snippet}")

    assert not leaked, "Embedding 链路必须统一走 is_valid_api_key：\n  - " + "\n  - ".join(leaked)
    print("✅ 源码级守卫：Embedding 链路已接入统一 Key 口径，无窄判断残留")


if __name__ == "__main__":
    test_placeholder_cases_are_detected()
    test_empty_is_not_a_placeholder()
    test_real_key_is_not_a_placeholder()
    test_is_valid_api_key_rejects_placeholders()
    test_is_valid_api_key_accepts_real_key()
    test_old_narrow_guard_would_have_accepted_the_env_placeholder()
    test_should_skip_matches_placeholder_semantics()
    test_analysis_paths_do_not_use_narrow_guard()
    test_embedding_paths_use_unified_key_guard()
    print("\n🎉 全部通过")
