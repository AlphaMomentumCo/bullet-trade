"""
基于 mini_demo 逻辑的指数成分股回测策略。

通过环境变量选择股票池：
  BT_UNIVERSE=hs300 | zz1000
成分列表读取本地已拉取的 constituents.txt（不再重复请求指数成分接口）。
选股仍走 get_fundamentals(市值 20-30 亿) ∩ 成分池，再过滤停牌，取最小市值 3 只。
"""

from __future__ import annotations

import os
from pathlib import Path

from jqdata import *
from bullet_trade.data.providers import jqdata as _jqdata_provider

query = _jqdata_provider.query
valuation = _jqdata_provider.jq.valuation

_ROOT = Path(__file__).resolve().parents[1]
_UNIVERSE_FILES = {
    "hs300": _ROOT / "my_results/index_daily/hs300_stocks_daily_1y/constituents.txt",
    "zz1000": _ROOT / "my_results/index_daily/zz1000_stocks_daily_1y/constituents.txt",
}


def _load_universe() -> list:
    key = (os.getenv("BT_UNIVERSE") or "hs300").strip().lower()
    path = _UNIVERSE_FILES.get(key)
    if path is None or not path.is_file():
        raise FileNotFoundError(f"未找到股票池文件 BT_UNIVERSE={key} path={path}")
    codes = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not codes:
        raise ValueError(f"股票池为空: {path}")
    return codes


def initialize(context):
    set_benchmark("000300.XSHG")
    set_option("use_real_price", True)
    set_option("order_volume_ratio", 1)
    set_order_cost(
        OrderCost(
            open_tax=0,
            close_tax=0.001,
            open_commission=0.0003,
            close_commission=0.0003,
            close_today_commission=0,
            min_commission=5,
        ),
        type="stock",
    )
    g.stocknum = 3
    g.days = 0
    g.refresh_rate = 5
    g.notified_trade_ids = set()
    g.universe = _load_universe()
    g.universe_name = (os.getenv("BT_UNIVERSE") or "hs300").strip().lower()
    log.info(f"universe={g.universe_name} size={len(g.universe)}")

    run_daily(trade, "open")
    run_daily(notify_trade_fills, "09:35")


def check_stocks(context):
    # 与 mini_demo 一致：全市场市值筛选（避免 code.in_(上千只) 导致接口极慢/卡住）
    # 再与本地成分池求交
    q = (
        query(valuation.code, valuation.market_cap)
        .filter(valuation.market_cap.between(20, 30))
        .order_by(valuation.market_cap.asc())
    )
    df = get_fundamentals(q)
    universe_set = set(g.universe)
    if df is None or len(df) == 0:
        buylist = list(g.universe)
    else:
        buylist = [code for code in df["code"].tolist() if code in universe_set]
        if not buylist:
            buylist = list(g.universe)
    # 按市值序扫描，凑够 stocknum 即可，避免对整池逐票 get_current_data
    return filter_paused_stock(buylist, limit=g.stocknum)


def trade(context):
    if g.days % g.refresh_rate != 0:
        g.days += 1
        return

    sell_list = list(context.portfolio.positions.keys())
    if sell_list:
        for stock in sell_list:
            order_target_value(stock, 0)

    if len(context.portfolio.positions) < g.stocknum:
        num = g.stocknum - len(context.portfolio.positions)
        cash = context.portfolio.available_cash / num if num > 0 else 0
    else:
        cash = 0

    stock_list = check_stocks(context)
    for stock in stock_list:
        if len(context.portfolio.positions.keys()) < g.stocknum:
            order_value(stock, cash)

    g.days = 1


def filter_paused_stock(stock_list, limit=None):
    current_data = get_current_data()
    out = []
    for stock in stock_list:
        if not current_data[stock].paused:
            out.append(stock)
            if limit is not None and len(out) >= limit:
                break
    return out


def notify_trade_fills(context):
    trades = get_trades()
    if not trades:
        return
    for trade_id, _trade in trades.items():
        if trade_id in g.notified_trade_ids:
            continue
        g.notified_trade_ids.add(trade_id)
