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
            close_tax=0,
            open_commission=0.0002,
            close_commission=0.0002,
            min_commission=5,
        ),
        type="stock",
    )

    # 用一篮子主流宽基和行业 ETF 做系统化轮动，避免只盯着少数热点主题。
    g.offensive_universe = [
        "510050.XSHG",  # 上证50ETF
        "510300.XSHG",  # 沪深300ETF
        "510500.XSHG",  # 中证500ETF
        "159915.XSHE",  # 创业板ETF
        "159949.XSHE",  # 创业板50ETF
        "588000.XSHG",  # 科创50ETF
        "512480.XSHG",  # 半导体ETF
        "512760.XSHG",  # 芯片ETF
        "159819.XSHE",  # 人工智能ETF
        "515790.XSHG",  # 光伏ETF
        "512000.XSHG",  # 券商ETF
        "512170.XSHG",  # 医疗ETF
        "512690.XSHG",  # 酒ETF
        "512980.XSHG",  # 银行ETF
        "512710.XSHG",  # 军工ETF
        "516160.XSHG",  # 新能源车ETF
    ]
    g.safe_asset = "511880.XSHG"  # 货币ETF，作为风控兜底
    g.benchmark_filter = "510300.XSHG"  # 用更稳的广基 ETF 做市场状态过滤
    g.lookback = 120
    g.short_window = 20
    g.mid_window = 60
    g.top_n = 1
    g.min_score = 0.02
    g.risk_off_drawdown = 0.10
    g.cooldown_days = 5
    g.cooldown_until = None
    g.peak_total_value = context.portfolio.total_value
    g.last_rebalance_date = None

    run_weekly(rebalance, 1, time="9:30")
    run_daily(risk_control, time="14:50")


def _load_price_frame(stocks, end_date, count: int, fields: List[str]) -> pd.DataFrame:
    securities = [stocks] if isinstance(stocks, str) else list(stocks)
    df = get_price(
        securities,
        end_date=end_date,
        count=count,
        frequency="daily",
        fields=fields,
        panel=False,
        fq=None,
    )
    if df is None or df.empty:
        return pd.DataFrame()

    out = df.copy()
    if "code" in out.columns:
        out["code"] = out["code"].astype(str)
    for field in fields:
        if field in out.columns:
            out[field] = pd.to_numeric(out[field], errors="coerce")
    if not isinstance(out.index, pd.DatetimeIndex):
        return pd.DataFrame()
    return out.dropna(subset=[field for field in fields if field in out.columns], how="all")


def _series_from_frame(frame: pd.DataFrame, code: str, field: str = "close") -> pd.Series:
    if frame is None or frame.empty:
        return pd.Series(dtype="float64")
    if "code" in frame.columns:
        sub = frame[frame["code"] == code]
        if sub.empty or field not in sub.columns:
            return pd.Series(dtype="float64")
        series = pd.to_numeric(sub[field], errors="coerce").dropna()
        return series.sort_index()
    if field not in frame.columns:
        return pd.Series(dtype="float64")
    series = pd.to_numeric(frame[field], errors="coerce").dropna()
    return series.sort_index()


def _benchmark_risk_on(context) -> Tuple[bool, Dict[str, float]]:
    frame = _load_price_frame(g.benchmark_filter, end_date=context.previous_date, count=g.lookback + 1, fields=["close"])
    series = _series_from_frame(frame, g.benchmark_filter, "close")

    if series.empty or len(series) < g.lookback + 1:
        return False, {}

    w = series.tail(g.lookback + 1).astype(float)
    latest = float(w.iloc[-1])
    ma20 = float(w.tail(g.short_window).mean()) if len(w) >= g.short_window else float(w.mean())
    ma60 = float(w.tail(g.mid_window).mean()) if len(w) >= g.mid_window else float(w.mean())
    ma120 = float(w.mean())
    ret20 = float(w.iloc[-1] / w.iloc[-(g.short_window + 1)] - 1.0) if len(w) > g.short_window and w.iloc[-(g.short_window + 1)] > 0 else 0.0
    risk_on = latest > ma20 and latest > ma60 and ret20 > -0.02
    return risk_on, {"latest": latest, "ma20": ma20, "ma60": ma60, "ma120": ma120, "ret20": ret20}


def _score_series(open_series: pd.Series, close_series: pd.Series) -> float:
    close_series = pd.to_numeric(close_series, errors="coerce").dropna().tail(g.lookback + 1)
    if len(close_series) < g.short_window + 1 or close_series.iloc[0] <= 0:
        return float("nan")

    open_series = pd.to_numeric(open_series, errors="coerce").dropna().tail(len(close_series))

    def ret(window: int) -> float:
        if len(close_series) <= window:
            return 0.0
        base = float(close_series.iloc[-(window + 1)])
        if base <= 0:
            return 0.0
        return float(close_series.iloc[-1] / base - 1.0)

    def mean_gap(window: int) -> float:
        if open_series.empty or len(open_series) < window:
            return 0.0
        closes = close_series.tail(window)
        opens = open_series.tail(window)
        if len(closes) != len(opens):
            m = min(len(closes), len(opens))
            closes = closes.tail(m)
            opens = opens.tail(m)
        valid = (opens > 0) & (closes > 0)
        if not valid.any():
            return 0.0
        return float((closes[valid] / opens[valid] - 1.0).mean())

    recent = close_series.tail(g.lookback)
    ma20 = float(recent.tail(g.short_window).mean()) if len(recent) >= g.short_window else float(recent.mean())
    ma60 = float(recent.tail(g.mid_window).mean()) if len(recent) >= g.mid_window else float(recent.mean())
    trend20 = float(recent.iloc[-1] / ma20 - 1.0) if ma20 > 0 else 0.0
    trend60 = float(recent.iloc[-1] / ma60 - 1.0) if ma60 > 0 else 0.0
    vol20 = float(recent.pct_change().dropna().std(ddof=0) * sqrt(252)) if len(recent) > 1 else 0.0
    drawdown20 = float((recent / recent.cummax() - 1.0).min()) if len(recent) > 1 else 0.0
    gap5 = mean_gap(5)

    score = (
        0.08 * ret(5)
        + 0.22 * ret(g.short_window)
        + 0.30 * ret(g.mid_window)
        + 0.18 * ret(g.lookback)
        + 0.08 * trend20
        + 0.08 * trend60
        + 0.06 * gap5
        - 0.05 * vol20
        + 0.03 * drawdown20
    )
    return float(score)


def _select_targets(context) -> List[Tuple[str, float]]:
    frame = _load_price_frame(
        g.offensive_universe,
        end_date=context.previous_date,
        count=g.lookback + 1,
        fields=["open", "close"],
    )
    if frame.empty:
        return []

    ranked: List[Tuple[str, float]] = []
    for code in g.offensive_universe:
        close_series = _series_from_frame(frame, code, "close")
        open_series = _series_from_frame(frame, code, "open")
        score = _score_series(open_series, close_series)
        if pd.isna(score) or score < g.min_score:
            continue
        ranked.append((code, score))

    ranked.sort(key=lambda item: item[1], reverse=True)
    log.info(
        "[选基] 候选=%d, 有效=%d, 前几名=%s",
        len(g.offensive_universe),
        len(ranked),
        ", ".join([f"{code}:{score:.4f}" for code, score in ranked[: min(5, len(ranked))]]),
    )
    return ranked[: g.top_n]


def _exit_all(context, reason: str) -> None:
    for stock in list(context.portfolio.positions.keys()):
        order_target_value(stock, 0)
    log.info("[调仓] %s，全部切换为现金", reason)


def _hold_safe_asset(context, reason: str) -> None:
    for stock in list(context.portfolio.positions.keys()):
        if stock != g.safe_asset:
            order_target_value(stock, 0)
    order_target_value(g.safe_asset, context.portfolio.total_value * 0.98)
    log.info("[调仓] %s，切换到安全资产 %s", reason, g.safe_asset)


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
        _hold_safe_asset(context, "组合回撤触发熔断")


def rebalance(context):
    today = context.current_dt.date()
    if g.last_rebalance_date == today:
        return
    g.last_rebalance_date = today

    if g.cooldown_until is not None and today <= g.cooldown_until:
        _hold_safe_asset(context, "冷静期内")
        return

    risk_on, regime = _benchmark_risk_on(context)
    if not risk_on:
        _hold_safe_asset(
            context,
            "基准趋势偏弱: latest=%.2f ma20=%.2f ma60=%.2f ma120=%.2f ret20=%.2f%%"
            % (
                regime.get("latest", 0.0),
                regime.get("ma20", 0.0),
                regime.get("ma60", 0.0),
                regime.get("ma120", 0.0),
                regime.get("ret20", 0.0) * 100.0,
            ),
        )
        return

    targets = _select_targets(context)
    if not targets:
        _hold_safe_asset(context, "没有满足阈值的强势 ETF")
        return

    target_codes = {code for code, _ in targets}
    for stock in list(context.portfolio.positions.keys()):
        if stock not in target_codes and stock != g.safe_asset:
            order_target_value(stock, 0)

    current_data = get_current_data()
    total_weight = 0.0
    tradable_targets: List[Tuple[str, float]] = []
    for code, score in targets:
        if code in current_data and not current_data[code].paused:
            tradable_targets.append((code, score))
            total_weight += max(score, 0.0)

    if not tradable_targets or total_weight <= 0:
        _hold_safe_asset(context, "目标 ETF 停牌或权重为零")
        return

    for code, score in tradable_targets:
        weight = max(score, 0.0) / total_weight
        order_target_value(code, context.portfolio.total_value * 0.98 * weight)

    if g.safe_asset in context.portfolio.positions:
        order_target_value(g.safe_asset, 0)

    log.info("[调仓] 选中: %s", ", ".join([f"{code}:{score:.4f}" for code, score in tradable_targets]))
