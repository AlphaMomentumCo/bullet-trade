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
    g.short_lookback = 20
    g.long_lookback = 60
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


def _build_return_table(frame: pd.DataFrame, short_lookback: int, long_lookback: int) -> pd.DataFrame:
    if frame.empty or "code" not in frame.columns:
        return pd.DataFrame()
    pivot = frame.pivot_table(index=frame.index, columns="code", values="close", aggfunc="last")
    if pivot.empty:
        return pd.DataFrame()
    pivot = pivot.sort_index().dropna(axis=1, how="all")
    if len(pivot) < long_lookback + 1:
        return pd.DataFrame()

    short_base = pivot.iloc[-(short_lookback + 1)]
    long_base = pivot.iloc[-(long_lookback + 1)]
    latest = pivot.iloc[-1]

    table = pd.DataFrame(
        {
            "short_ret": latest / short_base - 1.0,
            "long_ret": latest / long_base - 1.0,
        }
    )
    table = table.replace([pd.NA, pd.NaT, float("inf"), float("-inf")], pd.NA).dropna()
    return table


def _score_candidates(return_table: pd.DataFrame) -> pd.DataFrame:
    if return_table.empty:
        return return_table
    scores = return_table.copy()
    scores["score"] = 0.7 * scores["short_ret"] + 0.3 * scores["long_ret"]
    scores = scores[(scores["short_ret"] > 0) & (scores["long_ret"] > 0)]
    return scores.sort_values("score", ascending=False)


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

    frame = _load_price_frame(stocks, end_date=context.previous_date, count=g.long_lookback + 1)
    if frame.empty:
        for stock in current_positions:
            order_target_value(stock, 0)
        return

    return_table = _build_return_table(frame, g.short_lookback, g.long_lookback)
    if return_table.empty:
        for stock in current_positions:
            order_target_value(stock, 0)
        log.info("[调仓] 数据不足，空仓")
        return

    breadth = float((return_table["short_ret"] > 0).mean())
    avg_short = float(return_table["short_ret"].mean())
    avg_long = float(return_table["long_ret"].mean())
    if breadth < g.min_breadth or avg_short <= 0 or avg_long <= 0:
        for stock in current_positions:
            order_target_value(stock, 0)
        log.info(
            "[调仓] 广度不足，空仓 breadth=%.2f avg_short=%.4f avg_long=%.4f",
            breadth,
            avg_short,
            avg_long,
        )
        return

    scored = _score_candidates(return_table)
    if scored.empty:
        for stock in current_positions:
            order_target_value(stock, 0)
        log.info("[调仓] 没有满足双动量条件的标的，空仓")
        return

    ranked = scored.head(g.top_n)
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
        "[调仓] breadth=%.2f avg_short=%.4f avg_long=%.4f 选中: %s",
        breadth,
        avg_short,
        avg_long,
        ", ".join([f"{code}:{row.score:.4f}" for code, row in ranked.iterrows()]),
    )
