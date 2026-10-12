"""
跨模块共享的证券代码格式常量。

美股「交易所/板块」后缀集合（`US_EXCH_SUFFIXES`）原先在四处各写了一份字面量副本，
只靠注释互相提醒「改动需同步」，任一处漏改都会造成静默失配：路由侧把 `AAPL.O`
认成美股并剥成 `AAPL`，而预热侧若仍把 `.O` 当成代码本体就会写错 symbol、命中不了
缓存。现已收敛到本模块，四处统一引用：

- `app/worker/hk_us_preheat_service.py`   从 Wind 代码剥后缀（`BRK_B.N` → `BRK.B`）
- `app/routers/stocks.py`                标准化用户输入（`AAPL.O` → `AAPL`）
- `app/routers/paper.py`                 同上（模拟交易下单）
- `tradingagents/utils/stock_utils.py`   分析链路的市场识别

由 `tests/test_hk_us_preheat_codes.py` 断言四处引用的是同一集合，防止再次漂移。

本模块刻意保持零依赖（只 import 标准库），因为它会被 `tradingagents` 侧导入，
而 `tradingagents` 的导入链较重（会初始化 ConfigManager）。
"""

# 只认这些**已知**后缀，避免把股份类别的点号（`BRK.B` 里的 `B`）误当后缀剥掉。
# 实测本标的池 517 只美股实际只用到 {O(纳斯达克), N(纽交所), BAT(Cboe BZX)}，
# 其余为容错保留。**刻意不收单字母 A/B/C**——那正是股份类别的写法，
# 收了会把 `BRK.A` 剥成 `BRK`（Yahoo 的 `BRK` 是 A 类行情）。
US_EXCH_SUFFIXES = frozenset({
    "O", "N", "BAT", "OO", "NQ", "OB", "OQ", "PK", "AM", "GI", "HI",
})
