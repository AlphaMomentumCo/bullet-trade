# -*- coding: utf-8 -*-
"""Tiny backtest stub: exercise get_current_data + get_price(count=1) every minute bar."""

from jqdata import *


def initialize(context):
    set_benchmark("000300.XSHG")
    set_option("use_real_price", True)
    g.universe = [
        "600519.XSHG",
        "000858.XSHE",
        "601318.XSHG",
        "000001.XSHE",
        "600036.XSHG",
    ]
    set_universe(g.universe)
    # bullet-trade 分钟回测：必须显式 every_bar，否则 handle_data 只在开盘触发一次
    run_daily(market_open, time="every_bar")


def market_open(context):
    current = get_current_data()
    for sec in g.universe:
        _ = current[sec].last_price
        get_price(
            sec,
            end_date=context.current_dt,
            frequency="daily",
            fields=["open", "close", "high", "low", "volume"],
            count=1,
            fq="pre",
        )


def handle_data(context, data):
    # 保留空实现，避免引擎再按开盘钩子重复调用
    pass
