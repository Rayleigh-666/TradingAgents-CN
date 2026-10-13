#!/usr/bin/env python3
"""
本地推理服务判定（is_local_service）回归测试

背景：
embedding 链路原先只对 `name == "ollama"` 免 API Key，导致
  1. 接 LM Studio / oMLX / vLLM 时必须往正式配置里塞**假 Key** 才能过守卫；
  2. `localai` 出现在免检白名单，却在预检查里被当云端厂商要求 Key（不对称 bug）。
现统一为 `app.utils.api_key_utils.is_local_service(name, base_url)`：
判据 = 名字白名单 **或** 地址指向本机。

本用例纯函数级 + 源码级，不依赖 MongoDB / 网络。
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.utils.api_key_utils import is_local_service, is_valid_api_key  # noqa: E402

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_local_names_are_detected():
    """名字白名单内的本地推理工具，即使不给地址也判为本地服务。"""
    for name in [
        "ollama",
        "localai",
        "lmstudio",
        "lm-studio",
        "vllm",
        "sglang",
        "xinference",
        "mlx",
        "omlx",
        "llamacpp",
    ]:
        assert is_local_service(name) is True, f"{name} 应判为本地服务"
        assert is_local_service(name.upper()) is True, f"{name} 大写也应命中"
    print("✅ 名字白名单：10 个本地工具全部识别")


def test_localhost_address_is_detected():
    """名字不认识，但地址指向本机 → 也判为本地服务（这正是免配假 key 的关键）。"""
    cases = [
        ("my-custom-server", "http://localhost:11434/v1"),
        ("whatever", "http://127.0.0.1:1234/v1"),
        ("whatever", "http://0.0.0.0:8000/v1"),
        ("whatever", "http://[::1]:8080/v1"),
        ("whatever", "http://host.docker.internal:1234/v1"),
        ("whatever", "127.0.0.1:1234"),  # 省略 scheme 的容错写法
    ]
    for name, url in cases:
        assert is_local_service(name, url) is True, f"{url} 应判为本地服务"
    print("✅ 地址判据：localhost / 127.0.0.1 / ::1 / 0.0.0.0 / docker 内主机 全部命中")


def test_cloud_providers_are_not_local():
    """云端厂家 + 公网地址 → 不是本地服务（必须走 Key 校验）。"""
    cases = [
        ("dashscope", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
        ("deepseek", "https://api.deepseek.com/v1"),
        ("openai", "https://api.openai.com/v1"),
        ("siliconflow", "https://api.siliconflow.cn/v1"),
        ("", ""),
        (None, None),
    ]
    for name, url in cases:
        assert is_local_service(name, url) is False, f"{name}/{url} 不应判为本地服务"
    print("✅ 云端厂家：6 组均未被误判为本地服务")


def test_hostname_prefix_attack_is_rejected():
    """安全边界：'localhost.evil.com' 不是本机地址，不能被当成 localhost。"""
    assert is_local_service("x", "http://localhost.evil.com/v1") is False
    assert is_local_service("x", "http://127.0.0.1.evil.com/v1") is False
    print("✅ 安全边界：localhost.evil.com 之类的前缀伪装被拒绝")


def test_old_narrow_guard_would_have_missed_lmstudio():
    """非空转：显式证明旧的「只认 ollama」判据会漏掉 lmstudio。"""
    old_guard = lambda name, base_url: name == "ollama"  # noqa: E731
    assert old_guard("lmstudio", "http://127.0.0.1:1234/v1") is False
    assert is_local_service("lmstudio", "http://127.0.0.1:1234/v1") is True
    assert old_guard("whatever", "http://127.0.0.1:1234/v1") is False
    assert is_local_service("whatever", "http://127.0.0.1:1234/v1") is True
    print("✅ 非空转：旧窄判据确会漏判 lmstudio / localhost，新判据已覆盖")


def test_source_guard_no_hardcoded_local_whitelist():
    """源码级守卫：禁止「只认名字」的硬编码白名单回流。"""
    targets = [
        "core/llm/embedding_manager.py",
        "core/memory/config.py",
        "app/utils/api_key_utils.py",
    ]
    # 旧写法特征：in ["ollama", "localai"] / name == "ollama" and
    forbidden = [
        re.compile(r"""in\s*\[\s*["']ollama["']\s*,\s*["']localai["']\s*\]"""),
        re.compile(r"""name\s*==\s*["']ollama["']\s*and"""),
    ]
    for rel in targets:
        path = os.path.join(PROJECT_ROOT, rel)
        with open(path, "r", encoding="utf-8") as f:
            src = f.read()
        for pattern in forbidden:
            m = pattern.search(src)
            assert m is None, f"{rel} 仍存在窄判据: {m.group(0) if m else ''}"
    print("✅ 源码级守卫：3 个文件已无「只认 ollama」的硬编码判据")


def test_local_placeholder_key_is_usable():
    """mem0 兜底用的 'sk-local' 不能被占位符守卫误杀。"""
    assert is_valid_api_key("sk-local") is True
    assert is_valid_api_key("") is False
    print("✅ 兜底 Key：'sk-local' 可用，空值仍被拒")


if __name__ == "__main__":
    test_local_names_are_detected()
    test_localhost_address_is_detected()
    test_cloud_providers_are_not_local()
    test_hostname_prefix_attack_is_rejected()
    test_old_narrow_guard_would_have_missed_lmstudio()
    test_source_guard_no_hardcoded_local_whitelist()
    test_local_placeholder_key_is_usable()
    print("\n🎉 全部通过")
