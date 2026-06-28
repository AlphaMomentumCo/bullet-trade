from __future__ import annotations

from datetime import timedelta

import pandas as pd

from jqdata import *  # noqa: F401,F403


def initialize(context):
    set_data_provider("tushare")
    set_benchmark("000300.XSHG")
    set_option("use_real_price", True)
    set_option("avoid_future_data", True)

    g.index_code = "000300.XSHG"
    g.lookback = 20
    g.top_n = 5
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
        fq="pre",
    )
    if df is None or df.empty:
        return pd.DataFrame()
    if not {"time", "code", "close"}.issubset(df.columns):
        return pd.DataFrame()
    out = df[["time", "code", "close"]].copy()
    out["time"] = pd.to_datetime(out["time"])
    out["code"] = out["code"].astype(str)
    out["close"] = pd.to_numeric(out["close"], errors="coerce")
    return out.dropna(subset=["close"])


def _series_from_frame(frame: pd.DataFrame, code: str) -> pd.Series:
    if frame is None or frame.empty:
        return pd.Series(dtype="float64")
    if {"time", "code", "close"}.issubset(frame.columns):
        sub = frame[frame["code"] == code]
        if sub.empty:
            return pd.Series(dtype="float64")
        sub = sub.sort_values("time")
        series = pd.to_numeric(sub["close"], errors="coerce").dropna()
        series.index = pd.to_datetime(sub.loc[series.index, "time"])
        return series.sort_index()
    if "close" in frame.columns:
        series = pd.to_numeric(frame["close"], errors="coerce").dropna()
        series.index = pd.to_datetime(frame.index[: len(series)])
        return series.sort_index()
    return pd.Series(dtype="float64")


def _benchmark_risk_on(end_date) -> tuple[bool, dict[str, float]]:
    try:
        frame = get_price(
            g.index_code,
            end_date=end_date,
            count=121,
            frequency="daily",
            fields=["close"],
            fq="pre",
        )
    except Exception:
        return True, {}

    series = _series_from_frame(frame, g.index_code)
    if series.empty or len(series) < 121:
        return True, {}

    w = series.astype(float)
    latest = float(w.iloc[-1])
    ma60 = float(w.tail(60).mean())
    ma120 = float(w.tail(120).mean())
    ret20 = float(w.iloc[-1] / w.iloc[-21] - 1.0) if len(w) > 20 and w.iloc[-21] > 0 else 0.0
    risk_on = latest > ma60 and latest > ma120 and ret20 > -0.01
    return risk_on, {"latest": latest, "ma60": ma60, "ma120": ma120, "ret20": ret20}


def _select_targets(context):
    stocks = get_index_stocks(g.index_code, date=context.previous_date)
    if not stocks:
        return []

    frame = _load_price_frame(stocks, end_date=context.previous_date, count=g.lookback + 1)
    if frame.empty:
        return []

    momentum = {}
    for code in stocks:
        series = _series_from_frame(frame, code)
        if len(series) < g.lookback + 1:
            continue
        closes = series.tail(g.lookback + 1)
        if closes.iloc[0] <= 0:
            continue
        score = float(closes.iloc[-1] / closes.iloc[0] - 1.0)
        if score <= 0:
            continue
        momentum[code] = score

    if not momentum:
        return []

    return [code for code, _ in sorted(momentum.items(), key=lambda x: x[1], reverse=True)[: g.top_n]]


def _exit_all(context, reason: str) -> None:
    for stock in list(context.portfolio.positions.keys()):
        order_target_value(stock, 0)
    log.info("[调仓] %s，全部切换为现金", reason)


def _rebalance_targets(context, targets):
    current_positions = list(context.portfolio.positions.keys())
    for stock in current_positions:
        if stock not in targets:
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

    risk_on, regime = _benchmark_risk_on(context.previous_date)
    if not risk_on:
        _exit_all(
            context,
            "基准趋势偏弱: latest=%.2f ma60=%.2f ma120=%.2f ret20=%.2f%%"
            % (
                regime.get("latest", 0.0),
                regime.get("ma60", 0.0),
                regime.get("ma120", 0.0),
                regime.get("ret20", 0.0) * 100.0,
            ),
        )
        return

    targets = _select_targets(context)
    if not targets:
        _exit_all(context, "没有满足条件的强势成分股")
        return

    _rebalance_targets(context, targets)
    log.info("[调仓] 选中: %s", ", ".join(targets))
