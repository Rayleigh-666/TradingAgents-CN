#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
节点失败降级文案必须带「真实错误摘要」回归测试。

回归背景
--------
`core/workflow/builder.py` 的节点 except 分支只把 `type(exc).__name__`
写进降级文案，于是界面各板块只剩：

    [基本面分析师 v2.0 执行失败]
    该分析节点在运行过程中遇到错误，已跳过。
    错误摘要: ValueError          ← 真正的 401 invalid_api_key 被吞掉

而 `core/agents/base.py` 实际抛出的是
`ValueError("LLM API 调用失败（已重试 3 次，模型 qwen-turbo）: Error code: 401 ...")`。

修复口径
--------
降级文案改为由 `app.utils.error_formatter.build_node_failure_message()` 生成：
含错误类型 + **真实错误摘要**（脱敏、折叠空白、截断）+ 一句可操作提示；
写入 `_node_errors` 的 error 同样脱敏。

本用例纯函数级，不依赖 MongoDB / 网络 / 重型依赖。
"""

import os
import sys

# 支持 `python tests/test_degraded_error_summary.py` 直接运行（pytest 走 conftest.py）
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from app.utils.error_formatter import (  # noqa: E402
    DEGRADED_ERROR_MAX_LEN,
    build_node_failure_message,
    redact_secrets,
    summarize_error,
)


# ==================== 脱敏 ====================

def test_redact_secrets_masks_sk_keys():
    text = "Error code: 401 - Incorrect API key provided: sk-abcdef1234567890abcdef"
    out = redact_secrets(text)
    assert "sk-abcdef1234567890abcdef" not in out
    assert "sk-***" in out
    print("✅ sk-* 形态密钥被脱敏")


def test_redact_secrets_masks_key_value_pairs():
    for text in (
        "api_key=abcdef1234567890",
        "API-KEY: abcdef1234567890",
        "token = 'abcdef1234567890'",
        "secret=abcdef1234567890",
        "Authorization: Bearer abcdef1234567890",
    ):
        out = redact_secrets(text)
        assert "abcdef1234567890" not in out, f"未脱敏: {text!r} -> {out!r}"
    print("✅ key=value / Bearer 形态被脱敏")


def test_redact_secrets_keeps_normal_text():
    text = "Error code: 401 - invalid_api_key"
    assert redact_secrets(text) == text
    assert redact_secrets("") == ""
    assert redact_secrets(None) == ""
    print("✅ 普通错误文本不被误伤")


# ==================== 摘要 ====================

def test_summarize_error_collapses_and_truncates():
    raw = "line1\n\n  line2\tline3"
    assert summarize_error(raw) == "line1 line2 line3"

    long_raw = "x" * (DEGRADED_ERROR_MAX_LEN + 50)
    out = summarize_error(long_raw)
    assert len(out) == DEGRADED_ERROR_MAX_LEN + 1  # 截断后追加一个省略号
    assert out.endswith("…")

    # max_len<=0 表示不截断
    assert summarize_error(long_raw, max_len=0) == long_raw
    print("✅ summarize_error：折叠空白 + 截断")


def test_summarize_error_from_exception():
    exc = ValueError("LLM API 调用失败（已重试 3 次）: Error code: 401 - invalid_api_key")
    out = summarize_error(exc)
    assert "401" in out and "invalid_api_key" in out
    print("✅ 从异常对象能提取出真实原因")


# ==================== 降级文案 ====================

def test_failure_message_contains_real_error_not_just_type():
    exc = ValueError(
        "LLM API 调用失败（已重试 3 次，模型 qwen-turbo）: "
        "Error code: 401 - {'error': {'code': 'invalid_api_key'}}"
    )
    msg = build_node_failure_message("基本面分析师 v2.0", exc)

    # 1. 真实原因必须出现（这是本次修复的核心）
    assert "401" in msg, msg
    assert "invalid_api_key" in msg, msg
    assert "qwen-turbo" in msg, msg

    # 2. 不能只显示异常类型名
    assert "错误摘要: ValueError" not in msg, msg
    assert "错误类型: ValueError" in msg, msg
    assert "基本面分析师 v2.0" in msg, msg

    # 3. 应带一句可操作提示
    assert "可能原因" in msg and "API Key" in msg, msg
    print("✅ 降级文案含真实 401 原因 + 模型名 + 可操作提示")


def test_failure_message_does_not_leak_secret():
    exc = RuntimeError("request failed, api_key=abcdef1234567890xyz")
    msg = build_node_failure_message("新闻分析师", exc)
    assert "abcdef1234567890xyz" not in msg, msg
    print("✅ 降级文案不泄漏密钥")


def test_failure_message_hint_variants():
    cases = [
        ("Error code: 429 - rate limit exceeded", "额度不足"),
        ("HTTPSConnectionPool: Read timed out", "连接失败"),
        ("data_inspection_failed", "内容审核"),
    ]
    for raw, expected_hint_fragment in cases:
        msg = build_node_failure_message("某分析师", RuntimeError(raw))
        assert expected_hint_fragment in msg, f"{raw!r} -> {msg!r}"
    print("✅ 限流 / 网络 / 内容审核各有对应提示")


def test_failure_message_handles_plain_string():
    msg = build_node_failure_message("某分析师", "some raw error text")
    assert "some raw error text" in msg
    assert "错误类型: Error" in msg
    print("✅ 传入纯字符串也能构造降级文案")


if __name__ == "__main__":
    test_redact_secrets_masks_sk_keys()
    test_redact_secrets_masks_key_value_pairs()
    test_redact_secrets_keeps_normal_text()
    test_summarize_error_collapses_and_truncates()
    test_summarize_error_from_exception()
    test_failure_message_contains_real_error_not_just_type()
    test_failure_message_does_not_leak_secret()
    test_failure_message_hint_variants()
    test_failure_message_handles_plain_string()
    print("\n🎉 全部通过")
