from __future__ import annotations

from jqdata import *  # noqa: F401,F403

from bullet_trade.data.providers import jqdata as _jqdata_provider

query = _jqdata_provider.query
valuation = _jqdata_provider.jq.valuation


def initialize(context):
    set_benchmark("000300.XSHG")
    set_option("use_real_price", True)
    set_option("avoid_future_data", True)
    set_option("order_volume_ratio", 1)

    set_order_cost(
        OrderCost(
            open_tax=0,
            close_tax=0.001,
            open_commission=0.0003,
            close_commission=0.0003,
            min_commission=5,
        ),
        type="stock",
    )

    g.stocknum = 3
    g.days = 0
    g.refresh_rate = 5

    run_daily(trade, time="open")


def check_stocks(context):
    q = (
        query(valuation.code, valuation.market_cap)
        .filter(valuation.market_cap.between(20, 30))
        .order_by(valuation.market_cap.asc())
    )
    df = get_fundamentals(q)
    if df is None or df.empty:
        return []

    buylist = list(df["code"])
    buylist = filter_paused_stock(buylist)
    return buylist[: g.stocknum]


def trade(context):
    if g.days % g.refresh_rate == 0:
        sell_list = list(context.portfolio.positions.keys())

        if len(sell_list) > 0:
            for stock in sell_list:
                order_target_value(stock, 0)

        if len(context.portfolio.positions) < g.stocknum:
            num = g.stocknum - len(context.portfolio.positions)
            cash = context.portfolio.available_cash / num if num > 0 else 0
        else:
            cash = 0

        stock_list = check_stocks(context)
        for stock in stock_list:
            if len(context.portfolio.positions) < g.stocknum:
                order_value(stock, cash)

        g.days = 1
    else:
        g.days += 1


def filter_paused_stock(stock_list):
    current_data = get_current_data()
    return [stock for stock in stock_list if not current_data[stock].paused]
