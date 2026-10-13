"""
API Key 处理工具函数

提供统一的 API Key 验证、缩略、环境变量读取等功能
"""

import os
from typing import Optional


# 判定「模板 / 占位符」Key 的特征片段（按小写子串比对，命中即视为未配置）。
# 注意：这里刻意用「子串」而非「前后缀」，因为模板值形态很多，
# 例如 your_dashscope_api_key_here、sk-xxx、<YOUR_API_KEY>、changeme 等。
_PLACEHOLDER_FRAGMENTS = (
    "your",          # your_api_key / your-api-key / your_dashscope_api_key_here
    "xxx",           # sk-xxx / xxxxxxxxx
    "placeholder",   # placeholder / placeholder_api_key
    "change_me",
    "changeme",
    "replace_me",
    "insert_",
    "example",       # example_key
    "dummy",
    "fake",
    "todo",
    "_here",         # 模板后缀 _here / -here
    "-here",
    "填入",
    "占位",
    "密钥",          # 中文模板「你的密钥」
    "你的",
)


# 本机推理服务的常见 provider 名（这些服务跑在本地，**不需要** API Key）。
# 注意：白名单只是「名字兜底」，真正的判据还包含「地址指向本机」（见 is_local_service）。
_LOCAL_SERVICE_NAMES = frozenset({
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
    "llama.cpp",
    "text-generation-webui",
    "jdyun-local",
})

# 判定为「本机」的 host（urlparse 后的 hostname，已小写）
_LOCAL_HOSTS = frozenset({
    "localhost",
    "127.0.0.1",
    "0.0.0.0",
    "::1",
    "[::1]",
    "host.docker.internal",
})


def _extract_host(base_url: Optional[str]) -> str:
    """从 base_url 里取出 hostname（小写）。解析失败返回空串。"""
    value = str(base_url or "").strip()
    if not value:
        return ""
    # 容错：允许省略 scheme 的写法（如 "127.0.0.1:1234/v1"）
    if "://" not in value:
        value = f"http://{value}"
    try:
        from urllib.parse import urlparse

        return (urlparse(value).hostname or "").lower()
    except Exception:
        return ""


def is_local_service(provider_name: Optional[str], base_url: Optional[str] = None) -> bool:
    """判断一个 provider 是否是「跑在本机的推理服务」（免 API Key）。

    判据是「名字在白名单 **或** 地址指向本机」，而不是只看名字：

    1. 名字命中 `_LOCAL_SERVICE_NAMES`（ollama / localai / lmstudio / vllm / mlx …）
    2. 或 `base_url` 的 host 是 localhost / 127.0.0.1 / ::1 / 0.0.0.0 / host.docker.internal

    历史 bug（本函数存在的原因）：
    - embedding 链路原先只对 `name == "ollama"` 免 Key，于是接 LM Studio / oMLX /
      vLLM 时必须往正式配置里塞一个**假 Key** 才能过守卫；
    - 同时 `localai` 出现在「免检白名单」，却在预检查处被当成云端厂商要求 Key，
      两处判定不一致（不对称 bug）。
    统一到本函数后，换任何本地推理工具都不必再改代码或伪造 Key。

    Args:
        provider_name: 厂家标识（如 'lmstudio'）
        base_url: 该厂家的 OpenAI 兼容地址（如 'http://127.0.0.1:1234/v1'）

    Returns:
        bool: True 表示是本地服务（不需要 API Key）
    """
    name = str(provider_name or "").strip().lower()
    if name and name in _LOCAL_SERVICE_NAMES:
        return True

    host = _extract_host(base_url)
    return bool(host) and host in _LOCAL_HOSTS


def is_placeholder_api_key(api_key: Optional[str]) -> bool:
    """
    判断一个字符串是否**明显是模板 / 占位符**，而不是真实密钥。

    与 is_valid_api_key 的区别：本函数**不判空**——空值/None 返回 False，
    它只回答「这是不是一个占位符」，用于日志提示与守卫分支的语义说明。

    识别规则（任一命中即判为占位符）：
    1. 尖括号包裹的模板值：<YOUR_API_KEY>
    2. 含省略号（截断示意）：sk-abc... / sk-abc…
    3. 含常见占位词片段：your / xxx / placeholder / changeme / 你的 / 密钥 等

    历史教训：`.env` 里的模板值 `your_dashscope_api_key_here` 曾被
    「不等于字面量 'your-api-key' 且不以 'sk-xxx' 开头」这种窄判断放行，
    当成有效 Key 发出去，结果收到 401 invalid_api_key。

    Args:
        api_key: 待判断的值

    Returns:
        bool: True 表示是占位符
    """
    if api_key is None:
        return False

    value = str(api_key).strip()
    if not value:
        return False

    lowered = value.lower()

    # 1. 尖括号模板：<YOUR_API_KEY> / {API_KEY}
    if (lowered.startswith("<") and lowered.endswith(">")) or (
        lowered.startswith("{") and lowered.endswith("}")
    ):
        return True

    # 2. 省略号（截断 / 示意）
    if "..." in lowered or "…" in lowered:
        return True

    # 3. 常见占位词片段
    return any(fragment in lowered for fragment in _PLACEHOLDER_FRAGMENTS)


def is_valid_api_key(api_key: Optional[str]) -> bool:
    """
    判断 API Key 是否有效

    有效的 API Key 必须满足：
    1. 不能为空
    2. 长度必须 >= 5
    3. 不能是占位符（见 is_placeholder_api_key：your_*、*_here、sk-xxx、
       <YOUR_API_KEY>、changeme、含 '...' 等一律视为无效）

    这是全项目判断「Key 是否真的可用」的**唯一口径**，分析链路、配置接口、
    就绪检查都应复用它，不要在业务侧另写「!= 'your-api-key' and not
    startswith('sk-xxx')」这类窄判断（历史 bug 来源，见 is_placeholder_api_key）。

    Args:
        api_key: 要验证的 API Key

    Returns:
        bool: 是否有效
    """
    if not api_key:
        return False

    api_key = str(api_key).strip()

    # 1. 不能为空
    if not api_key:
        return False

    # 2. 长度必须 >= 5
    if len(api_key) < 5:
        return False

    # 3. 不能是占位符 / 截断值
    if is_placeholder_api_key(api_key):
        return False

    return True


def truncate_api_key(api_key: Optional[str]) -> Optional[str]:
    """
    缩略 API Key，显示前6位和后6位
    
    示例：
        输入：'d1el869r01qghj41hahgd1el869r01qghj41hai0'
        输出：'d1el86...j41hai0'
    
    Args:
        api_key: 要缩略的 API Key
        
    Returns:
        str: 缩略后的 API Key，如果输入为空或长度 <= 12 则返回原值
    """
    if not api_key or len(api_key) <= 12:
        return api_key
    
    return f"{api_key[:6]}...{api_key[-6:]}"


def get_env_api_key_for_provider(provider_name: str) -> Optional[str]:
    """
    从环境变量获取大模型厂家的 API Key
    
    环境变量名格式：{PROVIDER_NAME}_API_KEY
    
    Args:
        provider_name: 厂家名称（如 'deepseek', 'dashscope'）
        
    Returns:
        str: 环境变量中的 API Key，如果不存在或无效则返回 None
    """
    env_key_name = f"{provider_name.upper()}_API_KEY"
    env_key = os.getenv(env_key_name)
    
    if env_key and is_valid_api_key(env_key):
        return env_key
    
    return None


def get_env_api_key_for_datasource(ds_type: str) -> Optional[str]:
    """
    从环境变量获取数据源的 API Key
    
    数据源类型到环境变量名的映射：
    - tushare → TUSHARE_TOKEN
    - finnhub → FINNHUB_API_KEY
    - polygon → POLYGON_API_KEY
    - iex → IEX_API_KEY
    - quandl → QUANDL_API_KEY
    - alphavantage → ALPHAVANTAGE_API_KEY
    
    Args:
        ds_type: 数据源类型（如 'tushare', 'finnhub'）
        
    Returns:
        str: 环境变量中的 API Key，如果不存在或无效则返回 None
    """
    # 数据源类型到环境变量名的映射
    env_key_map = {
        "tushare": "TUSHARE_TOKEN",
        "qmt": "QMT_TOKEN",
        "finnhub": "FINNHUB_API_KEY",
        "polygon": "POLYGON_API_KEY",
        "iex": "IEX_API_KEY",
        "quandl": "QUANDL_API_KEY",
        "alphavantage": "ALPHAVANTAGE_API_KEY",
    }
    
    env_key_name = env_key_map.get(ds_type.lower())
    if not env_key_name:
        return None
    
    env_key = os.getenv(env_key_name)
    
    if env_key and is_valid_api_key(env_key):
        return env_key
    
    return None


def should_skip_api_key_update(api_key: Optional[str]) -> bool:
    """
    判断是否应该跳过 API Key 的更新

    以下情况应该跳过更新（保留原值）：
    1. API Key 是截断的密钥（包含 '...'）
    2. API Key 是占位符（your_*、*_here、sk-xxx、changeme 等）

    口径统一走 is_placeholder_api_key，避免各处判断漂移。

    Args:
        api_key: 要检查的 API Key

    Returns:
        bool: 是否应该跳过更新
    """
    if not api_key:
        return False

    api_key = str(api_key).strip()

    # 截断的密钥（包含 '...'）+ 各类占位符
    return is_placeholder_api_key(api_key)


def has_valid_api_key(llm_config, providers_dict: Optional[dict] = None) -> bool:
    """
    判断 LLM 配置是否有有效的 API Key

    检查顺序（与 /api/config/llm/providers 接口逻辑一致）：
    1. 检查是否是本地服务（名字白名单或地址指向本机），本地服务不需要 API Key
    2. 检查模型配置中的 api_key
    3. 检查厂家配置中的 api_key（如果提供了 providers_dict）
    4. 检查环境变量中的 api_key

    Args:
        llm_config: LLM 配置对象（LLMConfig 或字典）
        providers_dict: 厂家配置字典 {provider_name: LLMProvider}，可选

    Returns:
        bool: 是否有有效的 API Key（与 has_api_key 字段逻辑一致）
    """
    # 获取 provider 和 api_key
    if hasattr(llm_config, 'provider'):
        provider = llm_config.provider
        model_api_key = llm_config.api_key
    else:
        provider = llm_config.get('provider', '')
        model_api_key = llm_config.get('api_key', '')

    # 🔥 本地服务（ollama / lmstudio / oMLX / vLLM 等，或地址指向本机）不需要 API Key
    if is_local_service(provider):
        return True

    # 1. 检查模型配置中的 api_key
    if is_valid_api_key(model_api_key):
        return True

    # 2. 检查厂家配置中的 api_key（优先使用数据库配置）
    if providers_dict and provider in providers_dict:
        provider_obj = providers_dict[provider]
        # 🔥 检查厂家是否是本地服务（名字白名单或地址指向本机）
        if hasattr(provider_obj, 'name'):
            provider_name = provider_obj.name
            provider_base_url = getattr(provider_obj, 'default_base_url', '')
        else:
            provider_name = provider_obj.get('name', '')
            provider_base_url = provider_obj.get('default_base_url', '')
        if is_local_service(provider_name, provider_base_url):
            return True
        
        provider_api_key = provider_obj.api_key if hasattr(provider_obj, 'api_key') else provider_obj.get('api_key', '')
        if is_valid_api_key(provider_api_key):
            return True

    # 3. 检查环境变量中的 api_key（与 /api/config/llm/providers 逻辑一致）
    env_api_key = get_env_api_key_for_provider(provider)
    if env_api_key:
        return True

    return False

