"""大盘报告缓存身份对流程级参数（include_margin / lookback_days）的区分测试。

背景：原缓存键只有日期，导致同一天不同工作流 / 不同 include_margin /
lookback_days 复用同一份报告，使这两个新接通的流程参数形同虚设。
本文件验证新的缓存身份构造会把这两个参数纳入缓存符号。
"""

from unittest.mock import Mock

EFFECTIVE = "2026-04-20"
REQUESTED = "2026-04-21"
BASE_SYMBOL = "market_v2__req_20260421__eff_20260420"


def _patch(monkeypatch):
    from core.agents.adapters import index_analyst_v2 as module

    monkeypatch.setattr(
        module, "apply_stable_data_cutoff", lambda date_str, cutoff_hour=19: EFFECTIVE
    )
    return module


def test_include_margin_false_differs_from_true_and_none(monkeypatch):
    module = _patch(monkeypatch)

    sym_false = module._build_index_cache_identity(REQUESTED, include_margin=False)[0]
    sym_true = module._build_index_cache_identity(REQUESTED, include_margin=True)[0]
    sym_none = module._build_index_cache_identity(REQUESTED, include_margin=None)[0]

    # False 单独成键，且与 True/None 都不同
    assert sym_false == f"{BASE_SYMBOL}__nomargin"
    assert sym_true == BASE_SYMBOL
    assert sym_none == BASE_SYMBOL
    assert sym_false != sym_true
    assert sym_false != sym_none


def test_lookback_days_differs(monkeypatch):
    module = _patch(monkeypatch)

    sym_20 = module._build_index_cache_identity(REQUESTED, lookback_days=20)[0]
    sym_60 = module._build_index_cache_identity(REQUESTED, lookback_days=60)[0]

    assert sym_20 == f"{BASE_SYMBOL}__lb20"
    assert sym_60 == f"{BASE_SYMBOL}__lb60"
    assert sym_20 != sym_60


def test_same_params_produce_same_symbol(monkeypatch):
    module = _patch(monkeypatch)

    a = module._build_index_cache_identity(
        REQUESTED, include_margin=False, lookback_days=30
    )
    b = module._build_index_cache_identity(
        REQUESTED, include_margin=False, lookback_days=30
    )

    assert a == b
    assert a[0] == f"{BASE_SYMBOL}__nomargin__lb30"


def test_default_params_backward_compatible(monkeypatch):
    module = _patch(monkeypatch)

    symbol, trade_date, effective_date = module._build_index_cache_identity(REQUESTED)

    assert symbol == BASE_SYMBOL
    assert trade_date == REQUESTED
    assert effective_date == EFFECTIVE


def test_get_cached_index_report_passes_param_suffix_in_symbol(monkeypatch):
    module = _patch(monkeypatch)

    cache = Mock()
    cache.find_cached_analysis_report.return_value = None
    monkeypatch.setattr(module, "_get_cache_manager", lambda: cache)

    module._get_cached_index_report(
        REQUESTED, include_margin=False, lookback_days=45
    )

    kwargs = cache.find_cached_analysis_report.call_args.kwargs
    assert kwargs["symbol"] == f"{BASE_SYMBOL}__nomargin__lb45"
    assert kwargs["trade_date"] == REQUESTED
    assert kwargs["report_type"] == "index_report"
    assert kwargs["max_age_hours"] == 1


def test_save_index_report_to_cache_passes_param_suffix_in_symbol(monkeypatch):
    module = _patch(monkeypatch)

    cache = Mock()
    monkeypatch.setattr(module, "_get_cache_manager", lambda: cache)

    saved = module._save_index_report_to_cache(
        REQUESTED, "x" * 120, include_margin=False, lookback_days=45
    )

    assert saved is True
    kwargs = cache.save_analysis_report.call_args.kwargs
    assert kwargs["symbol"] == f"{BASE_SYMBOL}__nomargin__lb45"
    assert kwargs["trade_date"] == REQUESTED
    assert kwargs["report_type"] == "index_report"
    assert kwargs["report_data"] == "x" * 120
