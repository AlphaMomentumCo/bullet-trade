from __future__ import annotations

from datetime import timedelta

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
    g.universe_size = 30
    g.risk_off_drawdown = 0.10
    g.cooldown_days = 10
    g.cooldown_until = None
    g.peak_total_value = context.portfolio.total_value
    g.last_rebalance_date = None

    run_weekly(rebalance, 1, time="9:30")
    run_daily(risk_control, time="14:50")


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


def _select_targets(context):
    stocks = get_index_stocks(g.index_code, date=context.previous_date)
    if not stocks:
        return []
    if len(stocks) > g.universe_size:
        stocks = stocks[: g.universe_size]

    frame = _load_price_frame(stocks, end_date=context.previous_date, count=g.lookback + 1)
    if frame.empty:
        return []

    momentum = {}
    for code in stocks:
        sub = frame[frame["code"] == code] if "code" in frame.columns else frame
        if sub.empty:
            continue
        closes = pd.to_numeric(sub["close"], errors="coerce").dropna()
        closes = closes.tail(g.lookback + 1)
        if len(closes) < g.lookback + 1:
            continue
        if closes.iloc[0] <= 0:
            continue
        score = float(closes.iloc[-1] / closes.iloc[0] - 1.0)
        momentum[code] = score

    if not momentum:
        log.info("[选股] 候选=%d, 无可用动量序列", len(stocks))
        return []

    ranked = sorted(momentum.items(), key=lambda x: x[1], reverse=True)
    log.info(
        "[选股] 候选=%d, 有效=%d, 前几名=%s",
        len(stocks),
        len(ranked),
        ", ".join([f"{code}:{score:.4f}" for code, score in ranked[: min(5, len(ranked))]]),
    )
    return [code for code, _ in ranked[: g.top_n]]


def _exit_all(context, reason: str) -> None:
    for stock in list(context.portfolio.positions.keys()):
        order_target_value(stock, 0)
    log.info("[调仓] %s，全部切换为现金", reason)


def _rebalance_targets(context, targets):
    current_positions = list(context.portfolio.positions.keys())
    target_set = set(targets)
    for stock in current_positions:
        if stock not in target_set:
            order_target_value(stock, 0)

    if not targets:
        return

    current_data = get_current_data()
    tradable = [stock for stock in targets if stock in current_data and not current_data[stock].paused]
    if not tradable:
        return

    per_value = context.portfolio.total_value * 0.98 / len(tradable)
    for stock in tradable:
        order_target_value(stock, per_value)


def risk_control(context):
    current_value = float(context.portfolio.total_value)
    g.peak_total_value = max(g.peak_total_value, current_value)
    if g.peak_total_value <= 0:
        return

    drawdown = 1.0 - current_value / g.peak_total_value
    if drawdown >= g.risk_off_drawdown:
        g.cooldown_until = context.current_dt.date() + timedelta(days=g.cooldown_days)
        log.info(
            "[风险控制] 回撤熔断: peak=%.2f current=%.2f drawdown=%.2f%% cooldown_until=%s",
            g.peak_total_value,
            current_value,
            drawdown * 100.0,
            g.cooldown_until,
        )
        _exit_all(context, "组合回撤触发熔断")


def rebalance(context):
    today = context.current_dt.date()
    if g.last_rebalance_date == today:
        return
    g.last_rebalance_date = today

    if g.cooldown_until is not None and today <= g.cooldown_until:
        _exit_all(context, "冷静期内")
        return

    targets = _select_targets(context)
    if not targets:
        _exit_all(context, "没有满足条件的强势成分股")
        return

    _rebalance_targets(context, targets)
    log.info("[调仓] 选中: %s", ", ".join(targets))
