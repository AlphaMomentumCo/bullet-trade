from __future__ import annotations

from jqdata import *  # noqa: F401,F403


def initialize(context):
    set_data_provider("tushare")
    set_benchmark("000300.XSHG")
    set_option("use_real_price", True)
    set_option("avoid_future_data", True)

    g.index_code = "000300.XSHG"
    g.lookback = 20
    g.top_n = 5

    run_weekly(rebalance, 1, time="9:30")


def rebalance(context):
    stocks = get_index_stocks(g.index_code, date=context.previous_date)
    if not stocks:
        return

    price_df = get_price(
        stocks,
        end_date=context.previous_date,
        count=g.lookback + 1,
        frequency="daily",
        fields=["close"],
        panel=False,
        fq="pre",
    )
    if price_df is None or price_df.empty:
        return

    momentum = {}
    for code, group in price_df.groupby("code"):
        closes = group["close"].dropna()
        if len(closes) < g.lookback + 1:
            continue
        momentum[code] = float(closes.iloc[-1] / closes.iloc[0] - 1.0)

    if not momentum:
        return

    targets = [code for code, _ in sorted(momentum.items(), key=lambda x: x[1], reverse=True)[: g.top_n]]
    current_positions = list(context.portfolio.positions.keys())

    for stock in current_positions:
        if stock not in targets:
            order_target_value(stock, 0)

    per_value = context.portfolio.total_value / len(targets)
    current_data = get_current_data()
    for stock in targets:
        if current_data[stock].paused:
            continue
        order_target_value(stock, per_value)
