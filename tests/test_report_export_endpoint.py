#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
报告导出下载端点回归测试。

回归背景
--------
v3.6.0 社区版分离时，导出下载端点原计划拆到 Pro 侧
（docs/05-design/v3.0/v3.6.0-community-edition-separation-plan.md §5.2），
`app/routers/reports.py` 只留下一句注释：

    # 导出下载端点（Pro 专属，export_reports 门控）已拆分至 reports_export.py

而 `app/routers/reports_export.py` **从未存在**（无 git 历史，`app/pro` 目录也不存在），
于是前端 `ReportDetail.vue::downloadReport()` 调用的
`GET /api/reports/{id}/download` 稳定返回 404 → 用户「无法下载报告」。

修复口径
--------
在 `reports.py` 内提供社区版实现，支持 Markdown / PDF：
  - `format=markdown` → `text/markdown; charset=utf-8`（.md）
  - `format=pdf`      → `application/pdf`（引擎链见 app/utils/report_exporter.py）
报告文档加载口径与 `GET /{report_id}/detail` 保持一致（analysis_reports →
任务原始结果补齐 → 任务中心兜底还原）。

本用例纯函数级 / 路由级，不依赖 MongoDB、网络 or 无头 Chrome。
"""

import asyncio
import os
import sys
from unittest.mock import patch

# 支持 `python tests/test_report_export_endpoint.py` 直接运行（pytest 走 conftest.py）
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from fastapi import HTTPException  # noqa: E402

from app.routers import reports as reports_module  # noqa: E402
from app.routers.reports import (  # noqa: E402
    _ascii_fallback_filename,
    _build_export_filename,
    _load_report_doc_for_export,
    download_report,
)
from app.utils.report_exporter import report_exporter  # noqa: E402


# ==================== 测试替身：最小可用 async DB ====================

class _FakeCursor:
    def __init__(self, doc):
        self._doc = doc

    async def find_one(self, *args, **kwargs):
        return self._doc


class _FakeCollection:
    def __init__(self, docs=()):
        self._docs = list(docs)
        self.queries = []

    async def find_one(self, query=None, *args, **kwargs):
        self.queries.append(query)
        for doc in self._docs:
            for key, expected in (query or {}).items():
                if key == "$or":
                    if any(_match(doc, sub) for sub in expected):
                        return doc
                elif doc.get(key) != expected:
                    break
            else:
                return doc
        return None


class _FakeDB:
    def __init__(self, analysis_reports=(), one_time_tasks=(), legacy_tasks=()):
        self._collections = {
            "analysis_reports": _FakeCollection(analysis_reports),
            "unified_analysis_tasks": _FakeCollection(one_time_tasks),
            "analysis_tasks": _FakeCollection(legacy_tasks),
        }
        self.analysis_reports = self._collections["analysis_reports"]
        self.unified_analysis_tasks = self._collections["unified_analysis_tasks"]
        self.analysis_tasks = self._collections["analysis_tasks"]

    def __getitem__(self, name):
        return self._collections[name]


def _match(doc, query):
    for key, expected in query.items():
        if doc.get(key) != expected:
            return False
    return True


SAMPLE_REPORT = {
    "_id": "65f0c0ffee1234567890abcd",
    "analysis_id": "03669_20261013_133414",
    "task_id": "3c4fd27d-b6ff-427b-af35-08b885e43b59",
    "stock_symbol": "03669",
    "stock_name": "永达汽车",
    "analysis_date": "2026-10-13 00:00:00",
    "analysts": ["market_analyst", "fundamentals_analyst"],
    "research_depth": "全面",
    "summary": "这里是研究摘要。",
    "status": "completed",
    "reports": {
        "market_report": "## 核心观察\n\n市场技术研究内容。",
        "fundamentals_report": "## 基本面\n\n基本面研究内容。",
    },
}


# ==================== 路由注册 ====================

def test_download_route_is_registered():
    """核心回归：/api/reports/{report_id}/download 必须真实注册（历史上是 404）。"""
    paths = {}
    for route in reports_module.router.routes:
        paths.setdefault(route.path, set()).update(getattr(route, "methods", set()) or set())

    assert "/api/reports/{report_id}/download" in paths, (
        "导出端点缺失！前端 downloadReport() 会再次 404。"
        f"现有路由: {sorted(paths)}"
    )
    assert "GET" in paths["/api/reports/{report_id}/download"]
    print("✅ /api/reports/{report_id}/download 已注册（GET）")


def test_download_route_source_guard():
    """源码级守卫：导出端点必须留在社区版 reports.py（防止再次被「拆分」后丢失）。"""
    src_path = os.path.join(PROJECT_ROOT, "app", "routers", "reports.py")
    with open(src_path, "r", encoding="utf-8") as f:
        src = f.read()

    assert '@router.get("/{report_id}/download")' in src
    # 老的「已拆分到 reports_export.py」注释若还在，说明实现被误删
    assert "已拆分至 reports_export.py" not in src
    assert not os.path.exists(
        os.path.join(PROJECT_ROOT, "app", "routers", "reports_export.py")
    ), "若 Pro 版 reports_export.py 回归，社区版实现需相应调整（见 §5.2）"
    print("✅ 导出实现保留在社区版 reports.py 内")


# ==================== 文件名 ====================

def test_build_export_filename_normal():
    name = _build_export_filename(SAMPLE_REPORT, "md")
    assert name == "03669_永达汽车_分析报告_2026-10-13.md", name
    assert _build_export_filename(SAMPLE_REPORT, "pdf").endswith(".pdf")
    print("✅ 文件名：代码_名称_分析报告_日期.ext")


def test_build_export_filename_drops_placeholder_and_illegal_chars():
    # 占位名（后端脏数据形态）不应出现在文件名里
    doc = dict(SAMPLE_REPORT, stock_name="股票03669")
    assert _build_export_filename(doc, "md") == "03669_分析报告_2026-10-13.md"

    # 非法字符 / 空白被剔除
    doc = dict(SAMPLE_REPORT, stock_name="永达 汽车/集团:股份")
    assert _build_export_filename(doc, "md") == "03669_永达汽车集团股份_分析报告_2026-10-13.md"

    # 无名称、无日期也能出文件名
    doc = {"stock_symbol": "600519"}
    assert _build_export_filename(doc, "md") == "600519_分析报告.md"
    print("✅ 占位名被丢弃、非法字符被清理")


# ==================== 报告文档加载 ====================

def _run(coro):
    return asyncio.run(coro)


def test_load_doc_from_analysis_reports():
    db = _FakeDB(analysis_reports=[dict(SAMPLE_REPORT)])
    doc = _run(_load_report_doc_for_export(db, "03669_20261013_133414"))
    assert doc is not None
    assert doc["stock_symbol"] == "03669"
    assert "market_report" in doc["reports"]
    assert "fundamentals_report" in doc["reports"]
    print("✅ 命中 analysis_reports")


def test_load_doc_merges_richer_task_result():
    """任务原始结果更全时，应取更全的那份（与 /detail 一致）。"""
    task_result = {
        "analysis_id": "03669_20261013_133414",
        "reports": {
            "market_report": "## 核心观察\n\n市场技术研究内容。",
            "fundamentals_report": "## 基本面\n\n基本面研究内容。",
            "bear_researcher": "## 核心主张\n\n审慎情景论证内容。",
            "final_trade_decision": "## 综合研究结论\n\n结论内容。",
        },
    }
    db = _FakeDB(
        analysis_reports=[dict(SAMPLE_REPORT)],
        one_time_tasks=[{"task_id": "3c4fd27d-b6ff-427b-af35-08b885e43b59", "result": task_result}],
    )
    doc = _run(_load_report_doc_for_export(db, "03669_20261013_133414"))
    assert "bear_researcher" in doc["reports"], doc["reports"].keys()
    assert "final_trade_decision" in doc["reports"]
    print("✅ 用任务原始结果补齐缺失模块")


def test_load_doc_falls_back_to_task_center():
    """analysis_reports 没有时，应从任务中心还原一份可导出文档。"""
    task_result = {
        "analysis_id": "600519_20261013_090000",
        "stock_symbol": "600519",
        "stock_name": "贵州茅台",
        "analysis_date": "2026-10-13 00:00:00",
        "reports": {"market_report": "## 核心观察\n\n内容。"},
    }
    db = _FakeDB(
        analysis_reports=[],
        legacy_tasks=[{"task_id": "600519_20261013_090000", "result": task_result}],
    )
    doc = _run(_load_report_doc_for_export(db, "600519_20261013_090000"))
    assert doc is not None
    assert doc["stock_symbol"] == "600519"
    assert doc["reports"], "兜底文档必须带 reports"
    print("✅ 回退任务中心还原成功")


def test_load_doc_returns_none_when_absent():
    db = _FakeDB()
    assert _run(_load_report_doc_for_export(db, "not-exist")) is None
    print("✅ 报告不存在时返回 None")


# ==================== 端点行为 ====================

def _call_download(db, report_id, fmt):
    """直接调用端点函数（绕过依赖注入），db 通过 monkeypatch 注入。"""
    original = reports_module.get_mongo_db
    reports_module.get_mongo_db = lambda: db
    try:
        return _run(download_report(report_id, fmt, user={"id": "u1"}))
    finally:
        reports_module.get_mongo_db = original


def test_ascii_fallback_filename_is_header_safe():
    """回归：中文名直接进 filename="..." 会因 latin-1 编码抛 500。"""
    name = _build_export_filename(SAMPLE_REPORT, "md")
    assert name == "03669_永达汽车_分析报告_2026-10-13.md"

    fallback = _ascii_fallback_filename(name, "report.md")
    assert fallback == "03669_2026-10-13.md", fallback
    fallback.encode("latin-1")  # 必须可被 HTTP 头编码

    # 全中文 / 空名回退
    assert _ascii_fallback_filename("分析报告.md", "report.md") == "report.md"
    assert _ascii_fallback_filename("", "report.md") == "report.md"
    # 「代码_分析报告.md」（无名称无日期）不应出现 `_.` 这种丑陋组合
    assert _ascii_fallback_filename("03669_分析报告.md", "report.md") == "03669.md"
    print("✅ filename= 兜底名纯 ASCII，可被 latin-1 编码")


def test_endpoint_returns_markdown():
    db = _FakeDB(analysis_reports=[dict(SAMPLE_REPORT)])
    resp = _call_download(db, "03669_20261013_133414", "markdown")

    assert resp.status_code == 200
    assert resp.media_type == "text/markdown; charset=utf-8"
    body = resp.body.decode("utf-8")
    assert "永达汽车" in body, "导出标题应带股票名称"
    assert "03669" in body
    assert "市场技术研究内容" in body

    disposition = resp.headers["content-disposition"]
    assert disposition.startswith("attachment;")
    assert "filename*=UTF-8''" in disposition, "中文文件名需 RFC 5987 编码"
    # 回归：filename="..." 部分必须是纯 ASCII，否则 Starlette latin-1 编码会 500
    ascii_part = disposition.split('filename="', 1)[1].split('"', 1)[0]
    ascii_part.encode("latin-1")
    assert "永达汽车" not in ascii_part
    print("✅ markdown 端点：200 + text/markdown + 附件头（中文名走 filename*）")


def test_endpoint_accepts_md_alias():
    db = _FakeDB(analysis_reports=[dict(SAMPLE_REPORT)])
    resp = _call_download(db, "03669_20261013_133414", "MD")
    assert resp.status_code == 200
    assert resp.media_type.startswith("text/markdown")
    print("✅ format=md / 大小写不敏感")


def test_endpoint_returns_pdf():
    db = _FakeDB(analysis_reports=[dict(SAMPLE_REPORT)])
    fake_pdf = b"%PDF-1.4\n%fake\n"
    with patch.object(report_exporter, "generate_pdf_report", return_value=fake_pdf):
        resp = _call_download(db, "03669_20261013_133414", "pdf")

    assert resp.status_code == 200
    assert resp.media_type == "application/pdf"
    assert resp.body.startswith(b"%PDF-")
    assert ".pdf" in resp.headers["content-disposition"]
    print("✅ pdf 端点：200 + application/pdf")


def test_endpoint_rejects_unsupported_format():
    db = _FakeDB(analysis_reports=[dict(SAMPLE_REPORT)])
    for bad in ("docx", "json", "xlsx", "txt"):
        try:
            _call_download(db, "03669_20261013_133414", bad)
        except HTTPException as exc:
            assert exc.status_code == 400, (bad, exc.status_code)
        else:
            raise AssertionError(f"格式 {bad!r} 应被拒绝（400）")
    print("✅ 不支持的格式返回 400（docx / json / xlsx / txt）")


def test_endpoint_empty_format_falls_back_to_markdown():
    """`?format=` 空值走默认 markdown（与 Query 默认值一致）。"""
    db = _FakeDB(analysis_reports=[dict(SAMPLE_REPORT)])
    resp = _call_download(db, "03669_20261013_133414", "")
    assert resp.status_code == 200
    assert resp.media_type.startswith("text/markdown")
    print("✅ 空 format 回退 markdown")


def test_endpoint_returns_404_when_report_missing():
    db = _FakeDB()
    try:
        _call_download(db, "not-exist", "markdown")
    except HTTPException as exc:
        assert exc.status_code == 404, exc.status_code
    else:
        raise AssertionError("报告不存在应返回 404")
    print("✅ 报告不存在返回 404")


# ==================== 导出内容健壮性 ====================

def test_exporter_title_uses_stock_name():
    md = report_exporter.generate_markdown_report(dict(SAMPLE_REPORT))
    assert md.splitlines()[0] == "# 永达汽车（03669） 股票研究报告", md.splitlines()[0]

    # 占位名不进入标题
    md2 = report_exporter.generate_markdown_report(dict(SAMPLE_REPORT, stock_name="股票03669"))
    assert md2.splitlines()[0] == "# 03669 股票研究报告", md2.splitlines()[0]
    print("✅ 导出标题展示「名称（代码）」，占位名被忽略")


if __name__ == "__main__":
    test_download_route_is_registered()
    test_download_route_source_guard()
    test_build_export_filename_normal()
    test_build_export_filename_drops_placeholder_and_illegal_chars()
    test_ascii_fallback_filename_is_header_safe()
    test_load_doc_from_analysis_reports()
    test_load_doc_merges_richer_task_result()
    test_load_doc_falls_back_to_task_center()
    test_load_doc_returns_none_when_absent()
    test_endpoint_returns_markdown()
    test_endpoint_accepts_md_alias()
    test_endpoint_returns_pdf()
    test_endpoint_rejects_unsupported_format()
    test_endpoint_empty_format_falls_back_to_markdown()
    test_endpoint_returns_404_when_report_missing()
    test_exporter_title_uses_stock_name()
    print("\n🎉 全部通过")
