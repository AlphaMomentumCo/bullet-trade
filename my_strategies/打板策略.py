# -*- coding: utf-8 -*-
"""
JoinQuant version of the limit-up board strategy.

This file keeps the same broad idea as the QMT version:
- Build a daily watchlist from liquid stocks near 20-day highs.
- Look for intraday moves close to the upper limit.
- Prefer candidates with strong turnover expansion and fewer failed board attempts.
- Exit on failed board holding, stop loss, take profit, or time-based rules.

Designed for minute-level backtests / simulation in JoinQuant.
"""

import math
from jqdata import *


CONFIG = {
    "benchmark": "000300.XSHG",
    "max_positions": 2,
    "max_daily_openings": 1,
    "position_pct": 0.25,
    "min_price": 4.0,
    "max_price": 80.0,
    "min_daily_amount": 80000000,
    "min_minute_amount": 3000000,
    "min_20m_amount": 30000000,
    "min_intraday_gain": 0.035,
    "max_intraday_gain": 0.115,
    "near_limit_buffer": 0.012,
    "min_limit_attack_buffer": 0.003,
    "max_buy_to_limit_buffer": 0.02,
    "max_intraday_breaks": 1,
    "min_volume_ratio": 1.05,
    "max_volume_ratio": 8.0,
    "max_amount_burst": 8.0,
    "min_close_strength": 0.55,
    "min_breakout_ratio": 0.95,
    "min_recent_return5": 0.01,
    "max_recent_return5": 0.18,
    "score_threshold": 0.60,
    "same_day_stop_loss": 0.04,
    "next_day_stop_loss": 0.05,
    "next_day_take_profit": 0.04,
    "next_day_weak_open_exit": 0.02,
    "entry_start": "09:35",
    "entry_end": "14:45",
    "same_day_exit_time": "14:55",
    "next_day_force_exit": "14:40",
    "max_universe_size": 2000,
    "max_watchlist_size": 300,
}


def initialize(context):
    set_benchmark(CONFIG["benchmark"])
    set_option("use_real_price", True)
    set_order_cost(
        OrderCost(open_tax=0, close_tax=0.001, open_commission=0.0003, close_commission=0.0003, min_commission=5),
        type="stock",
    )

    g.entry_records = {}
    g.daily_watchlist = {}
    g.daily_open_count = 0
    g.last_trade_day = None
    g.universe = []
    g.closed_trades = []
    g.last_analysis_log_day = None
    g.pending_exits = {}


def before_trading_start(context):
    g.last_trade_day = context.current_dt.date()
    g.daily_open_count = 0
    g.universe = _resolve_universe(context)
    set_universe(g.universe)
    g.daily_watchlist = _build_daily_watchlist(context, g.universe)
    log.info("watchlist size: %s", len(g.daily_watchlist))


def handle_data(context, data):
    now = context.current_dt
    if not _is_trade_session(now):
        return

    _sync_positions_from_portfolio(context)
    _manage_positions(context, data, now)
    if _in_window(now, "14:59", "14:59"):
        _log_analysis_snapshot(context)

    if not _in_window(now, CONFIG["entry_start"], CONFIG["entry_end"]):
        return
    live_count = _live_position_count(context)
    if live_count >= CONFIG["max_positions"]:
        return
    if g.daily_open_count >= CONFIG["max_daily_openings"]:
        return
    if not g.daily_watchlist:
        return

    candidates = _scan_candidates(context, data)
    slots = CONFIG["max_positions"] - live_count
    for candidate in candidates[:slots]:
        if candidate["score"] < CONFIG["score_threshold"]:
            break
        _open_position(context, candidate)


def _resolve_universe(context):
    current_data = get_current_data()
    stock_df = pd.DataFrame()
    try:
        stock_df = get_all_securities(["stock"], date=context.previous_date)
    except Exception as exc:
        # qmt/miniqmt 回测不支持 get_all_securities 历史视角，降级到指数成分股
        log.info("get_all_securities 不可用，降级使用指数成分股: %s", exc)

    symbols = []
    if stock_df is not None and not stock_df.empty:
        symbols = stock_df.index.tolist()
    if not symbols:
        # QMT 场景下 get_all_securities 可能返回空，回退到基准指数成分股
        try:
            symbols = get_index_stocks(CONFIG["benchmark"], date=context.previous_date) or []
        except Exception:
            symbols = []

    universe = []

    for symbol in symbols:
        try:
            cd = current_data[symbol]
        except Exception:
            continue

        if cd.paused or cd.is_st:
            continue

        # QMT current_data 通常不带 name，优先从 securities 表取 display_name/name
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

    return universe[: CONFIG["max_universe_size"]]


def _build_daily_watchlist(context, universe):
    ranked = []
    if not universe:
        return {}

    price_panel = get_price(
        universe,
        end_date=context.previous_date,
        frequency="daily",
        fields=["close", "high", "money"],
        count=25,
        skip_paused=True,
        fq="pre",
        panel=False,
    )

    if price_panel is None or price_panel.empty:
        return {}

    for symbol in universe:
        stock_df = price_panel[price_panel["code"] == symbol].sort_index()
        if len(stock_df) < 22:
            continue

        closes = stock_df["close"].tolist()
        highs = stock_df["high"].tolist()
        amounts = stock_df["money"].tolist()
        prev_close = closes[-1]
        avg_amount5 = _mean(amounts[-5:])
        high20 = max(highs[-20:])
        breakout_ratio = prev_close / max(high20, 0.01)
        recent_return5 = prev_close / max(closes[-6], 0.01) - 1.0
        recent_return2 = prev_close / max(closes[-3], 0.01) - 1.0

        if prev_close < CONFIG["min_price"] or prev_close > CONFIG["max_price"]:
            continue
        if avg_amount5 < CONFIG["min_daily_amount"]:
            continue
        if breakout_ratio < CONFIG["min_breakout_ratio"]:
            continue
        if recent_return5 < CONFIG["min_recent_return5"] or recent_return5 > CONFIG["max_recent_return5"]:
            continue
        if recent_return2 > 0.12:
            continue

        ranked.append(
            (
                avg_amount5,
                symbol,
                {
                    "prev_close": prev_close,
                    "avg_amount5": avg_amount5,
                    "high20": high20,
                    "breakout_ratio": breakout_ratio,
                    "recent_return5": recent_return5,
                    "recent_return2": recent_return2,
                },
            )
        )

    ranked.sort(key=lambda item: item[0], reverse=True)
    return {symbol: meta for _, symbol, meta in ranked[: CONFIG["max_watchlist_size"]]}


def _scan_candidates(context, data):
    candidates = []
    current_data = get_current_data()
    stats = {
        "watchlist": len(g.daily_watchlist),
        "data_ready": 0,
        "bar_missing": 0,
        "minute_amount": 0,
        "gain": 0,
        "near_limit": 0,
        "touch_limit_zone": 0,
        "tradable_gap": 0,
        "history": 0,
        "breaks": 0,
        "volume": 0,
        "passed": 0,
    }

    for symbol, meta in g.daily_watchlist.items():
        if symbol in context.portfolio.positions:
            continue

        try:
            bar = data[symbol]
        except Exception:
            stats["bar_missing"] += 1
            continue

        if getattr(bar, "paused", False):
            continue
        stats["data_ready"] += 1

        last_price = _safe_float(bar.close)
        high_price = _safe_float(bar.high)
        low_price = _safe_float(bar.low)
        amount = _safe_float(getattr(bar, "money", 0))
        limit_price = _safe_float(current_data[symbol].high_limit)

        if limit_price <= 0 or last_price <= 0:
            continue
        if amount < CONFIG["min_minute_amount"]:
            continue
        stats["minute_amount"] += 1

        intraday_gain = last_price / max(meta["prev_close"], 0.01) - 1.0
        if intraday_gain < CONFIG["min_intraday_gain"]:
            continue
        if intraday_gain > CONFIG["max_intraday_gain"]:
            continue
        stats["gain"] += 1

        touched_limit_zone = high_price >= limit_price * (1.0 - CONFIG["near_limit_buffer"])
        if not touched_limit_zone:
            continue
        stats["near_limit"] += 1
        stats["touch_limit_zone"] += 1

        # Avoid placing orders once the stock is effectively sealed at limit-up.
        # We prefer the tradable zone just below the upper limit so the backtest
        # has a chance to get filled instead of generating many 0-share orders.
        buy_floor = limit_price * (1.0 - CONFIG["max_buy_to_limit_buffer"])
        buy_ceiling = limit_price * (1.0 - CONFIG["min_limit_attack_buffer"])
        if last_price < buy_floor or last_price >= buy_ceiling:
            continue
        stats["tradable_gap"] += 1

        minute_df = attribute_history(
            symbol,
            20,
            unit="1m",
            fields=["close", "high", "low", "money", "volume"],
            skip_paused=True,
            fq="pre",
        )
        if minute_df is None or minute_df.empty or len(minute_df) < 6:
            continue
        stats["history"] += 1

        recent_total_amount = _recent_total_amount(minute_df)
        if recent_total_amount < CONFIG["min_20m_amount"]:
            continue

        break_count = _count_limit_breaks(minute_df, limit_price)
        if break_count > CONFIG["max_intraday_breaks"]:
            continue
        stats["breaks"] += 1

        amount_burst = _current_amount_burst(minute_df)
        volume_ratio = _current_volume_ratio(minute_df)
        if volume_ratio < CONFIG["min_volume_ratio"]:
            continue
        if volume_ratio > CONFIG["max_volume_ratio"] or amount_burst > CONFIG["max_amount_burst"]:
            continue
        stats["volume"] += 1

        close_strength = _close_strength(last_price, low_price, high_price)
        if close_strength < CONFIG["min_close_strength"]:
            continue
        board_distance = 1.0 - min(
            max((limit_price - last_price) / max(limit_price * CONFIG["near_limit_buffer"], 0.01), 0.0),
            1.0,
        )
        breakout_strength = min(meta["breakout_ratio"], 1.0)
        break_penalty = min(break_count / max(CONFIG["max_intraday_breaks"], 1.0), 1.0)
        amount_score = min(amount / max(meta["avg_amount5"], 1.0), 1.0)
        amount_burst_score = min(amount_burst / 2.0, 1.0)
        volume_score = min(volume_ratio / 3.0, 1.0)

        score = (
            0.18 * breakout_strength
            + 0.10 * board_distance
            + 0.18 * min(intraday_gain / 0.10, 1.0)
            + 0.14 * amount_score
            + 0.12 * amount_burst_score
            + 0.12 * close_strength
            + 0.10 * volume_score
            + 0.10 * _tradable_gap_score(last_price, limit_price)
            - 0.14 * break_penalty
        )

        candidates.append(
            {
                "symbol": symbol,
                "score": score,
                "limit_price": limit_price,
                "last_price": last_price,
                "intraday_gain": intraday_gain,
                "break_count": break_count,
                "volume_ratio": volume_ratio,
                "amount_burst": amount_burst,
                "amount_score": amount_score,
                "close_strength": close_strength,
                "board_distance": board_distance,
                "breakout_ratio": meta["breakout_ratio"],
                "recent_return5": meta["recent_return5"],
                "recent_return2": meta["recent_return2"],
            }
        )
        stats["passed"] += 1

    candidates.sort(key=lambda item: item["score"], reverse=True)
    if _in_window(context.current_dt, "09:40", "09:40"):
        log.info("scan stats: %s", stats)
        if candidates:
            log.info(
                "top candidate %s score=%.4f price=%.2f limit=%.2f",
                candidates[0]["symbol"],
                candidates[0]["score"],
                candidates[0]["last_price"],
                candidates[0]["limit_price"],
            )
    return candidates


def _open_position(context, candidate):
    symbol = candidate["symbol"]
    cash = context.portfolio.available_cash
    target_value = cash * CONFIG["position_pct"]
    if target_value <= 0:
        return

    order_value(symbol, target_value)
    g.daily_open_count += 1
    g.entry_records[symbol] = {
        "entry_price": candidate["last_price"],
        "entry_day": context.current_dt.date(),
        "entry_dt": context.current_dt.strftime("%Y-%m-%d %H:%M"),
        "entry_score": candidate["score"],
        "intraday_gain": candidate["intraday_gain"],
        "break_count": candidate["break_count"],
        "volume_ratio": candidate["volume_ratio"],
        "amount_burst": candidate["amount_burst"],
        "amount_score": candidate["amount_score"],
        "close_strength": candidate["close_strength"],
        "board_distance": candidate["board_distance"],
        "breakout_ratio": candidate["breakout_ratio"],
        "recent_return5": candidate["recent_return5"],
        "recent_return2": candidate["recent_return2"],
    }
    log.info(
        "buy %s score=%.4f gain=%.4f vr=%.2f burst=%.2f breaks=%s board_dist=%.3f r5=%.3f",
        symbol,
        candidate["score"],
        candidate["intraday_gain"],
        candidate["volume_ratio"],
        candidate["amount_burst"],
        candidate["break_count"],
        candidate["board_distance"],
        candidate["recent_return5"],
    )


def _manage_positions(context, data, now):
    current_data = get_current_data()
    # 先拷贝，避免下单导致持仓字典变化引发运行时错误
    for symbol, position_obj in list(context.portfolio.positions.items()):
        if symbol not in g.entry_records:
            g.entry_records[symbol] = {
                "entry_price": _position_cost(position_obj),
                "entry_day": now.date(),
            }
        try:
            bar = data[symbol]
        except Exception:
            continue

        last_price = _safe_float(bar.close)
        if last_price <= 0:
            continue
        limit_price = _safe_float(current_data[symbol].high_limit)
        entry = g.entry_records[symbol]
        entry_cost = max(_position_cost(position_obj), entry["entry_price"], 0.01)
        pnl = last_price / entry_cost - 1.0
        closeable_amount = int(_safe_float(getattr(position_obj, "closeable_amount", 0)))
        is_entry_day = now.date() == entry["entry_day"]

        if is_entry_day:
            if closeable_amount <= 0:
                continue
            if pnl <= -CONFIG["same_day_stop_loss"]:
                _close_position(context, symbol, position_obj, "same_day_stop", pnl)
                continue
            if _in_window(now, CONFIG["same_day_exit_time"], "15:00") and last_price < limit_price * (
                1.0 - CONFIG["near_limit_buffer"]
            ):
                _close_position(context, symbol, position_obj, "failed_to_hold_board", pnl)
                continue
        else:
            if _in_window(now, "09:30", "09:40") and pnl <= -CONFIG["next_day_weak_open_exit"]:
                _close_position(context, symbol, position_obj, "next_day_weak_open_exit", pnl)
                continue
            if pnl >= CONFIG["next_day_take_profit"]:
                _close_position(context, symbol, position_obj, "next_day_take_profit", pnl)
                continue
            if pnl <= -CONFIG["next_day_stop_loss"]:
                _close_position(context, symbol, position_obj, "next_day_stop_loss", pnl)
                continue
            if _in_window(now, CONFIG["next_day_force_exit"], "15:00"):
                _close_position(context, symbol, position_obj, "next_day_time_exit", pnl)


def _close_position(context, symbol, position_obj, reason, pnl):
    closeable_amount = int(_safe_float(getattr(position_obj, "closeable_amount", 0)))
    if closeable_amount <= 0:
        log.info("skip sell %s reason=%s closeable=0 pnl=%.4f", symbol, reason, pnl)
        return
    entry = g.entry_records.get(symbol, {})
    existing_exit = g.pending_exits.get(symbol)
    if existing_exit and existing_exit.get("reason") == reason:
        log.info("pending sell %s reason=%s pnl=%.4f", symbol, reason, pnl)
        return

    hold_days = _holding_days(entry.get("entry_day"), context.current_dt.date())
    g.pending_exits[symbol] = {
        "reason": reason,
        "pnl": pnl,
        "hold_days": hold_days,
        "entry_score": _safe_float(entry.get("entry_score", 0)),
        "intraday_gain": _safe_float(entry.get("intraday_gain", 0)),
        "volume_ratio": _safe_float(entry.get("volume_ratio", 0)),
        "amount_burst": _safe_float(entry.get("amount_burst", 0)),
        "break_count": int(_safe_float(entry.get("break_count", 0))),
        "board_distance": _safe_float(entry.get("board_distance", 0)),
        "recent_return5": _safe_float(entry.get("recent_return5", 0)),
        "recent_return2": _safe_float(entry.get("recent_return2", 0)),
        "entry_dt": entry.get("entry_dt", ""),
        "exit_dt": context.current_dt.strftime("%Y-%m-%d %H:%M"),
    }
    log.info("try sell %s reason=%s closeable=%s pnl=%.4f", symbol, reason, closeable_amount, pnl)
    order_target(symbol, 0)
    log.info("submit sell %s reason=%s", symbol, reason)


def _sync_positions_from_portfolio(context):
    live_symbols = set(context.portfolio.positions.keys())
    for symbol in list(g.entry_records.keys()):
        if symbol not in live_symbols:
            exit_info = g.pending_exits.pop(symbol, None)
            if exit_info:
                g.closed_trades.append(
                    {
                        "symbol": symbol,
                        "reason": exit_info["reason"],
                        "pnl": exit_info["pnl"],
                        "hold_days": exit_info["hold_days"],
                        "entry_score": exit_info["entry_score"],
                        "intraday_gain": exit_info["intraday_gain"],
                        "volume_ratio": exit_info["volume_ratio"],
                        "amount_burst": exit_info["amount_burst"],
                        "break_count": exit_info["break_count"],
                        "board_distance": exit_info["board_distance"],
                        "recent_return5": exit_info["recent_return5"],
                        "recent_return2": exit_info["recent_return2"],
                        "entry_dt": exit_info["entry_dt"],
                        "exit_dt": exit_info["exit_dt"],
                    }
                )
                log.info(
                    "sell filled %s reason=%s pnl=%.4f hold_days=%s entry_score=%.4f gain=%.4f vr=%.2f breaks=%s",
                    symbol,
                    exit_info["reason"],
                    exit_info["pnl"],
                    exit_info["hold_days"],
                    exit_info["entry_score"],
                    exit_info["intraday_gain"],
                    exit_info["volume_ratio"],
                    exit_info["break_count"],
                )
            g.entry_records.pop(symbol, None)

    for symbol, position_obj in context.portfolio.positions.items():
        if symbol not in g.entry_records:
            g.entry_records[symbol] = {
                "entry_price": _position_cost(position_obj),
                "entry_day": context.current_dt.date(),
            }
        if symbol in g.pending_exits:
            g.pending_exits[symbol]["pnl"] = (
                max(_safe_float(getattr(position_obj, "price", 0)), _safe_float(getattr(position_obj, "avg_cost", 0)))
                / max(g.entry_records[symbol]["entry_price"], 0.01)
                - 1.0
            )


def _count_limit_breaks(minute_df, limit_price):
    breaks = 0
    for high_price, close_price in zip(minute_df["high"].tolist(), minute_df["close"].tolist()):
        if high_price >= limit_price * 0.999 and close_price < limit_price * 0.997:
            breaks += 1
    return breaks


def _current_amount_burst(minute_df):
    values = minute_df["money"].tolist()
    if len(values) < 6:
        return 0.0
    current_amount = _safe_float(values[-1])
    base_amount = _mean(values[-6:-1])
    if base_amount <= 0:
        return 0.0
    return current_amount / base_amount


def _recent_total_amount(minute_df):
    values = minute_df["money"].tolist()
    return sum(_safe_float(value) for value in values)


def _current_volume_ratio(minute_df):
    values = minute_df["volume"].tolist()
    if len(values) < 6:
        return 0.0
    current_volume = _safe_float(values[-1])
    base_volume = _mean(values[-6:-1])
    if base_volume <= 0:
        return 0.0
    return current_volume / base_volume


def _close_strength(last_price, low_price, high_price):
    if high_price <= low_price:
        return 0.0
    return min(max((last_price - low_price) / (high_price - low_price), 0.0), 1.0)


def _tradable_gap_score(last_price, limit_price):
    gap_ratio = (limit_price - last_price) / max(limit_price, 0.01)
    if gap_ratio <= CONFIG["min_limit_attack_buffer"] or gap_ratio >= CONFIG["max_buy_to_limit_buffer"]:
        return 0.0
    mid = (CONFIG["min_limit_attack_buffer"] + CONFIG["max_buy_to_limit_buffer"]) / 2.0
    span = max((CONFIG["max_buy_to_limit_buffer"] - CONFIG["min_limit_attack_buffer"]) / 2.0, 1e-6)
    return max(0.0, 1.0 - abs(gap_ratio - mid) / span)


def _in_window(now, start_text, end_text):
    current = now.strftime("%H:%M")
    return start_text <= current <= end_text


def _is_trade_session(now):
    current = now.strftime("%H:%M")
    return ("09:30" <= current <= "11:30") or ("13:00" <= current <= "15:00")


def _safe_float(value):
    try:
        number = float(value)
    except Exception:
        return 0.0
    if math.isnan(number) or math.isinf(number):
        return 0.0
    return number


def _live_position_count(context):
    return len(context.portfolio.positions)


def _position_cost(position_obj):
    return max(
        _safe_float(getattr(position_obj, "avg_cost", 0)),
        _safe_float(getattr(position_obj, "price", 0)),
    )


def _holding_days(entry_day, exit_day):
    if not entry_day or not exit_day:
        return 0
    try:
        return max((exit_day - entry_day).days, 0)
    except Exception:
        return 0


def _log_analysis_snapshot(context):
    current_day = context.current_dt.date()
    if g.last_analysis_log_day == current_day:
        return
    g.last_analysis_log_day = current_day

    trades = g.closed_trades
    if not trades:
        log.info("analysis snapshot: no closed trades yet")
        return

    total = len(trades)
    wins = [item for item in trades if item["pnl"] > 0]
    losses = [item for item in trades if item["pnl"] <= 0]
    avg_win = _mean([item["pnl"] for item in wins])
    avg_loss = _mean([item["pnl"] for item in losses])
    avg_hold = _mean([item["hold_days"] for item in trades])
    avg_score = _mean([item["entry_score"] for item in trades])
    avg_gain = _mean([item["intraday_gain"] for item in trades])
    avg_vr = _mean([item["volume_ratio"] for item in trades])
    avg_r5 = _mean([item["recent_return5"] for item in trades])
    win_rate = len(wins) / float(total)
    profit_factor = abs(avg_win / avg_loss) if avg_loss < 0 else 0.0

    reason_groups = {}
    for item in trades:
        reason_groups.setdefault(item["reason"], []).append(item["pnl"])
    reason_summary = ",".join(
        "%s:%s/%.3f" % (reason, len(values), _mean(values))
        for reason, values in sorted(reason_groups.items())
    )

    log.info(
        "analysis snapshot closed=%s win_rate=%.3f avg_win=%.3f avg_loss=%.3f pf=%.3f avg_hold=%.2f avg_score=%.3f avg_gain=%.3f avg_vr=%.2f avg_r5=%.3f reasons=%s",
        total,
        win_rate,
        avg_win,
        avg_loss,
        profit_factor,
        avg_hold,
        avg_score,
        avg_gain,
        avg_vr,
        avg_r5,
        reason_summary,
    )

def _mean(values):
    clean = [_safe_float(value) for value in values if value is not None]
    if not clean:
        return 0.0
    return sum(clean) / float(len(clean))
