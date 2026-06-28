from __future__ import annotations

import pandas as pd

from jqdata import *


def initialize(context):
    set_data_provider("tushare")
    set_benchmark("000300.XSHG")
    set_option("use_real_price", True)
    set_option("avoid_future_data", True)

    set_slippage(FixedSlippage(0.0005))
    set_order_cost(
        OrderCost(
            open_tax=0,
            close_tax=0.001,
            open_commission=0.0002,
            close_commission=0.0002,
            min_commission=5,
        ),
        type="stock",
    )

    g.index_code = "000300.XSHG"
    g.lookback = 20
    g.top_n = 5
    g.min_breadth = 0.55
    g.last_rebalance_date = None

    run_weekly(rebalance, 1, time="9:30")


def _load_price_frame(stocks, end_date, count: int) -> pd.DataFrame:
    if isinstance(stocks, str):
        stocks = [stocks]
    df = get_price(
        list(stocks),
        end_date=end_date,
        count=count,
        frequency="daily",
        fields=["close"],
        panel=False,
        fq=None,
    )
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.copy()
    if "code" in out.columns:
        out["code"] = out["code"].astype(str)
    if "close" in out.columns:
        out["close"] = pd.to_numeric(out["close"], errors="coerce")
    if not isinstance(out.index, pd.DatetimeIndex):
        return pd.DataFrame()
    return out.dropna(subset=["close"])


def _build_momentum_table(frame: pd.DataFrame, lookback: int) -> pd.DataFrame:
    if frame.empty or "code" not in frame.columns:
        return pd.DataFrame()
    pivot = frame.pivot_table(index=frame.index, columns="code", values="close", aggfunc="last")
    if pivot.empty:
        return pd.DataFrame()
    pivot = pivot.sort_index().dropna(axis=1, how="all")
    if len(pivot) < lookback + 1:
        return pd.DataFrame()

    base = pivot.iloc[-(lookback + 1)]
    latest = pivot.iloc[-1]
    table = pd.DataFrame({"momentum": latest / base - 1.0})
    table = table.replace([pd.NA, pd.NaT, float("inf"), float("-inf")], pd.NA).dropna()
    return table


def rebalance(context):
    today = context.current_dt.date()
    if g.last_rebalance_date == today:
        return
    g.last_rebalance_date = today

    current_positions = list(context.portfolio.positions.keys())

    stocks = get_index_stocks(g.index_code, date=context.previous_date)
    if not stocks:
        for stock in current_positions:
            order_target_value(stock, 0)
        return

    frame = _load_price_frame(stocks, end_date=context.previous_date, count=g.lookback + 1)
    if frame.empty:
        for stock in current_positions:
            order_target_value(stock, 0)
        return

    momentum_table = _build_momentum_table(frame, g.lookback)
    if momentum_table.empty:
        for stock in current_positions:
            order_target_value(stock, 0)
        log.info("[调仓] 动量数据不足，空仓")
        return

    breadth = float((momentum_table["momentum"] > 0).mean())
    avg_momentum = float(momentum_table["momentum"].mean())
    if breadth < g.min_breadth or avg_momentum <= 0:
        for stock in current_positions:
            order_target_value(stock, 0)
        log.info("[调仓] 广度不足，空仓 breadth=%.2f avg_momentum=%.4f", breadth, avg_momentum)
        return

    ranked = momentum_table[momentum_table["momentum"] > 0].sort_values("momentum", ascending=False).head(g.top_n)
    if ranked.empty:
        for stock in current_positions:
            order_target_value(stock, 0)
        log.info("[调仓] 无正动量标的，空仓")
        return

    targets = list(ranked.index)
    target_set = set(targets)

    for stock in current_positions:
        if stock not in target_set:
            order_target_value(stock, 0)

    current_data = get_current_data()
    tradable = [stock for stock in targets if stock in current_data and not current_data[stock].paused]
    if not tradable:
        return

    per_value = context.portfolio.total_value * 0.98 / len(tradable)
    for stock in tradable:
        order_target_value(stock, per_value)

    log.info(
        "[调仓] breadth=%.2f avg_momentum=%.4f 选中: %s",
        breadth,
        avg_momentum,
        ", ".join([f"{code}:{row.momentum:.4f}" for code, row in ranked.iterrows()]),
    )
