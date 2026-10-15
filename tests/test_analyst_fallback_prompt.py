"""回归护栏：大盘/板块分析师 v2 的降级提示词路径。

背景
----
``IndexAnalystV2._build_system_prompt`` / ``SectorAnalystV2._build_system_prompt``
在模板获取为空时会走「降级」分支返回一段默认提示词。历史上该分支的 f-string
引用了从未定义的局部变量 ``tool_descriptions``，导致降级路径本身抛
``NameError: name 'tool_descriptions' is not defined`` —— 本应优雅兜底却崩溃。

本文件**真实执行**降级分支（而非扫描源码），断言：

1. 不抛异常；
2. 返回非空字符串；
3. 不含未渲染的字面量 ``{tool_descriptions}``；
4. 工具说明确实被渲染进提示词。

关于「工具说明被渲染进去」的断言方式
------------------------------------
``_get_tool_descriptions()`` 只读取 ``self._langchain_tools``，方法内已有
``if not self._langchain_tools: return "（无可用工具）"`` 兜底。测试中注入一个假的工具
对象（带 name/description），即可在无需真实工具加载的环境下验证其返回值确实被嵌入
提示词——这比仅仅回退断言「（无可用工具）」兜底文案提供了更强的「确实渲染」证据。
（若某环境确实无工具，方法会退化为「（无可用工具）」；本测试另有一条用例专门覆盖该
兜底文案，见 ``test_fallback_prompt_without_tools``。）

关于 qmt 分支的覆盖方式（重要）
--------------------------------
``_build_system_prompt`` 在判断降级之前会先调用
``self._apply_mode_prompt_suffix(prompt, mode, "system")``。当 ``mode == "qmt"`` 时，
该方法返回 ``f"{prompt}{suffix}"``，而 suffix 恒非空，因此紧随其后的
``if prompt: return prompt`` 会提前返回，使 ``mode == "qmt"`` 的降级分支在正常路径下
**不可达**（返回的只是 QMT 约束后缀）。

为了真正执行到 qmt 降级代码，qmt 用例额外把 ``_apply_mode_prompt_suffix`` 临时 patch
成恒等函数（原样返回 prompt）。这是一种测试隔离手段，用于绕开上述提前返回遮蔽，
不改变被测的降级分支自身逻辑（该遮蔽与本次修复无关，按需求不加改动）。
"""

import pytest

from core.agents.adapters.index_analyst_v2 import IndexAnalystV2
from core.agents.adapters.sector_analyst_v2 import SectorAnalystV2


class _FakeTool:
    """最小化的假工具对象，满足 _get_tool_descriptions 的 name/description 读取。"""

    def __init__(self, name: str, description: str):
        self.name = name
        self.description = description


FAKE_TOOLS = [_FakeTool("fake_market_tool", "用于测试的行情工具描述")]

ADAPTERS = [
    (IndexAnalystV2, "index_analyst_v2"),
    (SectorAnalystV2, "sector_analyst_v2"),
]


def _make_agent(cls, tools):
    """绕开 __init__（避免真实 LLM/工具加载），仅装配被测方法所需属性。"""
    agent = cls.__new__(cls)
    agent._langchain_tools = tools
    return agent


def _patch_template_with(agent, return_value):
    """把模板获取入口 patch 成固定返回值（模拟模板拉取失败为空）。"""
    agent._get_prompt_from_template = lambda *args, **kwargs: return_value


def _patch_identity_suffix(agent):
    """解除 qmt 下的提前返回遮蔽，以便执行到 qmt 降级分支（见文件 docstring）。"""
    agent._apply_mode_prompt_suffix = lambda prompt, mode, prompt_type: prompt


@pytest.mark.parametrize("cls,label", ADAPTERS)
@pytest.mark.parametrize("mode", ["qmt", "tushare"])
def test_fallback_prompt_no_nameerror(cls, label, mode):
    """模板为空 -> 走降级分支，不得抛 NameError，且工具说明被渲染。"""
    agent = _make_agent(cls, FAKE_TOOLS)
    _patch_template_with(agent, "")
    if mode == "qmt":
        # 正常路径下 qmt 降级分支被 _apply_mode_prompt_suffix 的提前返回遮蔽，
        # 必须 patch 成恒等才能真实执行到该分支（原因见文件 docstring）。
        _patch_identity_suffix(agent)
    state = {"_analysis_data_source": mode}

    # 本缺陷核心：此前此处会抛 NameError: name 'tool_descriptions' is not defined
    out = agent._build_system_prompt(market_type="A股", state=state)

    assert isinstance(out, str) and out, f"{label}/{mode}: 降级提示词不应为空字符串"
    assert "{tool_descriptions}" not in out, f"{label}/{mode}: 存在未渲染的占位符"
    assert "fake_market_tool" in out, f"{label}/{mode}: 工具说明未被渲染进提示词"


@pytest.mark.parametrize("cls,label", ADAPTERS)
@pytest.mark.parametrize("mode", ["qmt", "tushare"])
def test_fallback_prompt_none_template_no_typeerror(cls, label, mode):
    """模板返回 None 时不得因 len(None) 抛 TypeError，同样应走降级分支。"""
    agent = _make_agent(cls, FAKE_TOOLS)
    _patch_template_with(agent, None)
    if mode == "qmt":
        _patch_identity_suffix(agent)
    state = {"_analysis_data_source": mode}

    out = agent._build_system_prompt(market_type="A股", state=state)

    assert isinstance(out, str) and out, f"{label}/{mode}: None 模板下降级提示词不应为空"
    assert "{tool_descriptions}" not in out, f"{label}/{mode}: 存在未渲染的占位符"
    assert "fake_market_tool" in out, f"{label}/{mode}: 工具说明未被渲染进提示词"


@pytest.mark.parametrize("cls,label", ADAPTERS)
def test_fallback_prompt_without_tools(cls, label):
    """无工具时 _get_tool_descriptions 应退化为「（无可用工具）」兜底文案，且不抛异常。"""
    agent = _make_agent(cls, [])
    _patch_template_with(agent, "")
    state = {"_analysis_data_source": "tushare"}

    out = agent._build_system_prompt(market_type="A股", state=state)

    assert isinstance(out, str) and out, f"{label}: 降级提示词不应为空字符串"
    assert "{tool_descriptions}" not in out, f"{label}: 存在未渲染的占位符"
    assert "（无可用工具）" in out, f"{label}: 应渲染无工具兜底文案"
