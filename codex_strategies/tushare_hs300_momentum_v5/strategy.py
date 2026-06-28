from __future__ import annotations

from datetime import timedelta
from math import sqrt
from typing import Dict, List, Optional, Sequence, Tuple

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
    g.top_n = 4
    g.risk_off_drawdown = 0.08
    g.cooldown_days = 10
    g.cooldown_until = None
    g.peak_total_value = context.portfolio.total_value
    g.last_rebalance_date = None

    run_weekly(rebalance, 1, time="9:30")
    run_daily(risk_control, time="14:50")


def _load_price_frame(stocks: Sequence[str], end_date, count: int) -> pd.DataFrame:
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


def _series_from_frame(frame: pd.DataFrame, code: Optional[str] = None) -> pd.Series:
    if frame is None or frame.empty:
        return pd.Series(dtype="float64")

    if isinstance(frame.columns, pd.MultiIndex):
        if "close" not in set(frame.columns.get_level_values(0)):
            return pd.Series(dtype="float64")
        block = frame.xs("close", axis=1, level=0)
        if code is not None and code in block.columns:
            series = pd.to_numeric(block[code], errors="coerce").dropna()
            series.index = pd.to_datetime(series.index)
            return series.sort_index()
        if code is None and block.shape[1] == 1:
            series = pd.to_numeric(block.iloc[:, 0], errors="coerce").dropna()
            series.index = pd.to_datetime(series.index)
            return series.sort_index()
        return pd.Series(dtype="float64")

    if {"time", "code", "close"}.issubset(frame.columns):
        sub = frame if code is None else frame[frame["code"] == code]
        if sub.empty:
            return pd.Series(dtype="float64")
        sub = sub.sort_values("time")
        series = pd.to_numeric(sub["close"], errors="coerce")
        series.index = pd.to_datetime(sub["time"])
        series = series.dropna()
        return series.sort_index()

    if "close" in frame.columns:
        series = pd.to_numeric(frame["close"], errors="coerce").dropna()
        if isinstance(frame.index, pd.DatetimeIndex):
            series.index = pd.to_datetime(frame.index[: len(series)])
        else:
            series.index = pd.to_datetime(frame.index[: len(series)])
        return series.sort_index()

    return pd.Series(dtype="float64")


def _score_series(series: pd.Series) -> Optional[Dict[str, float]]:
    if series is None or series.empty or len(series) < g.lookback + 1:
        return None
    w = series.tail(g.lookback + 1).astype(float)
    if w.iloc[-1] <= 0:
        return None

    def ret(n: int) -> float:
        if len(w) <= n or w.iloc[-(n + 1)] <= 0:
            return 0.0
        return float(w.iloc[-1] / w.iloc[-(n + 1)] - 1.0)

    ma20 = float(w.tail(20).mean()) if len(w) >= 20 else float(w.mean())
    vol20 = float(w.pct_change().tail(20).std(ddof=0) * sqrt(250)) if len(w) > 1 else 0.0

    score = 0.65 * ret(20) + 0.25 * ret(60) + 0.10 * ret(120) - 0.10 * vol20

    return {
        "score": float(score),
        "ret20": float(ret(20)),
        "ret60": float(ret(60)),
        "ret120": float(ret(120)),
        "vol20": float(vol20),
        "ma20": float(ma20),
        "last_close": float(w.iloc[-1]),
    }


def _benchmark_risk_on(end_date) -> Tuple[bool, Dict[str, float]]:
    frame = get_price(
        g.index_code,
        end_date=end_date,
        count=121,
        frequency="daily",
        fields=["close"],
        fq="pre",
    )
    series = _series_from_frame(frame, g.index_code)
    if series.empty or len(series) < 61:
        return False, {}

    w = series.astype(float)
    latest = float(w.iloc[-1])
    ma60 = float(w.tail(60).mean())
    ma120 = float(w.tail(120).mean()) if len(w) >= 120 else float(w.mean())
    ret20 = float(w.iloc[-1] / w.iloc[-21] - 1.0) if len(w) > 20 and w.iloc[-21] > 0 else 0.0
    risk_on = latest > ma60 and latest > ma120 and ret20 > -0.01
    return risk_on, {
        "latest": latest,
        "ma60": ma60,
        "ma120": ma120,
        "ret20": ret20,
    }


def _select_targets(context) -> List[Tuple[str, float, Dict[str, float]]]:
    stocks = get_index_stocks(g.index_code, date=context.previous_date)
    if not stocks:
        return []

    frame = _load_price_frame(stocks, end_date=context.previous_date, count=g.lookback + 1)
    if frame.empty:
        return []

    ranked: List[Tuple[str, float, Dict[str, float]]] = []
    for code in stocks:
        series = _series_from_frame(frame, code)
        payload = _score_series(series)
        if not payload:
            continue
        if payload["ret20"] <= 0:
            continue
        if payload["ma20"] <= 0:
            continue
        if payload["last_close"] < payload["ma20"]:
            continue
        ranked.append((code, payload["score"], payload))

    ranked.sort(key=lambda item: item[1], reverse=True)
    return ranked[: g.top_n]


def _exit_all(context, reason: str) -> None:
    for stock in list(context.portfolio.positions.keys()):
        order_target_value(stock, 0)
    log.info("[调仓] %s，全部切换为现金", reason)


def _rebalance_to_targets(context, targets: Sequence[Tuple[str, float, Dict[str, float]]]) -> None:
    target_codes = {code for code, _, _ in targets}
    current_positions = list(context.portfolio.positions.keys())

    for stock in current_positions:
        if stock not in target_codes:
            order_target_value(stock, 0)

    if not targets:
        return

    current_data = get_current_data()
    tradable = [item for item in targets if item[0] in current_data and not current_data[item[0]].paused]
    if not tradable:
        return

    per_value = context.portfolio.total_value * 0.98 / len(tradable)
    for code, _, _ in tradable:
        order_target_value(code, per_value)


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

    _rebalance_to_targets(context, targets)
    log.info(
        "[调仓] 选中: %s",
        ", ".join([f"{code}:{score:.4f}" for code, score, _ in targets]),
    )
