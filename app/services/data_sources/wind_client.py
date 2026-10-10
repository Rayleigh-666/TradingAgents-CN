"""
Wind 万得 MCP 客户端封装

通过本地 MCP CLI（wind-mcp-skill 提供的 scripts/cli.mjs）调用 Wind 的 7 个 MCP 服务：
  stock_data / fund_data / index_data / bond_data / financial_docs / economic_data / analytics_data

设计要点：
- 每次调用 = 一个 node 子进程，串行执行（skill 契约要求并发 1）
- 成功回执：stdout 为 JSON 对象，业务数据在 content[0].text（JSON 字符串）
- 失败回执：stdout 为 {"ok": false, "code": "...", "message": "..."} 错误信封
- API Key 由 skill 自身管理，无需在本项目中配置

环境变量（均可在 .env 中覆盖）：
  WIND_MCP_SKILL_DIR   skill 目录，默认 ~/.workbuddy/skills/wind-mcp-skill
  WIND_MCP_NODE        node 可执行文件路径，默认自动探测
  WIND_MCP_TIMEOUT     单次调用超时秒数，默认 60
"""

import json
import logging
import os
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import pandas as pd

logger = logging.getLogger(__name__)

# 合法的 server_type（来自 wind-mcp-skill 契约）
VALID_SERVER_TYPES = {
    "stock_data",
    "fund_data",
    "index_data",
    "bond_data",
    "financial_docs",
    "economic_data",
    "analytics_data",
}

DEFAULT_SKILL_DIR = os.path.expanduser("~/.workbuddy/skills/wind-mcp-skill")


class WindMCPError(Exception):
    """Wind MCP 调用错误，code 为错误信封中的错误码"""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}")

    @property
    def is_auth_error(self) -> bool:
        return self.code == "AUTH_ERROR"

    @property
    def is_rate_limited(self) -> bool:
        return self.code == "RATE_LIMIT_ERROR"

    @property
    def is_backend_error(self) -> bool:
        return self.code == "backend_error"


def _detect_node() -> Optional[str]:
    """探测 node 可执行文件路径"""
    env_node = os.environ.get("WIND_MCP_NODE")
    if env_node and (Path(env_node).exists() or shutil.which(env_node)):
        return env_node
    # 常见的托管/本地 node 路径
    candidates = [
        "/Users/bowen/.workbuddy/binaries/node/versions/22.22.2-6/bin/node",
        "/Users/bowen/.local/node/bin/node",
        "/usr/local/bin/node",
        "/opt/homebrew/bin/node",
    ]
    for c in candidates:
        if Path(c).exists():
            return c
    return shutil.which("node")


class WindMCPClient:
    """Wind MCP CLI 的同步客户端（线程安全，串行调用）"""

    _instance: Optional["WindMCPClient"] = None
    _instance_lock = threading.Lock()

    def __init__(self):
        self.skill_dir = os.environ.get("WIND_MCP_SKILL_DIR", DEFAULT_SKILL_DIR)
        self.cli_path = os.path.join(self.skill_dir, "scripts", "cli.mjs")
        self.timeout = int(os.environ.get("WIND_MCP_TIMEOUT", "60"))
        self.node_path = _detect_node()
        # skill 契约要求并发 1：全局锁串行化所有调用
        self._call_lock = threading.Lock()

    @classmethod
    def get_instance(cls) -> "WindMCPClient":
        """单例入口（skill_dir/timeout 每次从环境变量读取，便于运行时调整）"""
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def is_available(self) -> bool:
        """静态可用性检查：CLI 文件与 node 均存在（不发起真实调用，不消耗积分）"""
        return bool(self.node_path) and os.path.isfile(self.cli_path)

    def _parse_stdout(self, stdout: str) -> Dict[str, Any]:
        """解析 CLI stdout：成功为数据对象，失败为 {"ok": false, ...} 错误信封"""
        stdout = (stdout or "").strip()
        if not stdout:
            raise WindMCPError("EMPTY_RESPONSE", "CLI 无输出")
        try:
            envelope = json.loads(stdout)
        except json.JSONDecodeError as e:
            raise WindMCPError("INVALID_JSON", f"CLI 输出无法解析: {stdout[:200]} ({e})")

        if envelope.get("ok") is False:
            raise WindMCPError(
                envelope.get("code", "UNKNOWN"),
                envelope.get("message", "未知错误"),
            )
        return envelope

    def _extract_payload(self, envelope: Dict[str, Any]) -> Dict[str, Any]:
        """从成功信封中提取 content[0].text 的业务 JSON"""
        content = envelope.get("content") or []
        if not content:
            raise WindMCPError("EMPTY_CONTENT", "返回无 content")
        text = content[0].get("text")
        if text is None:
            raise WindMCPError("EMPTY_TEXT", "content[0] 无 text 字段")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            # 非严格 JSON 的文本结果，原样包一层返回
            return {"raw_text": text}
        if isinstance(payload, dict) and payload.get("error"):
            # 业务层错误 {"data": null, "error": "..."}
            raise WindMCPError("backend_error", str(payload.get("error")))
        return payload if isinstance(payload, dict) else {"data": payload}

    def call(
        self,
        server_type: str,
        tool_name: str,
        params: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        调用一个 Wind MCP 工具，返回解析后的业务 payload（content[0].text 的 JSON dict）。

        Raises:
            WindMCPError: 任何失败场景（认证/限流/参数/网络/后端错误）
        """
        if server_type not in VALID_SERVER_TYPES:
            raise WindMCPError("ROUTE_ERROR", f"未知 server_type: {server_type}")
        if not self.is_available():
            raise WindMCPError("SETUP_ERROR", f"Wind MCP CLI 不可用: {self.cli_path}")

        args_json = json.dumps(params, ensure_ascii=False)
        # ⚠️ cli.mjs 必须以相对路径作为入口（配合 cwd=skill 目录），
        #    传绝对路径会导致 CLI 静默退出（rc=0 且无输出）
        cmd = [self.node_path, "scripts/cli.mjs", "call", server_type, tool_name, args_json]

        with self._call_lock:
            try:
                proc = subprocess.run(
                    cmd,
                    cwd=self.skill_dir,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout,
                )
            except subprocess.TimeoutExpired:
                raise WindMCPError("TIMEOUT", f"调用超时（>{self.timeout}s）: {tool_name}")
            except FileNotFoundError as e:
                raise WindMCPError("SETUP_ERROR", f"node 不可执行: {e}")

        if proc.returncode != 0 and not proc.stdout.strip():
            stderr = (proc.stderr or "").strip()[:500]
            raise WindMCPError("CLI_ERROR", f"CLI 进程失败 rc={proc.returncode}: {stderr}")

        envelope = self._parse_stdout(proc.stdout)
        return self._extract_payload(envelope)

    # ==================== 便捷方法 ====================

    def call_table(
        self,
        server_type: str,
        tool_name: str,
        params: Dict[str, Any],
    ) -> pd.DataFrame:
        """
        调用返回表格结构的工具，解析为 DataFrame。兼容两种实测形态：
        - 扁平: {"data": {"columns": [...], "rows": [...]}}        （行情、K线）
        - 嵌套: {"data": {"data": [{"columns": ...}, ...]}}        （fundamentals/technicals 等自然语言工具）
        多张嵌套表按序纵向拼接（列名不同时按列名对齐，缺列填 NaN）。
        """
        payload = self.call(server_type, tool_name, params)
        data = payload.get("data")
        if not isinstance(data, dict):
            return pd.DataFrame()

        tables: List[Dict[str, Any]] = []
        if isinstance(data.get("data"), list):
            # 嵌套形态：data.data 是表格列表
            for item in data["data"]:
                if isinstance(item, dict) and item.get("columns") and item.get("rows"):
                    tables.append(item)
        elif data.get("columns") and data.get("rows"):
            # 扁平形态
            tables.append(data)

        if not tables:
            return pd.DataFrame()

        frames = []
        for t in tables:
            columns = [c.get("name") for c in (t.get("columns") or [])]
            rows = t.get("rows") or []
            if columns and rows:
                frames.append(pd.DataFrame(rows, columns=columns))
        if not frames:
            return pd.DataFrame()
        return frames[0] if len(frames) == 1 else pd.concat(frames, ignore_index=True)

    def call_items(
        self,
        server_type: str,
        tool_name: str,
        params: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """
        调用返回 items 列表结构的工具（如新闻、公告）。
        返回结构: {"data": {"items": [{...}]}}
        """
        payload = self.call(server_type, tool_name, params)
        data = payload.get("data")
        if not isinstance(data, dict):
            return []
        items = data.get("items")
        return items if isinstance(items, list) else []


def get_wind_client() -> WindMCPClient:
    """模块级便捷入口"""
    return WindMCPClient.get_instance()
