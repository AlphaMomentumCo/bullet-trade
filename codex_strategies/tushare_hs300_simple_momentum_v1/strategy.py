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
    g.last_rebalance_date = None

    run_weekly(rebalance, 1, time="9:30")


def _load_price_frame(stocks, end_date, count: int) -> pd.DataFrame:
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


def rebalance(context):
    today = context.current_dt.date()
    if g.last_rebalance_date == today:
        return
    g.last_rebalance_date = today

    stocks = get_index_stocks(g.index_code, date=context.previous_date)
    if not stocks:
        return

    frame = _load_price_frame(stocks, end_date=context.previous_date, count=g.lookback + 1)
    if frame.empty:
        return

    momentum = {}
    for code in stocks:
        sub = frame[frame["code"] == code] if "code" in frame.columns else frame
        if sub.empty:
            continue
        closes = pd.to_numeric(sub["close"], errors="coerce").dropna().tail(g.lookback + 1)
        if len(closes) < g.lookback + 1 or closes.iloc[0] <= 0:
            continue
        score = float(closes.iloc[-1] / closes.iloc[0] - 1.0)
        if score <= 0:
            continue
        momentum[code] = score

    if not momentum:
        return

    ranked = sorted(momentum.items(), key=lambda x: x[1], reverse=True)[: g.top_n]
    targets = [code for code, _ in ranked]
    current_positions = list(context.portfolio.positions.keys())
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

    log.info("[调仓] 选中: %s", ", ".join([f"{code}:{score:.4f}" for code, score in ranked]))
