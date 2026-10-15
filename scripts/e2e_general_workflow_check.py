#!/usr/bin/env python3
"""通用研究工作流 端到端验证（跑在真实后端上，需后端已启动）。

覆盖四件事：
  1. 自由文本「分析目标」不再被当成股票代码
     （task_params 只写 market_type + task_category=general，不写 symbol/stock_code）
  2. 执行不再卡在参数校验（`缺少必需参数: symbol`）
     且 agent 不再报 `Missing required parameters: ticker`
  3. 运行中轮询 /api/analysis/tasks/{id}/status 全程 200
     —— 用**合成的 running 任务**（task_params.selected_analysts=None）精确复现旧 500 现场，
        不依赖竞态
  4. 结果里 news / social 两个维度真的出了数据（不是降级空壳、不是 cannot import name）

⚠️ 本脚本会**自助铸造 admin token**（用 JWT_SECRET 签一个 sub=admin 的短期令牌），
   且只连 127.0.0.1 上的本机后端 / Mongo —— 属**本机自测**工具，**不得**指向远端环境。

JWT_SECRET 不内置默认值：先取环境变量 JWT_SECRET，其次项目根 `.env`（本机配置，已 gitignore），
两者都取不到则直接报错退出（不会回退到任何内置密钥）。

可选环境变量 / 参数：
  BACKEND_URL        后端地址，默认 http://127.0.0.1:8001
  MONGO_URL          MongoDB 地址，默认 mongodb://localhost:27017
  JWT_SECRET         签名密钥；缺省时从项目根 .env 读取
  E2E_TARGET         自由文本分析目标，默认「A股市场当日盘面复盘」
  E2E_MARKET_TYPE    市场类型，默认 cn
  E2E_DEPTH          研究深度，默认 快速
  E2E_CUSTOM_JSON    额外 custom_params（JSON），如 '{"holdings":"..."}'
  E2E_EXPECT_REPORTS 期望存在的报告维度（逗号分隔），默认 news_report,sentiment_report
  POLL_TIMEOUT       轮询状态上限秒数，默认 420
  位置参数 [workflow_id]，默认 general_daily_market_review

用法：
  env -i HOME="$HOME" PATH="$PATH" LANG=zh_CN.UTF-8 .venv/bin/python \
      scripts/e2e_general_workflow_check.py [workflow_id]
"""
from __future__ import annotations

import copy
import json
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt
import requests
from pymongo import MongoClient

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ENV_FILE = _PROJECT_ROOT / ".env"

_JWT_SECRET_HELP = (
    "未找到 JWT_SECRET。请到项目根 .env 中取 JWT_SECRET=（本机配置，已在 .gitignore 内），"
    "或显式 `export JWT_SECRET=...` 后再运行。脚本不会回退到任何内置默认密钥。"
)


def _parse_env_file_for_key(path, key: str):
    """从 .env 文本里取 `KEY=...` 的**原始**值（找不到/读取失败返回 None）。

    规则（不追求完整 dotenv 语义，只服务本脚本）：
      - 忽略空行与以 # 开头的注释行；
      - 只匹配以 `KEY=` 开头的行（因此 `# KEY=...`、`KEY_EXTRA=...` 均不命中）；
      - 值 = 等号后到行尾的内容，strip 两端空白；
      - 若值两端是配对的单/双引号则剥掉；
      - **不做**任何转义 / URL 解码 / 变量展开（值里的 / 与 + 原样保留）。
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return None
    prefix = f"{key}="
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if not line.startswith(prefix):
            continue
        value = line[len(prefix):].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if value:
            return value
    return None


def _load_jwt_secret(env=None, env_file=None) -> str:
    """解析 JWT_SECRET：环境变量优先 → 项目根 .env → 都没有则 SystemExit(1)。

    纯函数，便于单测：``env`` 传入映射、``env_file`` 传入路径即可完全控制输入。
    """
    env = os.environ if env is None else env
    env_file = _ENV_FILE if env_file is None else env_file

    value = (env.get("JWT_SECRET") or "").strip()
    if value:
        return value

    value = _parse_env_file_for_key(env_file, "JWT_SECRET")
    if value:
        return value

    print(_JWT_SECRET_HELP, file=sys.stderr, flush=True)
    raise SystemExit(1)


BACKEND = os.environ.get("BACKEND_URL", "http://127.0.0.1:8001")
JWT_SECRET = _load_jwt_secret()
MONGO = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
DB_NAME = "tradingagents"
TASK_COLL = "unified_analysis_tasks"   # 任务实际落在该集合（不是 analysis_tasks）

WORKFLOW_ID = sys.argv[1] if len(sys.argv) > 1 else "general_daily_market_review"
TARGET = os.environ.get("E2E_TARGET", "A股市场当日盘面复盘")   # 自由文本分析目标，非股票代码
MARKET_TYPE = os.environ.get("E2E_MARKET_TYPE", "cn")
POLL_TIMEOUT = int(os.environ.get("POLL_TIMEOUT", "420"))

# 额外自定义参数（JSON），例如流程 4 必填的 holdings：
#   E2E_CUSTOM_JSON='{"holdings":"600519 贵州茅台 30%\n000858 五粮液 20%"}'
EXTRA_CUSTOM: dict = json.loads(os.environ.get("E2E_CUSTOM_JSON") or "{}")

# 必须存在且非降级的报告维度（默认针对「每日市场复盘」流程）
EXPECT_REPORTS = [
    s.strip() for s in
    (os.environ.get("E2E_EXPECT_REPORTS") or "news_report,sentiment_report").split(",")
    if s.strip()
]

# 维度名 → 找 key 时用的关键词
REPORT_KEYWORDS = {
    "news_report": ("news", "新闻", "资讯"),
    "sentiment_report": ("sentiment", "social", "情绪", "舆情"),
    "valuation_report": ("valuation", "估值"),
    "fundamentals_report": ("fundamental", "基本面"),
    "market_report": ("market", "技术", "市场"),
    "sector_report": ("sector", "板块"),
    "index_report": ("index", "指数", "大盘"),
}

# 降级/失败的标志性文案：结果里出现即视为「空壳成功」
DEGRADED_MARKERS = (
    "Missing required parameters",
    "工具执行或模型生成未完成",
    "cannot import name",
    "本节生成失败",
    "模块执行未完成",
)

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"{'✅' if ok else '❌'} {name}" + (f"  —— {detail}" if detail else ""), flush=True)


def unwrap(payload):
    """兼容 {data: X} / {success, data: {data: X}} 等包装层级。"""
    for _ in range(4):
        if isinstance(payload, dict) and "data" in payload and isinstance(payload["data"], (dict, list)):
            payload = payload["data"]
        else:
            break
    return payload


def mint_token() -> str:
    exp = datetime.now(timezone.utc) + timedelta(hours=2)
    return jwt.encode({"sub": "admin", "exp": exp}, JWT_SECRET, algorithm="HS256")


def status_regression_probe(s, db) -> None:
    """确定性复现旧 500：合成一条 running 且 selected_analysts=None 的任务，查状态接口。

    旧代码链条：analysis.py `task_params.get("selected_analysts") or []`（None 时默认值失效）
      → RedisProgressTracker(analysts=None) → tracker.py `len(self.analysts)` → TypeError → 500
    仅在「任务进行中」分支触发（要求 start_time 有值、end_time 为空）。
    """
    src = db[TASK_COLL].find_one({"task_params.task_category": "general"}, sort=[("created_at", -1)])
    if not src:
        check("合成 running 任务复现 500 回归", False, "库中没有 general 任务可作模板")
        return

    probe_id = f"e2e-probe-{uuid.uuid4()}"
    probe = copy.deepcopy(src)
    probe.pop("_id", None)
    probe["task_id"] = probe_id
    probe["status"] = "running"
    probe["progress"] = 50
    probe["started_at"] = datetime.now(timezone.utc)
    for k in ("end_time", "completed_at", "error"):
        probe.pop(k, None)
    tp = probe.setdefault("task_params", {})
    tp["selected_analysts"] = None          # ← 旧 bug 的精确触发条件
    tp.pop("end_time", None)
    db[TASK_COLL].insert_one(probe)

    try:
        rs = s.get(f"{BACKEND}/api/analysis/tasks/{probe_id}/status", timeout=30)
        ok = rs.status_code == 200
        detail = f"code={rs.status_code}"
        if ok:
            body = unwrap(rs.json()) or {}
            # 证明真的走进了「任务进行中」+ tracker 分支（会算预估总时长与剩余时间）
            est = body.get("estimated_total_time")
            rem = body.get("remaining_time")
            reached = est is not None or rem is not None
            detail = f"code=200 estimated_total_time={est} remaining_time={rem} status={body.get('status')}"
            check("复现路径已命中（tracker 分支真实执行）", reached, detail)
        else:
            detail = f"code={rs.status_code} body={rs.text[:200]}"
        check("selected_analysts=None 时状态接口仍 200（旧代码会 500）", ok, detail)
    finally:
        db[TASK_COLL].delete_one({"task_id": probe_id})


def main() -> int:
    s = requests.Session()
    s.trust_env = False          # 绕过沙箱 HTTP_PROXY
    s.headers["Authorization"] = f"Bearer {mint_token()}"

    db = MongoClient(MONGO)[DB_NAME]

    # ---------- 0. 工作流定义 ----------
    wf = db.workflows.find_one({"_id": WORKFLOW_ID}, {"name": 1, "workflow_type": 1, "input_config": 1})
    check("工作流存在且为 general", bool(wf) and wf.get("workflow_type") == "general",
          f"name={wf.get('name') if wf else None}")
    if not wf:
        return 1
    fields = (wf.get("input_config") or {}).get("fields") or []
    keys = [f.get("name") for f in fields]
    check("input_config 含 market_type 字段", "market_type" in keys, f"keys={keys}")

    # ---------- 1. 执行（复刻前端真实载荷） ----------
    # frontend/src/views/Analysis/GeneralAnalysis.vue:1016-1034：
    #   自由文本走 custom_params.analysis_target，无 ticker；market_type 走 custom_params
    payload = {
        "research_depth": os.environ.get("E2E_DEPTH", "快速"),
        "custom_params": {
            "analysis_target": TARGET,
            "market_type": MARKET_TYPE,
            **EXTRA_CUSTOM,
        },
    }
    r = s.post(f"{BACKEND}/api/workflows/{WORKFLOW_ID}/execute", json=payload, timeout=60)
    check("执行接口 HTTP 200", r.status_code == 200, f"code={r.status_code}")
    body = r.json()
    task_id = (body.get("data") or {}).get("task_id") or body.get("task_id")
    check("响应带 task_id", bool(task_id), f"task_id={task_id}")
    if not task_id:
        print("响应体:", json.dumps(body, ensure_ascii=False)[:600])
        return 1

    # ---------- 2. task_params 语义 ----------
    time.sleep(1.5)
    doc = db[TASK_COLL].find_one({"task_id": task_id}, {"task_params": 1, "status": 1})
    tp = (doc or {}).get("task_params") or {}
    check("task_category == general", tp.get("task_category") == "general",
          f"task_category={tp.get('task_category')!r}")
    check("task_params.market_type == cn", tp.get("market_type") == "cn",
          f"market_type={tp.get('market_type')!r}")
    check("自由文本未被写成 symbol", not tp.get("symbol") and not tp.get("stock_code"),
          f"symbol={tp.get('symbol')!r} stock_code={tp.get('stock_code')!r}")

    # ---------- 3. 轮询状态接口 ----------
    print(f"\n⏳ 轮询 /api/analysis/tasks/{task_id}/status（上限 {POLL_TIMEOUT}s）…", flush=True)
    t0 = time.time()
    codes: list[int] = []
    status_samples: list[dict] = []
    final_status = None

    while time.time() - t0 < POLL_TIMEOUT:
        try:
            rs = s.get(f"{BACKEND}/api/analysis/tasks/{task_id}/status", timeout=30)
            codes.append(rs.status_code)
            if rs.status_code == 200:
                sd = unwrap(rs.json()) or {}
                st = sd.get("status")
                status_samples.append({"t": round(time.time() - t0, 1), "status": st,
                                       "progress": sd.get("progress")})
                final_status = st
                if st in ("completed", "failed", "cancelled", "error"):
                    print(f"   ↳ 终态 status={st} @ t={round(time.time()-t0,1)}s", flush=True)
                    break
        except Exception as e:  # noqa: BLE001
            codes.append(-1)
            print(f"   ⚠ 轮询异常: {e}", flush=True)
        time.sleep(2)

    bad = [c for c in codes if c != 200]
    check("真实任务轮询全程无 5xx", not bad,
          f"共 {len(codes)} 次，码={sorted(set(codes))}" + (f"，异常={bad[:5]}" if bad else ""))
    check("任务到达终态", final_status in ("completed", "failed", "cancelled", "error"),
          f"final_status={final_status}")

    # ---------- 4. 确定性复现旧 500 现场 ----------
    print("\n🧪 合成 running 任务，复现 selected_analysts=None 的旧 500 现场…", flush=True)
    status_regression_probe(s, db)

    # ---------- 5. 结果内容 ----------
    rr = s.get(f"{BACKEND}/api/analysis/tasks/{task_id}/result", timeout=60)
    check("结果接口 HTTP 200", rr.status_code == 200, f"code={rr.status_code}")
    res = unwrap(rr.json()) or {}
    reports = res.get("reports") or (res.get("result") or {}).get("reports") or {}
    if not reports:
        print("   （响应里没找到 reports，改读数据库）")
        dbres = (db[TASK_COLL].find_one({"task_id": task_id}, {"result": 1}) or {}).get("result") or {}
        reports = dbres.get("reports") or {}
        res = dbres
    rtext = json.dumps(reports, ensure_ascii=False)
    print(f"\n📄 reports 维度: {list(reports.keys())}  (总长 {len(rtext)} 字符)")

    def find_key(*needles):
        for k in reports:
            if any(n in k.lower() for n in needles):
                return k
        return None

    for want in EXPECT_REPORTS:
        kw = REPORT_KEYWORDS.get(want, (want.rstrip("_report"),))
        key = find_key(*kw)
        check(f"结果含维度 {want}", bool(key), f"key={key}")
        if not key:
            continue
        text = json.dumps(reports.get(key, ""), ensure_ascii=False)
        check(f"{want} 非降级空壳", not any(m in text for m in DEGRADED_MARKERS),
              f"长度 {len(text)}")
        check(f"{want} 有实质内容（>200 字符）", len(text) > 200, f"长度 {len(text)}")
        if want == "sentiment_report":
            for k2 in ("Reddit", "未配置", "中文", "情绪"):
                if k2 in text:
                    print(f"     · 情绪文本含关键词「{k2}」")

    # 全局：任一维度出现降级标志即算失败
    check("全部 reports 无降级/缺参标志", not any(m in rtext for m in DEGRADED_MARKERS),
          f"命中: {[m for m in DEGRADED_MARKERS if m in rtext]}")

    # ---------- 6. 无占位名污染 ----------
    check("结果未写入占位 symbol", not (res.get("stock_symbol") or res.get("stock_code")),
          f"stock_symbol={res.get('stock_symbol')!r} stock_code={res.get('stock_code')!r}")

    # ---------- 汇总 ----------
    print("\n" + "=" * 72)
    print(f"task_id = {task_id}")
    print(f"状态采样 = {json.dumps(status_samples[:6], ensure_ascii=False)}")
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"通过 {passed}/{len(RESULTS)}")
    if failed:
        print("失败项: " + " | ".join(failed))
    print("=" * 72)

    out = {
        "task_id": task_id,
        "workflow_id": WORKFLOW_ID,
        "final_status": final_status,
        "result_keys": list(reports.keys()),
        "result_len": len(rtext),
        "checks": [{"name": n, "ok": ok, "detail": d} for n, ok, d in RESULTS],
    }
    with open("/tmp/e2e_general_workflow_result.json", "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2)
    print("明细已写入 /tmp/e2e_general_workflow_result.json")

    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
