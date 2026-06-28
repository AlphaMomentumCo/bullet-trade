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
    g.regime_lookback = 120
    g.top_n = 5
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


def _series_return(frame: pd.DataFrame, code: str, window: int) -> float | None:
    sub = frame[frame["code"] == code] if "code" in frame.columns else frame
    if sub.empty:
        return None
    closes = pd.to_numeric(sub["close"], errors="coerce").dropna().tail(window + 1)
    if len(closes) < window + 1 or closes.iloc[0] <= 0:
        return None
    return float(closes.iloc[-1] / closes.iloc[0] - 1.0)


def _in_bull_regime(context) -> bool:
    frame = _load_price_frame(g.index_code, end_date=context.previous_date, count=g.regime_lookback + 1)
    if frame.empty:
        return False

    closes = pd.to_numeric(frame["close"], errors="coerce").dropna().tail(g.regime_lookback + 1)
    if len(closes) < g.regime_lookback + 1:
        return False

    latest = float(closes.iloc[-1])
    prior = closes.iloc[:-1]
    if prior.empty:
        return False

    trend_ok = latest > float(prior.mean())
    momentum_ok = latest / float(closes.iloc[-21]) - 1.0 > 0
    return trend_ok and momentum_ok


def rebalance(context):
    today = context.current_dt.date()
    if g.last_rebalance_date == today:
        return
    g.last_rebalance_date = today

    current_positions = list(context.portfolio.positions.keys())

    if not _in_bull_regime(context):
        for stock in current_positions:
            order_target_value(stock, 0)
        log.info("[调仓] 大盘趋势未满足，空仓")
        return

    stocks = get_index_stocks(g.index_code, date=context.previous_date)
    if not stocks:
        return

    frame = _load_price_frame(stocks, end_date=context.previous_date, count=g.long_lookback + 1)
    if frame.empty:
        return

    scores = {}
    for code in stocks:
        short_ret = _series_return(frame, code, g.short_lookback)
        long_ret = _series_return(frame, code, g.long_lookback)
        if short_ret is None or long_ret is None:
            continue
        if short_ret <= 0 or long_ret <= 0:
            continue
        score = 0.7 * short_ret + 0.3 * long_ret
        scores[code] = score

    if not scores:
        for stock in current_positions:
            order_target_value(stock, 0)
        log.info("[调仓] 大盘转强但没有可买标的，空仓")
        return

    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)[: g.top_n]
    targets = [code for code, _ in ranked]
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
