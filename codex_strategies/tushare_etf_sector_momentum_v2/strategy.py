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

    # 采用更宽的 ETF 池，避免只盯着少数主题。
    g.safe_asset = "511880.XSHG"
    g.benchmark_code = "510300.XSHG"
    g.offensive_universe = [
        "510050.XSHG",
        "510300.XSHG",
        "510500.XSHG",
        "159915.XSHE",
        "159949.XSHE",
        "512480.XSHG",
        "512760.XSHG",
        "159819.XSHE",
        "588000.XSHG",
        "515790.XSHG",
        "512000.XSHG",
        "512170.XSHG",
        "512690.XSHG",
        "512980.XSHG",
        "512710.XSHG",
        "516160.XSHG",
    ]

    g.lookback = 130
    g.short_window = 20
    g.mid_window = 60
    g.long_window = 120
    g.top_n = 3
    g.min_strength = 0.008
    g.max_portfolio_drawdown = 0.085
    g.individual_stop_drawdown = 0.070
    g.individual_stop_gap = 0.040
    g.cooldown_days = 7
    g.cooldown_until = None
    g.peak_total_value = context.portfolio.total_value
    g.last_rebalance_date = None

    run_weekly(rebalance, 1, time="9:30")
    run_daily(risk_control, time="14:50")


def _load_price_frame(stocks, end_date, count: int, fields: List[str]) -> pd.DataFrame:
    securities = [stocks] if isinstance(stocks, str) else list(stocks)
    frames: List[pd.DataFrame] = []

    for security in securities:
        df = get_price(
            security,
            end_date=end_date,
            count=count,
            frequency="daily",
            fields=fields,
            panel=False,
            fq=None,
        )
        if df is None or df.empty:
            continue

        out = df.copy()
        if "code" not in out.columns:
            out["code"] = security
        else:
            out["code"] = out["code"].astype(str)
        for field in fields:
            if field in out.columns:
                out[field] = pd.to_numeric(out[field], errors="coerce")
        if not isinstance(out.index, pd.DatetimeIndex):
            continue
        frames.append(out)

    if not frames:
        return pd.DataFrame()

    merged = pd.concat(frames, axis=0, ignore_index=False)
    merged["code"] = merged["code"].astype(str)
    return merged.dropna(subset=[field for field in fields if field in merged.columns], how="all")


def _series_from_frame(frame: pd.DataFrame, code: str, field: str = "close") -> pd.Series:
    if frame is None or frame.empty:
        return pd.Series(dtype="float64")
    if "code" in frame.columns:
        sub = frame[frame["code"] == code]
        if sub.empty or field not in sub.columns:
            return pd.Series(dtype="float64")
        series = pd.to_numeric(sub[field], errors="coerce").dropna().sort_index()
        series.index = pd.to_datetime(series.index)
        return series
    if field not in frame.columns:
        return pd.Series(dtype="float64")
    series = pd.to_numeric(frame[field], errors="coerce").dropna().sort_index()
    series.index = pd.to_datetime(series.index)
    return series


def _safe_ret(series: pd.Series, window: int) -> float:
    if len(series) <= window:
        return 0.0
    base = float(series.iloc[-(window + 1)])
    if base <= 0:
        return 0.0
    return float(series.iloc[-1] / base - 1.0)


def _score_series(series: pd.Series, benchmark: Optional[pd.Series] = None) -> Optional[Dict[str, float]]:
    if series is None:
        return None
    closes = pd.to_numeric(series, errors="coerce").dropna().tail(g.lookback + 1)
    if len(closes) < g.long_window + 1 or closes.iloc[0] <= 0:
        return None

    recent = closes.tail(g.lookback)
    if recent.empty:
        return None

    ma20 = float(recent.tail(g.short_window).mean()) if len(recent) >= g.short_window else float(recent.mean())
    ma60 = float(recent.tail(g.mid_window).mean()) if len(recent) >= g.mid_window else float(recent.mean())
    ma120 = float(recent.tail(g.long_window).mean()) if len(recent) >= g.long_window else float(recent.mean())

    ret5 = _safe_ret(closes, 5)
    ret20 = _safe_ret(closes, g.short_window)
    ret60 = _safe_ret(closes, g.mid_window)
    ret120 = _safe_ret(closes, g.long_window)

    trend20 = float(recent.iloc[-1] / ma20 - 1.0) if ma20 > 0 else 0.0
    trend60 = float(recent.iloc[-1] / ma60 - 1.0) if ma60 > 0 else 0.0
    trend120 = float(recent.iloc[-1] / ma120 - 1.0) if ma120 > 0 else 0.0
    vol20 = float(recent.pct_change().tail(g.short_window).std(ddof=0) * sqrt(252)) if len(recent) > 1 else 0.0
    dd20 = float((recent.tail(g.short_window) / recent.tail(g.short_window).cummax() - 1.0).min()) if len(recent) > 1 else 0.0

    rel60 = 0.0
    if benchmark is not None and not benchmark.empty and len(benchmark) >= g.mid_window + 1:
        bench = pd.to_numeric(benchmark, errors="coerce").dropna().tail(g.lookback + 1)
        if len(bench) >= g.mid_window + 1 and bench.iloc[0] > 0:
            rel60 = ret60 - _safe_ret(bench, g.mid_window)

    score = (
        0.05 * ret5
        + 0.20 * ret20
        + 0.24 * ret60
        + 0.16 * ret120
        + 0.08 * trend20
        + 0.10 * trend60
        + 0.07 * trend120
        + 0.08 * rel60
        - 0.05 * vol20
        + 0.03 * dd20
    )

    return {
        "score": float(score),
        "ret5": float(ret5),
        "ret20": float(ret20),
        "ret60": float(ret60),
        "ret120": float(ret120),
        "trend20": float(trend20),
        "trend60": float(trend60),
        "trend120": float(trend120),
        "vol20": float(vol20),
        "dd20": float(dd20),
        "rel60": float(rel60),
    }


def _move_to_safe_asset(context, reason: str) -> None:
    for stock in list(context.portfolio.positions.keys()):
        if stock != g.safe_asset:
            order_target_value(stock, 0)
    order_target_value(g.safe_asset, context.portfolio.total_value * 0.98)
    log.info("[调仓] %s，切换到防御资产 %s", reason, g.safe_asset)


def _select_targets(context) -> List[Tuple[str, Dict[str, float]]]:
    frame = _load_price_frame(
        [*g.offensive_universe, g.benchmark_code],
        end_date=context.previous_date,
        count=g.lookback + 1,
        fields=["close"],
    )
    if frame.empty:
        return []

    benchmark = _series_from_frame(frame, g.benchmark_code, "close")
    ranked: List[Tuple[str, Dict[str, float]]] = []
    for code in g.offensive_universe:
        series = _series_from_frame(frame, code, "close")
        payload = _score_series(series, benchmark=benchmark)
        if not payload:
            continue
        if payload["score"] <= 0:
            continue
        ranked.append((code, payload))

    ranked.sort(key=lambda item: item[1]["score"], reverse=True)
    if ranked:
        log.info(
            "[选基] 有效=%d, 前几名=%s",
            len(ranked),
            ", ".join([f"{code}:{payload['score']:.4f}" for code, payload in ranked[: min(5, len(ranked))]]),
        )
    else:
        log.info("[选基] 无有效进攻标的")
    return ranked[: g.top_n]


def risk_control(context):
    current_value = float(context.portfolio.total_value)
    g.peak_total_value = max(g.peak_total_value, current_value)
    if g.peak_total_value <= 0:
        return

    drawdown = 1.0 - current_value / g.peak_total_value
    today = context.current_dt.date()
    if drawdown >= g.max_portfolio_drawdown:
        g.cooldown_until = today + timedelta(days=g.cooldown_days)
        log.info(
            "[风控] 组合回撤触发熔断: peak=%.2f current=%.2f drawdown=%.2f%% cooldown_until=%s",
            g.peak_total_value,
            current_value,
            drawdown * 100.0,
            g.cooldown_until,
        )
        _move_to_safe_asset(context, "组合回撤触发熔断")
        return

    current_positions = list(context.portfolio.positions.keys())
    if not current_positions:
        return

    for stock in current_positions:
        if stock == g.safe_asset:
            continue
        frame = _load_price_frame(stock, end_date=context.previous_date, count=21, fields=["close"])
        series = _series_from_frame(frame, stock, "close")
        if len(series) < 21:
            continue
        closes = series.tail(21)
        latest = float(closes.iloc[-1])
        peak_20 = float(closes.max())
        ma20 = float(closes.tail(20).mean()) if len(closes) >= 20 else float(closes.mean())
        trailing_dd = 1.0 - latest / peak_20 if peak_20 > 0 else 0.0
        if trailing_dd >= g.individual_stop_drawdown or (ma20 > 0 and latest < ma20 * (1.0 - g.individual_stop_gap)):
            log.info(
                "[风控] 个股止损: %s latest=%.2f peak20=%.2f ma20=%.2f dd=%.2f%%",
                stock,
                latest,
                peak_20,
                ma20,
                trailing_dd * 100.0,
            )
            order_target_value(stock, 0)


def rebalance(context):
    today = context.current_dt.date()
    if g.last_rebalance_date == today:
        return
    g.last_rebalance_date = today

    if g.cooldown_until is not None and today <= g.cooldown_until:
        _move_to_safe_asset(context, "冷静期内")
        return

    targets = _select_targets(context)
    if not targets:
        _move_to_safe_asset(context, "没有满足阈值的强势 ETF")
        return

    scores = [payload["score"] for _, payload in targets]
    strength = sum(scores) / len(scores)
    if strength < g.min_strength:
        _move_to_safe_asset(context, f"强度不足 strength={strength:.4f}")
        return

    exposure = min(1.0, max(0.25, (strength - g.min_strength) / 0.030))
    total_score = sum(max(payload["score"], 0.0) for _, payload in targets)
    if total_score <= 0:
        _move_to_safe_asset(context, "评分总和为零")
        return

    target_weights: Dict[str, float] = {}
    for code, payload in targets:
        target_weights[code] = exposure * max(payload["score"], 0.0) / total_score

    for stock in list(context.portfolio.positions.keys()):
        if stock not in target_weights and stock != g.safe_asset:
            order_target_value(stock, 0)

    current_data = get_current_data()
    tradable = {
        code: weight
        for code, weight in target_weights.items()
        if code in current_data and not current_data[code].paused
    }
    if not tradable:
        _move_to_safe_asset(context, "目标 ETF 停牌或不可交易")
        return

    total_weight = sum(tradable.values())
    if total_weight <= 0:
        _move_to_safe_asset(context, "目标权重为零")
        return

    for code, weight in tradable.items():
        order_target_value(code, context.portfolio.total_value * 0.98 * weight / total_weight)

    if g.safe_asset in context.portfolio.positions:
        order_target_value(g.safe_asset, 0)

    log.info(
        "[调仓] strength=%.4f exposure=%.2f 选中: %s",
        strength,
        exposure,
        ", ".join([f"{code}:{payload['score']:.4f}" for code, payload in targets]),
    )
