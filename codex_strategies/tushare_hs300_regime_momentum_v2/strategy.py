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
    g.signal_lookback = 20
    g.regime_lookback = 120
    g.top_n = 3
    g.min_breadth = 0.50
    g.risk_off_drawdown = 0.10
    g.cooldown_days = 10
    g.cooldown_until = None
    g.peak_total_value = context.portfolio.total_value
    g.last_rebalance_date = None

    run_weekly(rebalance, 1, time="9:30")
    run_daily(risk_control, time="14:50")


def _load_price_frame(securities, end_date, count: int) -> pd.DataFrame:
    if isinstance(securities, str):
        securities = [securities]

    df = get_price(
        list(securities),
        end_date=end_date,
        count=count,
        frequency="daily",
        fields=["close"],
        panel=False,
        fq="pre",
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


def _build_momentum_table(frame: pd.DataFrame, lookback: int) -> pd.DataFrame:
    if frame.empty or "code" not in frame.columns:
        return pd.DataFrame()

    pivot = frame.pivot_table(index=frame.index, columns="code", values="close", aggfunc="last")
    if pivot.empty:
        return pd.DataFrame()

    pivot = pivot.sort_index().dropna(axis=1, how="all")
    if len(pivot) < lookback + 1:
        return pd.DataFrame()

    base = pivot.iloc[-(lookback + 1)]
    latest = pivot.iloc[-1]
    table = pd.DataFrame({"momentum": latest / base - 1.0})
    table = table.replace([pd.NA, pd.NaT, float("inf"), float("-inf")], pd.NA).dropna()
    return table


def _benchmark_regime(context) -> bool:
    frame = _load_price_frame(g.index_code, end_date=context.previous_date, count=g.regime_lookback + 1)
    if frame.empty:
        return True

    closes = pd.to_numeric(frame["close"], errors="coerce").dropna().tail(g.regime_lookback + 1)
    if len(closes) < g.regime_lookback + 1:
        return True

    latest = float(closes.iloc[-1])
    long_ma = float(closes.iloc[:-1].mean())
    return_20d = float(latest / closes.iloc[-21] - 1.0)
    return latest > long_ma and return_20d > 0


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

    if not _benchmark_regime(context):
        _exit_all(context, "大盘趋势未满足")
        return

    stocks = get_index_stocks(g.index_code, date=context.previous_date)
    if not stocks:
        _exit_all(context, "沪深300成分股为空")
        return

    frame = _load_price_frame(stocks, end_date=context.previous_date, count=g.signal_lookback + 1)
    if frame.empty:
        _exit_all(context, "成分股行情不足")
        return

    momentum_table = _build_momentum_table(frame, g.signal_lookback)
    if momentum_table.empty:
        _exit_all(context, "动量数据不足")
        return

    breadth = float((momentum_table["momentum"] > 0).mean())
    if breadth < g.min_breadth:
        _exit_all(context, f"广度不足 breadth={breadth:.2f}")
        return

    ranked = momentum_table[momentum_table["momentum"] > 0].sort_values("momentum", ascending=False).head(g.top_n)
    if ranked.empty:
        _exit_all(context, "无正动量标的")
        return

    targets = list(ranked.index)
    _rebalance_targets(context, targets)
    log.info(
        "[调仓] breadth=%.2f 选中: %s",
        breadth,
        ", ".join([f"{code}:{row.momentum:.4f}" for code, row in ranked.iterrows()]),
    )
