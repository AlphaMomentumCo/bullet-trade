# -*- coding: utf-8 -*-
"""打板策略剖析包装：收紧规模、宇宙早停、补齐 set_universe。"""

import 打板策略 as _orig
from 打板策略 import *  # noqa: F401,F403

# 补齐 jqdata 兼容 API（部分环境未注入）
try:
    set_universe  # type: ignore[name-defined]
except NameError:

    def set_universe(security_list):  # noqa: F811
        return None


_orig.set_universe = set_universe
globals()["set_universe"] = set_universe

_orig.CONFIG["max_universe_size"] = 200
_orig.CONFIG["max_watchlist_size"] = 80
_orig.CONFIG["entry_start"] = "09:30"
CONFIG = _orig.CONFIG


def _resolve_universe(context):
    """与原版相同过滤，凑满 max_universe_size 即停。"""
    current_data = get_current_data()
    stock_df = pd.DataFrame()
    try:
        stock_df = get_all_securities(["stock"], date=context.previous_date)
    except Exception as exc:
        log.info("get_all_securities 不可用，降级使用指数成分股: %s", exc)

    symbols = []
    if stock_df is not None and not stock_df.empty:
        symbols = stock_df.index.tolist()
    if not symbols:
        try:
            symbols = get_index_stocks(CONFIG["benchmark"], date=context.previous_date) or []
        except Exception:
            symbols = []

    universe = []
    limit = int(CONFIG["max_universe_size"])
    for symbol in symbols:
        if len(universe) >= limit:
            break
        try:
            cd = current_data[symbol]
        except Exception:
            continue
        if cd.paused or getattr(cd, "is_st", False):
            continue
        name = ""
        if stock_df is not None and not stock_df.empty and symbol in stock_df.index:
            row = stock_df.loc[symbol]
            if hasattr(row, "get"):
                name = row.get("display_name", "") or row.get("name", "") or ""
        if not name:
            name = getattr(cd, "name", "") or ""
        if "ST" in name or "*" in name or "退" in name:
            continue
        if symbol.startswith("688") or symbol.startswith("8"):
            continue
        universe.append(symbol)
    return universe


def before_trading_start(context):
    g.last_trade_day = context.current_dt.date()
    g.daily_open_count = 0
    g.universe = _resolve_universe(context)
    set_universe(g.universe)
    g.daily_watchlist = _build_daily_watchlist(context, g.universe)
    log.info("watchlist size: %s", len(g.daily_watchlist))


# 同步补丁到原模块，避免旧 before_trading_start 被误用
_orig._resolve_universe = _resolve_universe
_orig.before_trading_start = before_trading_start
_orig.CONFIG = CONFIG
